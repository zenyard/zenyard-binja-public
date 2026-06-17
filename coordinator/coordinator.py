from __future__ import annotations

import queue
import threading
import typing as ty

from binaryninja import (
    BackgroundTaskThread,
    BinaryView,
    execute_on_main_thread,
    execute_on_main_thread_and_wait,
)  # type: ignore[import]

from ..api_client import make_client
from ..change_tracker import ChangeTracker
from ..configuration import (
    DEFAULT_MAX_BINARY_SIZE_MB,
    get_cached_max_binary_size_mb,
    save_max_binary_size_mb,
)
from ..helpers.analytics import track_file_open
from ..helpers.inference_types import InferenceItem
from ..helpers.log import (
    bind_logger,
    log_debug,
    log_info,
    log_request_error,
    log_warn,
)
from ..helpers.retry import (
    Disposition,
    RetryPolicy,
    _GaveUp,
    call_backend,
)
from ..helpers.sections import binary_mapped_size
from ..mcp_server.endpoint import BinaryMcpEndpoint
from ..mcp_server.ports import get_port_pool
from ..model import Model
from ..ui.dialogs import show_auth_error, show_size_limit_exceeded
from ..zenyard_client import ApiClient, BinariesApi, UserApi
from .classes import (
    UserAction,
)
from .setup_gate import ensure_setup
from ..ui.status_bar.state import RunSnapshot


from ..tasks.apply_inferences import ApplyInferencesTask  # noqa: E402
from ..tasks.bring_up import BringUpTask  # noqa: E402
from ..tasks.download_inferences import (  # noqa: E402
    DownloadInferencesTask,
)

_ShutdownSentinel: object = object()


def _resolve_max_binary_size_mb(
    client: ApiClient,
    on_permanent: ty.Callable[[Disposition], None],
) -> int:
    result = call_backend(
        "GET user_config",
        lambda: UserApi(client).get_user_config().max_binary_size_mb,
        RetryPolicy(max_retries=3, on_permanent=on_permanent),
    )
    if not isinstance(result, _GaveUp) and result is not None and result > 0:
        save_max_binary_size_mb(result)
        return result
    return get_cached_max_binary_size_mb() or DEFAULT_MAX_BINARY_SIZE_MB


