from __future__ import annotations

import queue
import threading
import time
import traceback
import typing as ty
from dataclasses import dataclass

from binaryninja.log import Logger

from ..helpers.inference_types import InferenceItem
from ..helpers.log import (
    log_debug,
    log_error,
    log_info,
)
from ..helpers.retry import (
    Disposition,
    RetryPolicy,
    _GaveUp,
    call_backend,
)
from ..model import Model
from ..zenyard_client import BinariesApi
from ..zenyard_client.models import BinaryStateQueued, Inference

from .base import LongLivedTask, TaskCancelled

_STATUS_POLL_INITIAL = 3.0
_STATUS_POLL_MAX = 60.0
_ERROR_BACKOFF_BASE = 2.0
_ERROR_BACKOFF_MAX = 60.0


# def _concise_error(e: Exception) -> str:
#     if isinstance(e, ApiException):
#         return f"HTTP {e.status} {e.reason}"
#     flattened = " ".join(str(e).split())
#     return f"{type(e).__name__}: {flattened}" if flattened else type(e).__name__


@dataclass(frozen=True)
class _Target:
    target_revision: int
    start_cursor: int | None
    apply: bool = True


class DownloadInferencesTask(LongLivedTask):
    """
    Long-lived. Drives one fetch cycle per ``set_target(...)`` call: polls the
    server until the target revision is analysed, then fetches inference pages
    and pushes them — each as ``(items, end_cursor)`` — onto a shared
    ``queue.Queue`` (one page per slot). The queue is bounded to size 1 — when
    ``ApplyInferencesTask`` is behind, the next ``put`` blocks; that is the
    backpressure mechanism. This task never writes ``model.inference_cursor``;
    the apply side persists it once a page is actually applied.

    Cooperative interruption while a cycle is in flight is via
    ``request_drain()``: at the next page boundary (or while blocked on
    ``channel.put``) the cycle exits cleanly and the Task returns to idle. The
    Coordinator uses this during Create Revision to stop the stream so a new
    dirty-only bringup can run.

    Errors on individual API calls are classified (``helpers.retry.classify``):
    transient ones (connection loss, timeouts, 5xx) are retried **forever**
    with exponential backoff — an outage can never end the cycle, so the
    stream resumes by itself when connectivity returns. Any other disposition
    (auth, stale binary, bug) stops the cycle cleanly with a log; retrying
    those cannot help. ``consecutive_failures`` exposes the current outage to
    the status bar ("Reconnecting…").
    """

    def __init__(
        self,
        *,
        api: BinariesApi,
        model: Model,
        channel: "queue.Queue[tuple[list[InferenceItem], int]]",
        stop: threading.Event,
        logger: Logger | None = None,
        on_permanent_error: ty.Callable[[Disposition], None] | None = None,
    ) -> None:
        super().__init__("", stop=stop, logger=logger)
        self._api = api
        self._model = model
        self._channel = channel
        # Notified (with the Disposition) when a cycle stops on a permanent,
        # non-transient error so the Coordinator can disable/surface it
        # (auth → dialog, stale → status). Wired via ``_cycle_policy``.
        self._on_permanent_error = on_permanent_error
        self._drain = threading.Event()
        self._max_server_revision: int | None = None
        self.downloaded = 0
        self.waiting = False
        self.analysis_ready = False
        # Server-side analysis progress, read GIL-atomically by the status bar
        # (Coordinator.progress_snapshot) to fill the "Analyzing on server" bar.
        # ``server_revision / target_revision`` is the same ratio the poll loop
        # uses to decide completion, so the bar hits 100% exactly when ready.
        self.server_revision: float = 0.0
        self.target_revision: int = 0
        # Queue position while the server holds the binary in its analysis
        # queue (``BinaryStateQueued``); None once analysis runs. Read
        # GIL-atomically by the status bar, like ``server_revision``.
        self.queue_position: int | None = None
        # Consecutive transient API failures in the current cycle; reset on
        # every success. Read GIL-atomically by the status bar to surface
        # "Reconnecting…" once an outage outlives a short grace.
        self.consecutive_failures = 0

    # ── Public signal-in ──────────────────────────────────────────────────────

    def set_target(
        self,
        *,
        target_revision: int,
        start_cursor: int | None,
        apply: bool = True,
    ) -> None:
        self._drain.clear()
        self.analysis_ready = False
        self._submit(
            _Target(
                target_revision=target_revision,
                start_cursor=start_cursor,
                apply=apply,
            )
        )

    def request_drain(self) -> None:
        self.analysis_ready = False
        self._drain.set()

    # ── LongLivedTask hook ────────────────────────────────────────────────────

    def _do_work(self, payload: object) -> None:
        assert isinstance(payload, _Target)
        target = payload
        binary_id = self._model.binary_id
        if binary_id is None:
            log_debug("DownloadInferencesTask: no binary_id yet — skipping")
            return
        self._max_server_revision = None
        self.analysis_ready = False
        # Reset progress at cycle start (not just inside the poll loop) so the
        # first "server" tick of a new revision never reads the previous
        # cycle's stale full bar.
        self.target_revision = target.target_revision
        self.server_revision = 0.0
        self.queue_position = None
        self.consecutive_failures = 0

        self.waiting = True
        try:
            ready = self._poll_until_ready(target.target_revision)
            if ready and not target.apply:
                self.analysis_ready = True
        finally:
            self.waiting = False
            self.queue_position = None
        if not ready:
            return

        if not target.apply:
            # Poll-only auto-apply off
            log_info(
                f"analysis ready for revision {target.target_revision};"
                " auto-apply off — awaiting manual apply"
            )
            return

        cursor = target.start_cursor
        while not self.is_cancelled() and not self._drain.is_set():
            page = self._fetch_page(target.target_revision, cursor)
            if page is None:
                return
            items, next_cursor, done = page
            # Every page travels with its end cursor — even empty ones — so
            # ApplyInferencesTask can persist the cursor once the page is
            # applied. A close mid-flight then replays instead of skipping.
            if not self._put_with_drain((items, next_cursor)):
                return
            self.downloaded += len(items)
            if done:
                log_info(
                    f"download cycle complete for revision {target.target_revision}"
                )
                return
            cursor = next_cursor

    # ── Polling ───────────────────────────────────────────────────────────────

    def _poll_until_ready(self, target_revision: int) -> bool:
        """Block until ``server_revision >= target_revision``.
        Returns True if ready, False on cancel/drain or a non-transient
        error. The status call runs through ``call_backend``, so transient
        errors are retried forever with backoff — an outage can never make
        this poll give up — while a drain/cancel or permanent disposition
        ends it (``_GaveUp``)."""
        interval = _STATUS_POLL_INITIAL
        prev_progress: float | None = None
        prev_queue_position: int | None = None
        binary_id = self._model.binary_id
        assert binary_id is not None

        while True:
            self.check_cancelled()
            if self._drain.is_set():
                return False
            status = call_backend(
                "GET /detailed_status",
                lambda: self._api.get_detailed_status(str(binary_id)),
                self._cycle_policy(),
            )
            if isinstance(status, _GaveUp):
                return False

            state_inst = status.state.actual_instance
            queue_position = (
                state_inst.queue_position
                if isinstance(state_inst, BinaryStateQueued)
                else None
            )
            self.queue_position = queue_position

            server_revision = self._server_revision_from(
                status, target_revision
            )
            self.server_revision = server_revision

            log_info(
                f"binary state: {type(status.state.actual_instance).__name__},"
                f" server_revision: {server_revision:.3f}, target: {target_revision}"
            )

            if server_revision >= target_revision:
                log_info(f"analysis complete for revision {target_revision}")
                return True

            rev_status = next(
                (
                    r
                    for r in status.revision_analyses
                    if r.revision == target_revision
                ),
                None,
            )
            # rev_status found ⇒ revision_analyses non-empty, so progress is
            # real; otherwise fall back to a blind doubling backoff.
            if rev_status is not None:
                cur_progress = rev_status.progress
                if cur_progress != prev_progress:
                    interval = _STATUS_POLL_INITIAL
                else:
                    interval = min(interval * 2, _STATUS_POLL_MAX)
                prev_progress = cur_progress
            else:
                interval = min(interval * 2, _STATUS_POLL_MAX)
                prev_progress = None

            if queue_position != prev_queue_position:
                # A moving queue position is progress too — reset the backoff
                # so the displayed position stays fresh while queued.
                interval = _STATUS_POLL_INITIAL
            prev_queue_position = queue_position

            if not self._sleep_or_interrupt(interval):
                return False

    # ── Fetching ──────────────────────────────────────────────────────────────

    def _fetch_page(
        self, target_revision: int, cursor: int | None
    ) -> tuple[list[InferenceItem], int, bool] | None:
        """Fetch one page. Returns ``(items, next_cursor, done)`` where
        ``next_cursor`` is the fresh server cursor and ``done`` is True iff
        this is the terminal page. Returns None on cancel/drain or a
        non-transient error (``_GaveUp`` from ``call_backend``); transient
        errors are retried forever."""
        binary_id = self._model.binary_id
        assert binary_id is not None

        while True:
            self.check_cancelled()
            if self._drain.is_set():
                return None
            result = call_backend(
                "GET /inferences",
                lambda: self._api.get_inferences(
                    target_revision,
                    str(binary_id),
                    cursor=cursor,
                    limit=50,
                ),
                self._cycle_policy(),
            )
            if isinstance(result, _GaveUp):
                return None

            concrete: list[InferenceItem] = []
            for item in result.inferences:
                try:
                    inference = item.actual_instance
                    if (
                        isinstance(inference, Inference)
                        and inference.actual_instance is not None
                    ):
                        concrete.append(inference.actual_instance)  # type: ignore[arg-type]
                except Exception as e:
                    # Poison isolation: one malformed inference must not
                    # stall the whole stream — drop it and keep going (the
                    # page cursor advances past it regardless).
                    log_error(f"dropping malformed inference: {e!r}")

            if result.has_next:
                if not concrete:
                    # Pure pacing — a drain still forwards the page (the
                    # cursor must travel); the put below drops it if drained.
                    self._sleep_or_interrupt(_STATUS_POLL_INITIAL)
                log_info(
                    f"fetched {len(concrete)} inference(s),"
                    f" cursor {cursor} → {result.cursor}, has_next=True"
                )
                return concrete, result.cursor, False

            # Terminal page — confirm the server really reached the target
            # before calling the cycle done. The status check is its own
            # ``call_backend``: a transient blip retries the (idempotent)
            # status check, never fabricates "done"; a permanent/drain stops it.
            status = call_backend(
                "GET /detailed_status",
                lambda: self._api.get_detailed_status(str(binary_id)),
                self._cycle_policy(),
            )
            if isinstance(status, _GaveUp):
                return None
            server_revision = self._server_revision_from(
                status, target_revision
            )
            log_info(
                f"fetched {len(concrete)} inference(s),"
                f" cursor {cursor} → {result.cursor}, has_next=False,"
                f" server_revision={server_revision:.3f}"
            )
            if server_revision >= target_revision:
                return concrete, result.cursor, True

            log_info(
                f"server_revision {server_revision:.3f} < {target_revision},"
                f" retrying after sleep"
            )
            if not self._sleep_or_interrupt(_STATUS_POLL_INITIAL):
                return None

    def _server_revision_from(
        self, status: object, target_revision: int
    ) -> float:
        analyses = status.revision_analyses  # type: ignore[attr-defined]
        if analyses:
            max_rev = max(r.revision for r in analyses)
            self._max_server_revision = max(
                max_rev, self._max_server_revision or 0
            )
            missing = sum(1.0 - r.progress for r in analyses)
            return self._max_server_revision - missing
        return float(target_revision)

    def _on_error(self, exc: BaseException) -> None:
        # An exception escaping the cycle body idles this task with nothing
        # to re-arm it — make that loud (full traceback), not a one-line
        # mystery in the log.
        log_error(
            "DownloadInferencesTask: unexpected error; cycle abandoned\n"
            + "".join(traceback.format_exception(exc))
        )

    # ── Shared retry policy / sleep plumbing ──────────────────────────────────

    def _cycle_policy(self) -> RetryPolicy:
        """The cycle's per-call retry policy for ``call_backend``: transient
        errors (connection loss, timeouts, 5xx) retry forever with backoff — an
        outage can never end the cycle
        """
        return RetryPolicy(
            max_retries=None,
            base_delay=_ERROR_BACKOFF_BASE,
            max_delay=_ERROR_BACKOFF_MAX,
            stop=self._stop,
            should_stop=lambda: self._drain.is_set() or self.is_cancelled(),
            on_permanent=self._on_permanent_error,
            on_failure_count=lambda n: setattr(self, "consecutive_failures", n),
        )

    def _sleep_or_interrupt(self, seconds: float) -> bool:
        """Sleep ``seconds`` in ≤0.5 s slices, responsive to all three interrupts."""
        deadline = time.monotonic() + seconds
        while True:
            self.check_cancelled()
            if self._drain.is_set():
                return False
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return True
            if self._stop.wait(min(0.5, remaining)) or self.cancelled:
                raise TaskCancelled()

    # ── Channel put with cooperative drain ────────────────────────────────────

    def _put_with_drain(self, page: tuple[list[InferenceItem], int]) -> bool:
        """Put ``page`` on the channel, polling cancel/drain while blocked.
        Returns True on success, False if interrupted by cancel/drain."""
        while True:
            self.check_cancelled()
            if self._drain.is_set():
                return False
            try:
                self._channel.put(page, timeout=0.5)
                return True
            except queue.Full:
                continue
