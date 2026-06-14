from __future__ import annotations

import queue
import threading
from dataclasses import dataclass

from binaryninja.log import Logger

from ..helpers.inference_types import InferenceItem
from ..helpers.log import (
    log_api_error,
    log_debug,
    log_error,
    log_info,
    log_request_error,
)
from ..model import Model
from ..zenyard_client import ApiException, BinariesApi
from ..zenyard_client.models import BinaryStateQueued, Inference

from .base import LongLivedTask

_STATUS_POLL_INITIAL = 3.0
_STATUS_POLL_MAX = 60.0
_STATUS_ERROR_RETRIES = 10
_STATUS_ERROR_SLEEP = 5.0
_INFER_ERROR_RETRIES = 10
_INFER_ERROR_SLEEP = 5.0


@dataclass(frozen=True)
class _Target:
    target_revision: int
    start_cursor: int | None
    # When False, poll until the revision is analysed then stop (set
    # ``analysis_ready``) without fetching/applying — the auto-apply-off path.
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

    Errors on individual API calls retry up to ``_STATUS_ERROR_RETRIES`` /
    ``_INFER_ERROR_RETRIES`` with a fixed sleep; on exhaustion the cycle gives
    up and the Task returns to idle (the Coordinator can re-signal later via
    ``set_target``).
    """

    def __init__(
        self,
        *,
        api: BinariesApi,
        model: Model,
        channel: "queue.Queue[tuple[list[InferenceItem], int]]",
        stop: threading.Event,
        logger: Logger | None = None,
    ) -> None:
        super().__init__("", stop=stop, logger=logger)
        self._api = api
        self._model = model
        self._channel = channel
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
        """Block until ``server_revision >= target_revision`` or give up.
        Returns True if ready, False on cancel/drain/exhausted retries."""
        retries = 0
        interval = _STATUS_POLL_INITIAL
        prev_progress: float | None = None
        prev_queue_position: int | None = None
        max_rev: int | None = None
        binary_id = self._model.binary_id
        assert binary_id is not None

        while True:
            self.check_cancelled()
            if self._drain.is_set():
                return False
            try:
                status = self._api.get_detailed_status(str(binary_id))
                retries = 0
            except ApiException as e:
                log_api_error("GET /detailed_status failed", e)
                retries += 1
                if retries >= _STATUS_ERROR_RETRIES:
                    log_error(
                        "GET /detailed_status failed too many times; giving up"
                    )
                    return False
                self.sleep_or_cancel(_STATUS_ERROR_SLEEP)
                continue
            except Exception as e:
                log_request_error("GET /detailed_status request failed", e)
                retries += 1
                if retries >= _STATUS_ERROR_RETRIES:
                    log_error(
                        "GET /detailed_status request failed too many times; giving up"
                    )
                    return False
                self.sleep_or_cancel(_STATUS_ERROR_SLEEP)
                continue

            state_inst = status.state.actual_instance
            queue_position = (
                state_inst.queue_position
                if isinstance(state_inst, BinaryStateQueued)
                else None
            )
            self.queue_position = queue_position

            if status.revision_analyses:
                max_rev = max(r.revision for r in status.revision_analyses)
                self._max_server_revision = max(
                    max_rev, self._max_server_revision or 0
                )
                missing = sum(
                    1.0 - r.progress for r in status.revision_analyses
                )
                server_revision: float = self._max_server_revision - missing
            else:
                max_rev = None
                server_revision = float(target_revision)

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
            if rev_status is not None and max_rev is not None:
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

            self.sleep_or_cancel(interval)

    # ── Fetching ──────────────────────────────────────────────────────────────

    def _fetch_page(
        self, target_revision: int, cursor: int | None
    ) -> tuple[list[InferenceItem], int, bool] | None:
        """Fetch one page. Returns ``(items, next_cursor, done)`` where
        ``next_cursor`` is the fresh server cursor and ``done`` is True iff
        this is the terminal page. Returns None on cancel/drain/give-up."""
        retries = 0
        binary_id = self._model.binary_id
        assert binary_id is not None

        while True:
            self.check_cancelled()
            if self._drain.is_set():
                return None
            try:
                result = self._api.get_inferences(
                    target_revision,
                    str(binary_id),
                    cursor=cursor,
                    limit=50,
                )
                retries = 0
            except ApiException as e:
                log_api_error("GET /inferences failed", e)
                retries += 1
                if retries >= _INFER_ERROR_RETRIES:
                    log_error(
                        "GET /inferences failed too many times; giving up"
                    )
                    return None
                self.sleep_or_cancel(_INFER_ERROR_SLEEP)
                continue
            except Exception as e:
                log_request_error("GET /inferences request failed", e)
                retries += 1
                if retries >= _INFER_ERROR_RETRIES:
                    log_error(
                        "GET /inferences request failed too many times; giving up"
                    )
                    return None
                self.sleep_or_cancel(_INFER_ERROR_SLEEP)
                continue

            concrete: list[InferenceItem] = [
                item.actual_instance.actual_instance  # type: ignore[misc]
                for item in result.inferences
                if isinstance(item.actual_instance, Inference)
                and item.actual_instance.actual_instance is not None
            ]

            if result.has_next:
                if not concrete:
                    self.sleep_or_cancel(_STATUS_POLL_INITIAL)
                log_info(
                    f"fetched {len(concrete)} inference(s),"
                    f" cursor {cursor} → {result.cursor}, has_next=True"
                )
                return concrete, result.cursor, False

            server_revision = self._compute_server_revision(target_revision)
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
            self.sleep_or_cancel(_STATUS_POLL_INITIAL)

    def _compute_server_revision(self, target_revision: int) -> float:
        binary_id = self._model.binary_id
        assert binary_id is not None
        try:
            status = self._api.get_detailed_status(str(binary_id))
        except Exception:
            return float(target_revision)
        if status.revision_analyses:
            max_rev = max(r.revision for r in status.revision_analyses)
            self._max_server_revision = max(
                max_rev, self._max_server_revision or 0
            )
            missing = sum(1.0 - r.progress for r in status.revision_analyses)
            return self._max_server_revision - missing
        return float(target_revision)

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