class Coordinator(BackgroundTaskThread):
    """
    Per-BinaryView coordinator. Slim orchestrator over three Tasks:
    ``BringUpTask`` (one-shot per request), ``DownloadInferencesTask``
    (long-lived, signal-driven via ``set_target`` / ``request_drain``), and
    ``ApplyInferencesTask`` (always-running consumer of the shared inference
    channel).

    Owns the Model, the bounded inference channel, the ChangeTracker, and
    the API client. The action queue surfaces UserActions (and the shutdown
    sentinel) to a simple control-flow run loop; the FSM is gone.
    """

    def __init__(self, bv: BinaryView) -> None:
        super().__init__("Zenyard", can_cancel=False)
        self._bv = bv
        self._logger = bv.create_logger("Zenyard")
        self._model = Model.create(bv)
        self._api: BinariesApi | None = None
        self._client: ApiClient | None = None
        self._size_blocked = False
        self._auth_blocked = False
        self._stale_binary = False
        self._stop = threading.Event()
        self._channel: queue.Queue[tuple[list[InferenceItem], int]] = (
            queue.Queue(maxsize=1)
        )
        self._change_tracker = ChangeTracker(self._model)
        self._change_tracker_registered = False
        self._download: DownloadInferencesTask | None = None
        self._apply: ApplyInferencesTask | None = None
        self._current_bring_up: BringUpTask | None = None
        self._bring_up_active = False
        self._actions: queue.Queue[object] = queue.Queue()
        self._mcp = BinaryMcpEndpoint(
            self._bv, ports=get_port_pool(), logger=self._logger
        )

    # ── Public surface ────────────────────────────────────────────────────────

    @property
    def model(self) -> Model:
        return self._model

    def post(self, action: UserAction) -> None:
        self._actions.put(action)

    def first_revision_done(self) -> bool:
        return self._model.last_completed_revision > 0

    def agent_upstream_id(self) -> str | None:
        """Relay routing id for the Zenyard Agent, or None if the relay is down.

        None whenever the MCP relay subprocess isn't running (no API key, or it
        hasn't started yet) — the menu action is gated on a non-None value, the
        same way the IDA plugin disables the action until its relay is up.
        Safe to read from the Qt main thread (``relay_running`` is GIL-atomic).
        """
        return self._mcp.upstream_id if self._mcp.relay_running else None

    def progress_snapshot(self) -> RunSnapshot:
        """Lock-safe view of run state for the status-bar widget.

        Called from the Qt main thread (the status-bar driver). Reads the
        background tasks' monotonic counters (GIL-atomic) and the model under
        its lock, assembling a frozen value object — the single wiring seam.
        Never hands out a live task object.
        """

        bu = self._current_bring_up
        dl = self._download
        ap = self._apply

        bring_up_active = self._bring_up_active
        download_working = dl is not None and not dl.is_idle()
        download_waiting = bool(
            download_working and dl is not None and dl.waiting
        )
        analysis_ready = bool(dl is not None and dl.analysis_ready)
        download_active = bool(
            download_working and not download_waiting and not analysis_ready
        )
        apply_active = ap is not None and not ap.is_idle()

        downloaded = dl.downloaded if dl is not None else 0
        applied = ap.applied if ap is not None else 0
        queued = max(0, downloaded - applied)

        server_revision = dl.server_revision if dl is not None else 0.0
        target_revision = dl.target_revision if dl is not None else 0
        queue_position = dl.queue_position if dl is not None else None

        if bring_up_active and bu is not None:
            connection_failures = bu.connection_failures
        elif dl is not None:
            connection_failures = dl.consecutive_failures
        else:
            connection_failures = 0

        m = self._model
        with m._lock:
            binary_registered = m.binary_id is not None
            first_revision_done = m.last_completed_revision > 0
            applied_total = m.applied_count
            dirty_count = (
                len(m.dirty_functions)
                + len(m.dirty_globals)
                + len(m.removed_functions)
                + len(m.removed_globals)
            )

        return RunSnapshot(
            binary_registered=binary_registered,
            bring_up_active=bring_up_active,
            first_revision_done=first_revision_done,
            download_active=download_active,
            download_waiting=download_waiting,
            analysis_ready=analysis_ready,
            apply_active=apply_active,
            server_revision=server_revision,
            target_revision=target_revision,
            queue_position=queue_position,
            connection_failures=connection_failures,
            auth_blocked=self._auth_blocked,
            stale_binary=self._stale_binary,
            objects_uploaded=bu.objects_uploaded if bu is not None else 0,
            objects_total=bu.objects_total if bu is not None else 0,
            downloaded=downloaded,
            applied=applied,
            queued=queued,
            applied_total=applied_total,
            dirty_count=dirty_count,
        )

    def is_idle(self) -> bool:
        return (
            not self._bring_up_active
            and (self._download is None or self._download.is_idle())
            and (self._apply is None or self._apply.is_idle())
        )

    def request_shutdown(self) -> None:
        self._stop.set()
        self._actions.put(_ShutdownSentinel)
        log_debug(f"request shutdown {self._bv._file.filename}")

    # ── Run loop ──────────────────────────────────────────────────────────────

    def run(self) -> None:
        bind_logger(self._logger)
        if not self._await_setup():
            return
        self._client = make_client()
        self._api = BinariesApi(self._client)
        # Fire-and-forget "File - Open" analytics, once per opened binary. Runs
        # before the size check so a size-blocked open is still reported.
        track_file_open(self._bv)
        self._check_binary_size_allowed()

        try:
            # Start the MCP server + relay immediately, independent of binary_id,
            try:
                self._mcp.start(binary_id=self._model.binary_id)
            except Exception as e:
                log_request_error("Coordinator: failed to start MCP server", e)

            if not self._size_blocked:
                self._run_bring_up()
            if self._stop.is_set():
                return
            # Registration may not have happened
            # Don't exit: stay in the action loop so a later "Create Revision"
            # can register and bring up.
            # The MCP server + relay stay available even while unregistered
            if not self._size_blocked and self._model.binary_id is not None:
                self._mcp.set_binary_id(self._model.binary_id)
                self._enter_steady_state()
            else:
                log_debug(
                    "Coordinator: binary not registered; hosting MCP+relay only"
                )

            while not self._stop.is_set():
                try:
                    action = self._actions.get(timeout=0.5)
                except queue.Empty:
                    continue
                if action is _ShutdownSentinel:
                    return
                assert isinstance(action, UserAction)
                self._handle_action(action)
        except Exception as e:
            # Per-action failures are isolated in `_handle_action`; this catches an unexpected escape from the run loop itself
            log_request_error(
                "Coordinator: unexpected error; tearing down session", e
            )
        finally:
            self._do_shutdown()

    def _await_setup(self) -> bool:
        """
        Block until the machine is onboarded (EULA accepted + API key set).
        Returns True once onboarded; False only on shutdown.
        """
        if ensure_setup():
            return True
        log_debug("Coordinator: setup incomplete — awaiting analyze to retry")
        while not self._stop.is_set():
            try:
                action = self._actions.get(timeout=0.5)
            except queue.Empty:
                continue
            if action is _ShutdownSentinel:
                return False
            # Any UserAction here means "I want to start" → re-run onboarding.
            assert isinstance(action, UserAction)
            if ensure_setup():
                return True
        return False

    def _check_binary_size_allowed(self) -> bool:
        """Enforce the account's max-binary-size limit (hard block, no
        override — the server may raise the limit, so each call re-fetches).
        Sets ``_size_blocked`` and shows the notice when over the limit.
        """
        assert self._client is not None
        limit_mb = _resolve_max_binary_size_mb(
            self._client, self._on_permanent_error
        )
        size = binary_mapped_size(self._bv)
        if size > limit_mb * 2**20:
            log_warn(
                f"binary is {size} bytes — over the {limit_mb} MB limit;"
                " analysis disabled"
            )
            execute_on_main_thread_and_wait(
                lambda: show_size_limit_exceeded(limit_mb)
            )
            self._size_blocked = True
            return False
        self._size_blocked = False
        return True

    def _on_permanent_error(self, disposition: Disposition) -> None:
        if disposition is Disposition.AUTH:
            if self._auth_blocked:
                return
            self._auth_blocked = True
            log_warn(
                "Coordinator: authentication failed (401/403); analysis"
                " disabled until the API key is fixed"
            )
            execute_on_main_thread(show_auth_error)
        elif disposition is Disposition.STALE_BINARY:
            if self._stale_binary:
                return
            self._stale_binary = True
            log_warn(
                "Coordinator: binary not found server-side (404); re-run"
                " analysis to register it again"
            )

    def _enter_steady_state(self) -> None:
        """Register the change tracker and start the long-lived download/apply
        tasks. Idempotent: a no-op once started (guarded on ``_download``)."""
        if self._download is not None:
            return
        assert self._api is not None

        if self._model.binary_id is not None:
            self._mcp.set_binary_id(self._model.binary_id)
            self._ensure_inference_pipeline_started()
        else:
            log_debug(
                "Coordinator: binary not registered; hosting MCP+relay only"
            )

        # Stay alive while the file is open — even when unregistered — so
        # the MCP server and relay remain available.
        while not self._stop.is_set():
            try:
                action = self._actions.get(timeout=0.5)
            except queue.Empty:
                continue
            if action is _ShutdownSentinel:
                return
            assert isinstance(action, UserAction)
            self._handle_action(action)

    def _ensure_inference_pipeline_started(self) -> None:
        """Idempotently start the change tracker + apply/download tasks.

        Requires ``binary_id`` to be known. Safe to call repeatedly; only the
        first call starts the tasks."""
        if self._apply is not None:
            return
        assert self._api is not None
        self._bv.register_notification(self._change_tracker)
        self._change_tracker_registered = True
        log_debug("change tracker registered")

        self._apply = ApplyInferencesTask(
            bv=self._bv,
            model=self._model,
            channel=self._channel,
            change_tracker=self._change_tracker,
            stop=self._stop,
            logger=self._logger,
        )
        self._apply.start()
        self._download = DownloadInferencesTask(
            api=self._api,
            model=self._model,
            channel=self._channel,
            stop=self._stop,
            logger=self._logger,
            on_permanent_error=self._on_permanent_error,
        )
        self._download.start()

        # Start polling whenever there's a completed revision. With auto-apply on we always poll
        if self.first_revision_done():
            auto_apply = self._model.auto_apply
            just_uploaded = (
                self._current_bring_up is not None
                and self._current_bring_up.objects_uploaded > 0
            )
            if auto_apply or just_uploaded:
                self._download.set_target(
                    target_revision=self._model.last_completed_revision,
                    start_cursor=self._model.inference_cursor,
                    apply=auto_apply,
                )
                if not auto_apply:
                    log_info(
                        "auto-apply off; polling for readiness — apply via"
                        " 'Check Inferences' when ready"
                    )

    def _run_bring_up(self, *, prompt_intro: bool = True) -> None:
        assert self._api is not None
        self._current_bring_up = BringUpTask(
            bv=self._bv,
            api=self._api,
            model=self._model,
            stop=self._stop,
            prompt_intro=prompt_intro,
            logger=self._logger,
            on_permanent_error=self._on_permanent_error,
        )
        # Retain the reference after join so the status bar can keep reading
        # the final upload counts; only the active flag flips back off.
        self._bring_up_active = True
        self._current_bring_up.start()
        self._current_bring_up.join()
        self._bring_up_active = False

    def _handle_action(self, action: UserAction) -> None:
        # One bad action must never tear down the coordinator: an unhandled
        # exception here would escape both action loops (``run`` and
        # ``_enter_steady_state``), hit ``run``'s ``finally`` and shut down the
        # MCP server, relay, and steady-state tasks for the whole session. Log
        # and continue instead — matching IDA/Ghidra, where a task failure is
        # isolated and never kills the plugin. (``_stop``-driven control flow
        # uses return, not exceptions, so this does not swallow shutdown.)
        try:
            if action.kind == "ensure_setup":
                ensure_setup()
            elif action.kind == "create_revision":
                self._handle_create_revision()
            elif action.kind == "check_inferences":
                self._handle_check_inferences()
        except Exception as e:
            log_request_error(
                f"Coordinator: action {action.kind!r} failed; continuing", e
            )

    def _handle_create_revision(self) -> None:
        if self._auth_blocked:
            execute_on_main_thread(show_auth_error)
            return
        if self._size_blocked and not self._check_binary_size_allowed():
            return

        if self._model.binary_id is None:
            # The Create-Revision click is itself the user's intent — don't
            # re-show the intro prompt on this unregistered re-run.
            self._run_bring_up(prompt_intro=False)
            if self._stop.is_set() or self._model.binary_id is None:
                return
            self._mcp.set_binary_id(self._model.binary_id)
            self._enter_steady_state()
            return

        assert self._download is not None
        assert self._apply is not None
        log_debug("create_revision: draining download…")
        self._download.request_drain()
        self._download.wait_idle()
        self._apply.wait_idle()
        if self._stop.is_set():
            return
        log_debug("create_revision: starting dirty-only bring-up")
        self._run_bring_up()
        if self._stop.is_set():
            return

        # Always poll the server after an upload.
        auto_apply = self._model.auto_apply
        self._download.set_target(
            target_revision=self._model.last_completed_revision,
            start_cursor=self._model.inference_cursor,
            apply=auto_apply,
        )
        if not auto_apply:
            log_info(
                "auto-apply off; polling for readiness — apply via"
                " 'Check Inferences' when ready"
            )

    def _handle_check_inferences(self) -> None:
        if self._auth_blocked:
            execute_on_main_thread(show_auth_error)
            return

        if self._model.binary_id is None or self._download is None:
            return
        m = self._model

        log_debug("check_inferences: draining download…")
        self._download.request_drain()
        self._download.wait_idle()
        if self._stop.is_set():
            return
        log_info("check_inferences: applying results")
        self._download.set_target(
            target_revision=m.last_completed_revision,
            start_cursor=m.inference_cursor,
        )

    def _do_shutdown(self) -> None:
        log_debug(f"Coordinator: shutting down. {self._bv._file.filename}")
        try:
            self._mcp.stop()
        except Exception:
            log_debug("Coordinator: error stopping MCP server / relay")
        if self._download is not None:
            self._download.cancel()
        if self._apply is not None:
            self._apply.cancel()
        if self._current_bring_up is not None:
            self._current_bring_up.cancel()
        if self._change_tracker_registered:
            try:
                self._bv.unregister_notification(self._change_tracker)
            except Exception:
                pass
            self._change_tracker_registered = False


# ── Process-level registry ────────────────────────────────────────────────────

_coordinators: dict[str, Coordinator] = {}
_coordinators_lock = threading.Lock()


def get_coordinator_for_bv(bv: BinaryView) -> Coordinator | None:
    with _coordinators_lock:
        return _coordinators.get(bv._file.filename)


def on_bv_created(bv: BinaryView) -> None:
    if bv.view_type == "Raw":
        return
    filename = bv._file.filename
    with _coordinators_lock:
        if filename in _coordinators:
            return
        coord = Coordinator(bv)
        _coordinators[filename] = coord
    coord.start()


def shutdown_coordinators_for_file(filename: str) -> None:
    to_shutdown: list[Coordinator] = []
    with _coordinators_lock:
        for bv_id, coord in list(_coordinators.items()):
            if coord._bv.file.filename == filename:
                del _coordinators[bv_id]
                to_shutdown.append(coord)
    for coord in to_shutdown:
        coord.request_shutdown()
