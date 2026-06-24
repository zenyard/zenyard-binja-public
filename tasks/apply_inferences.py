from __future__ import annotations

import queue
import threading

from binaryninja import (  # type: ignore[import]
    BinaryView,
)
from binaryninja.log import Logger  # type: ignore[import]

from ..helpers.apply_inferences import BaselineCache, apply_inferences
from ..helpers.inference_types import ChannelItem, _EndOfStream
from ..helpers.log import log_debug, log_error, use_logger
from ..helpers.main_thread import run_on_main_thread
from ..model import Model

from .base import CancellableTask, TaskCancelled


_LOOP_RECOVER_DELAY = 2.0

# Mid-burst safety valve: settle (run deferred analysis once, re-arm the hold)
# if a single uninterrupted burst ever exceeds this many pages. Normal bursts
# settle once at the end, when DownloadInferencesTask sends END_OF_STREAM.
_SETTLE_EVERY_PAGES = 10_000


class ApplyInferencesTask(CancellableTask):
    def __init__(
        self,
        *,
        bv: BinaryView,
        model: Model,
        channel: "queue.Queue[ChannelItem]",
        stop: threading.Event,
        logger: Logger | None = None,
    ) -> None:
        super().__init__("", stop=stop, logger=logger)
        self._bv = bv
        self._model = model
        self._channel = channel
        self._idle_cv = threading.Condition()
        self._in_batch = False
        self._hold_active = False
        self.applied = 0
        # Capture-before-write user baselines, shared across every page of this
        # single analysis so the signature handlers (param rename + param/return
        # types) never mistake zenyard's own earlier write for a user edit.
        self._baseline: BaselineCache = {}

    # ── Public idle observation ───────────────────────────────────────────────

    def is_idle(self) -> bool:
        with self._idle_cv:
            return self._is_idle_locked()

    def wait_idle(self, timeout: float | None = None) -> bool:
        with self._idle_cv:
            return self._idle_cv.wait_for(
                self._is_idle_locked,
                timeout=timeout,
            )

    def _is_idle_locked(self) -> bool:
        return (
            self._channel.empty()
            and not self._in_batch
            and not self._hold_active
        )

    # ── Loop ──────────────────────────────────────────────────────────────────

    def _run(self) -> None:
        pages_since_settle = 0
        try:
            while not self.is_cancelled():
                item = None
                try:
                    item = self._take_next_batch()
                    if item is None:
                        # Empty queue: wait for the next page or the producer's
                        # END_OF_STREAM marker (which is what settles a burst).
                        self.sleep_or_cancel(0.5)
                        continue
                    if isinstance(item, _EndOfStream):
                        # Producer finished this cycle — settle the deferred
                        # analysis and release the hold (cursor already current).
                        if self._hold_active:
                            self._end_burst()
                        continue
                    batch, cursor = item
                    if batch:
                        if not self._hold_active:
                            self._begin_burst()
                            pages_since_settle = 0

                        def _apply() -> int:
                            with use_logger(self._logger):
                                return apply_inferences(
                                    self._bv,
                                    batch,
                                    self._model,
                                    self._baseline,
                                )

                        applied = run_on_main_thread(_apply)
                        self.applied += applied
                        self._model.add_applied(applied)
                        pages_since_settle += 1
                        if pages_since_settle >= _SETTLE_EVERY_PAGES:
                            self._settle_keeping_hold()
                            pages_since_settle = 0
                    self._model.inference_cursor = cursor
                except TaskCancelled:
                    # Cancellation/shutdown still ends the consumer cleanly.
                    raise
                except Exception as e:
                    log_error(f"apply loop error; continuing: {e}")
                    self.sleep_or_cancel(_LOOP_RECOVER_DELAY)
                finally:
                    if item is not None:
                        with self._idle_cv:
                            self._in_batch = False
                            self._idle_cv.notify_all()
        finally:
            self._release_hold_no_wait()

    # ── Analysis-hold lifecycle ───────────────────────────────────────────────

    def _begin_burst(self) -> None:
        self._bv.set_analysis_hold(True)
        with self._idle_cv:
            self._hold_active = True
        log_debug("apply: analysis hold acquired for burst")

    def _settle_keeping_hold(self) -> None:
        self._bv.set_analysis_hold(False)
        self._bv.update_analysis_and_wait()
        self._bv.set_analysis_hold(True)

    def _end_burst(self) -> None:
        try:
            self._bv.set_analysis_hold(False)
            self._bv.update_analysis_and_wait()
        finally:
            with self._idle_cv:
                self._hold_active = False
                self._idle_cv.notify_all()
        log_debug("apply: analysis hold released (burst settled)")

    def _release_hold_no_wait(self) -> None:
        """Best-effort release on task exit. Kicks analysis asynchronously so
        shutdown never blocks on a full pass, and always clears the hold flag."""
        if not self._hold_active:
            return
        try:
            self._bv.set_analysis_hold(False)
            self._bv.update_analysis()
        except Exception as e:
            log_error(f"apply: failed to release analysis hold on exit: {e}")
        with self._idle_cv:
            self._hold_active = False
            self._idle_cv.notify_all()

    def _take_next_batch(self) -> ChannelItem | None:
        """Grab one channel item (a page or END_OF_STREAM) and mark in-batch,
        or notify idle waiters that the queue is empty (returns None)."""
        with self._idle_cv:
            try:
                item = self._channel.get_nowait()
            except queue.Empty:
                self._idle_cv.notify_all()
                return None
            self._in_batch = True
            return item
