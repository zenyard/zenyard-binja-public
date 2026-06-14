from __future__ import annotations

import queue
import threading

from binaryninja import (  # type: ignore[import]
    BinaryView,
    execute_on_main_thread_and_wait,
)
from binaryninja.log import Logger

from ..change_tracker import ChangeTracker
from ..helpers.apply_inferences import apply_inferences
from ..helpers.inference_types import InferenceItem
from ..helpers.log import log_error, use_logger
from ..model import Model

from .base import CancellableTask


class ApplyInferencesTask(CancellableTask):
    """
    Always-running consumer for the Coordinator's lifetime. Pulls inference
    pages — ``(items, end_cursor)`` — from a shared queue and applies them on
    the main thread, with ChangeTracker paused around each apply so the BV
    writes aren't observed as user edits. ``model.inference_cursor`` is
    persisted here, only after a page is applied: the cursor means "applied
    up to here", so a shutdown mid-page replays it next session instead of
    silently skipping it.

    Started once at Coordinator start; only stops when ``cancel()`` is invoked
    (during Coordinator shutdown). When the queue is empty the 0.5s tick
    keeps the cancel/shutdown path responsive.

    Exposes ``is_idle()`` / ``wait_idle()`` so the Coordinator can block on
    the channel being drained during Create Revision. Channel observation
    and the in-batch flag are read together under a Condition lock so the
    "between get_nowait and in_batch=True" window cannot produce a false
    idle: ``_take_next_batch`` performs both under the same lock.
    """

    def __init__(
        self,
        *,
        bv: BinaryView,
        model: Model,
        channel: "queue.Queue[tuple[list[InferenceItem], int]]",
        change_tracker: ChangeTracker,
        stop: threading.Event,
        logger: Logger | None = None,
    ) -> None:
        super().__init__("", stop=stop, logger=logger)
        self._bv = bv
        self._model = model
        self._channel = channel
        self._change_tracker = change_tracker
        self._idle_cv = threading.Condition()
        self._in_batch = False
        self.applied = 0

    # ── Public idle observation ───────────────────────────────────────────────

    def is_idle(self) -> bool:
        with self._idle_cv:
            return self._channel.empty() and not self._in_batch

    def wait_idle(self, timeout: float | None = None) -> bool:
        with self._idle_cv:
            return self._idle_cv.wait_for(
                lambda: self._channel.empty() and not self._in_batch,
                timeout=timeout,
            )

    # ── Loop ──────────────────────────────────────────────────────────────────

    def _run(self) -> None:
        while not self.is_cancelled():
            page = self._take_next_batch()
            if page is None:
                self.sleep_or_cancel(0.5)
                continue
            batch, cursor = page
            try:
                if batch:
                    count: list[int] = []
                    with self._change_tracker.paused():
                        # apply_inferences runs on the main thread, whose
                        # context isn't bound to this session — scope the bind
                        # to the callback so its log lines route to this tab,
                        # then revert.
                        def _apply() -> None:
                            with use_logger(self._logger):
                                count.append(
                                    apply_inferences(
                                        self._bv, batch, self._model
                                    )
                                )

                        execute_on_main_thread_and_wait(_apply)
                        # Drain analysis here (BG thread) so notifications
                        # fired during the analysis pass still see paused ==
                        # True. BN forbids waiting from the UI thread, so it
                        # can't happen inside the lambda above.
                        self._bv.update_analysis_and_wait()
                    applied = count[0] if count else 0
                    self.applied += applied
                    self._model.add_applied(applied)
                # Only now is the page durably applied; an exception above
                # leaves the cursor behind so the page replays next session.
                self._model.inference_cursor = cursor
            except Exception as e:
                log_error(f"apply batch failed: {e}")
            finally:
                with self._idle_cv:
                    self._in_batch = False
                    self._idle_cv.notify_all()

    def _take_next_batch(
        self,
    ) -> tuple[list[InferenceItem], int] | None:
        """Under the idle lock: either grab one page from the channel and
        mark in-batch, or notify idle waiters that we are still empty."""
        with self._idle_cv:
            try:
                page = self._channel.get_nowait()
            except queue.Empty:
                self._idle_cv.notify_all()
                return None
            self._in_batch = True
            return page
