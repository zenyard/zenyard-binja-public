from __future__ import annotations

import queue
import threading

from binaryninja import (
    BackgroundTaskThread,
    BinaryView,
    execute_on_main_thread_and_wait,
)  # type: ignore[import]

from ..api_client import make_client
from ..change_tracker import ChangeTracker
from ..configuration import (
    DEFAULT_MAX_BINARY_SIZE_MB,
    get_cached_max_binary_size_mb,
    save_max_binary_size_mb,
)
from ..helpers.inference_types import InferenceItem
from ..helpers.log import (
    bind_logger,
    log_debug,
    log_info,
    log_request_error,
    log_warn,
)
from ..helpers.sections import binary_mapped_size
from ..mcp_server.endpoint import BinaryMcpEndpoint
from ..mcp_server.ports import get_port_pool
from ..model import Model
from ..ui.dialogs import show_size_limit_exceeded
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


def _resolve_max_binary_size_mb(client: ApiClient) -> int:
    """The user's max-binary-size limit, in MB.

    Fetched from the server each session (the server owns trial/plan limits);
    the last-known value is cached machine-globally in ``~/.binja/zenyard.json``
    and used when the fetch fails, with a hardcoded default when nothing was
    ever fetched.
    """
    try:
        mb = UserApi(client).get_user_config().max_binary_size_mb
        if mb is not None and mb > 0:
            save_max_binary_size_mb(mb)
            return mb
    except Exception as e:
        log_request_error("failed to fetch user config", e)
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
        # One logger per open file, bound to this tab's BN session id, so its
        # output lands in this tab's Log panel instead of the shared session 0.
        # Threaded explicitly into the tasks, MCP endpoint, and relay below;
        # leaf call sites resolve it from the contextvar bound at thread entry.
        self._logger = bv.create_logger("Zenyard")
        self._model = Model.create(bv)
        self._api: BinariesApi | None = None
        self._client: ApiClient | None = None
        # Per-session verdict of the size gate — never persisted.
        self._size_blocked = False
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
        # Exclude the ready flag so the brief window after a poll-only cycle
        # drops ``waiting`` but before it returns can't read as "applying".
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
        # _stop set first so a BringUpTask blocked in the initial _run_bring_up
        # join can see is_cancelled() flip; the sentinel kicks the action loop.
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
        # Size gate before bring-up: an over-limit Binary must see the
        # "Binary Size Exceeded" notice before any intro prompt, registration
        # or upload. While blocked the coordinator stays alive hosting
        # MCP + relay only — the same posture as an unregistered Binary.
        self._check_binary_size_allowed()

        # `finally` guarantees teardown on every exit path now that the MCP
        # server + relay start before (and outlive) bring-up.
        try:
            # Start the MCP server + relay immediately, independent of
            # binary_id, so they persist for the lifetime of the open file.
            try:
                self._mcp.start(binary_id=self._model.binary_id)
            except Exception as e:
                log_request_error("Coordinator: failed to start MCP server", e)

            if not self._size_blocked:
                self._run_bring_up()
            if self._stop.is_set():
                return
            # Registration may not have happened — the user can cancel the
            # startup prompt, intending to load an image first and analyze
            # later. Don't exit: stay in the action loop so a later "Create
            # Revision" can register and bring up. The MCP server + relay stay
            # available even while unregistered. Steady-state tasks start only
            # once registered.
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
        finally:
            self._do_shutdown()

    def _await_setup(self) -> bool:
        """Block until the machine is onboarded (EULA accepted + API key set).

        The first ``ensure_setup`` call shows the onboarding modal. If the user
        cancels it we must *not* exit the thread: this coordinator stays in the
        process registry, so a dead ``run`` thread would leave ``_actions``
        undrained and the status-bar "Click to analyze with Zenyard" would post
        ``create_revision`` into a queue nobody reads — the click does nothing.
        Instead we stay alive in a minimal loop; any user action re-attempts
        setup, re-showing the modal. The consumed action is intentionally
        dropped: ``run`` then falls through to the cold-start ``_run_bring_up``,
        which is exactly what that action would have triggered. Re-posting it
        would instead run a redundant dirty-only bring-up once registered.

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
        limit_mb = _resolve_max_binary_size_mb(self._client)
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

    def _enter_steady_state(self) -> None:
        """Register the change tracker and start the long-lived download/apply
        tasks. Idempotent: a no-op once started (guarded on ``_download``)."""
        if self._download is not None:
            return
        assert self._api is not None

        # Bring-up has already run in the caller (`run` on cold start,
        # `_handle_create_revision` on late registration), which also guards
        # `_stop` afterwards. Repeating it here gave every startup a redundant
        # dirty-check pass ("no dirty objects to upload") and, worse, left
        # `_current_bring_up` pointing at that zeroed second task so the status
        # bar read 0/0 counts.
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
        )
        self._download.start()

        # Start polling whenever there's a completed revision. With auto-apply
        # on we always poll (so a reopened binary resumes applying any unconsumed
        # inferences). With auto-apply off we only poll when *this* session just
        # uploaded objects — i.e. the initial analysis that produced this
        # revision — so a reopened, already-applied binary doesn't show a false
        # "ready". The apply step itself is gated on the per-binary setting.
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
        )
        # Retain the reference after join so the status bar can keep reading
        # the final upload counts; only the active flag flips back off.
        self._bring_up_active = True
        self._current_bring_up.start()
        self._current_bring_up.join()
        self._bring_up_active = False

    def _handle_action(self, action: UserAction) -> None:
        if action.kind == "ensure_setup":
            ensure_setup()
        elif action.kind == "create_revision":
            self._handle_create_revision()
        elif action.kind == "check_inferences":
            self._handle_check_inferences()

    def _handle_create_revision(self) -> None:
        # While size-blocked, every retry re-checks against a fresh fetch (a
        # server-side limit raise unblocks live) and re-shows the notice
        # instead of dead-clicking — same retry shape as _await_setup.
        if self._size_blocked and not self._check_binary_size_allowed():
            return

        # Not yet registered (e.g. the user cancelled the startup prompt): this
        # is still an *initial* analysis, so re-run bring-up with the intro +
        # instructions prompts. They keep reappearing on every manual retry
        # until the first analysis actually registers (``binary_id`` set); the
        # registered branch below never prompts. This branch must precede the
        # asserts below, which assume the steady-state tasks exist.
        if self._model.binary_id is None:
            self._run_bring_up()
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
        # Always poll the server after an upload. When auto-apply is off the
        # cycle polls until ready then stops (surfacing the "ready" state)
        # instead of fetching+applying — the apply step is the only thing the
        # per-binary setting gates.
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
        # A stray "Check Inferences" while unregistered (no steady-state tasks)
        # must not crash the run loop — bail before touching _download.
        if self._model.binary_id is None or self._download is None:
            return
        m = self._model
        # Manual "apply now". Drain any in-flight cycle (e.g. a poll-only cycle
        # running because auto-apply is off — it only checks _drain, not the
        # pending payload) before submitting an apply target. The download task
        # polls until ready then fetches+applies; if analysis is not ready yet
        # the status bar already reflects that via the "server" state.
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
    # One coordinator per file. The registry is keyed by filename because the
    # UI resolves coordinators by the active view's filename, not by id(bv)
    # (see get_coordinator_for_bv). A second non-Raw view of the same file must
    # therefore reuse the existing coordinator, not overwrite it: overwriting
    # orphans the first coordinator's already-started BackgroundTaskThread,
    # which then never receives request_shutdown() (shutdown only reaches
    # coordinators still in the dict), so its download poller keeps running and
    # its MCP port is never returned to the pool. Claim the slot under the lock;
    # only start the thread once we own it.
    #
    # TODO: closing one of several same-file views still tears down the shared
    # coordinator. A complete fix reference-counts open views per filename and
    # shuts down only on the last close — pending confirmation that
    # on_bv_created / OnBeforeCloseFile fire in balanced pairs.
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
