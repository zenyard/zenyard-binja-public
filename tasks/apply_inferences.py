from __future__ import annotations

import queue
import threading

from binaryninja import (  # type: ignore[import]
    BinaryView,
)
from binaryninja.log import Logger

from ..helpers.apply_inferences import apply_inferences
from ..helpers.inference_types import InferenceItem
from ..helpers.log import log_error, use_logger
from ..helpers.main_thread import run_on_main_thread
from ..model import Model

from .base import CancellableTask, TaskCancelled


_LOOP_RECOVER_DELAY = 2.0


class ApplyInferencesTask(CancellableTask):
    def __init__(
        self,
        *,
        bv: BinaryView,
        model: Model,
        channel: "queue.Queue[tuple[list[InferenceItem], int]]",
        stop: threading.Event,
        logger: Logger | None = None,
    ) -> None:
        super().__init__("", stop=stop, logger=logger)
        self._bv = bv
        self._model = model
        self._channel = channel
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
            page = None
            try:
                page = self._take_next_batch()
                if page is None:
                    self.sleep_or_cancel(0.5)
                    continue
                batch, cursor = page
                if batch:

                    def _apply() -> int:
                        with use_logger(self._logger):
                            return apply_inferences(
                                self._bv, batch, self._model
                            )

                    applied = run_on_main_thread(_apply)
                    self._bv.update_analysis_and_wait()
                    self.applied += applied
                    self._model.add_applied(applied)
                self._model.inference_cursor = cursor
            except TaskCancelled:
                # Cancellation/shutdown still ends the consumer cleanly.
                raise
            except Exception as e:
                log_error(f"apply loop error; continuing: {e}")
                self.sleep_or_cancel(_LOOP_RECOVER_DELAY)
            finally:
                if page is not None:
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
