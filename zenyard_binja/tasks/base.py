from __future__ import annotations

import threading
import traceback
from typing import Any
from binaryninja import BackgroundTaskThread  # type: ignore[import]
from binaryninja.log import Logger  # type: ignore[import]
from ..helpers.log import bind_logger, log_error


class TaskCancelled(Exception):
    pass


class CancellableTask(BackgroundTaskThread):
    def __init__(
        self,
        title: str,
        stop: threading.Event,
        logger: Logger | None = None,
    ) -> None:
        super().__init__(title, can_cancel=True)
        self._stop_event = stop
        # This task's per-session logger, supplied by the Coordinator. Bound to
        # this thread once in ``run`` so every leaf ``log_*`` call below it —
        # including the helper modules it calls — routes to the right tab.
        self._logger = logger
        # Per-task cancel, distinct from the shared coordinator ``_stop_event``
        # (which tears down the whole plugin). UI affordances like the extraction
        # progress dialog's Cancel button set this — never ``_stop_event``.
        self._cancel_requested = threading.Event()

    def run(self) -> None:
        bind_logger(self._logger)
        try:
            self._run()
        except TaskCancelled:
            pass
        except Exception as e:
            self._on_error(e)

    def _run(self) -> None:
        raise NotImplementedError

    def _on_error(self, exc: BaseException) -> None:
        # An unexpected (non-cancel) exception escaping ``_run`` would otherwise
        # propagate uncaught out of BN's BackgroundTaskThread and vanish with no
        # plugin-level log. Surface it loud (full traceback) on this task's
        # session logger; the thread then exits cleanly, not silently.
        log_error(
            f"{type(self).__name__}: unexpected error; task ended\n"
            + "".join(traceback.format_exception(exc))
        )

    def request_cancel(self) -> None:
        """Ask this task to stop at its next ``check_cancelled`` boundary."""
        self._cancel_requested.set()

    def is_cancelled(self) -> bool:
        return (
            self._stop_event.is_set()
            or self.cancelled
            or self._cancel_requested.is_set()
        )

    def check_cancelled(self) -> None:
        if self.is_cancelled():
            raise TaskCancelled()

    def sleep_or_cancel(self, seconds: float) -> None:
        if self._stop_event.wait(seconds) or self.cancelled:
            raise TaskCancelled()


_NO_PAYLOAD: Any = object()


class LongLivedTask(CancellableTask):
    """
    Long-lived worker built on :class:`CancellableTask`'s stop/cancel
    semantics. Stays in BN's task panel for its lifetime: idle by default;
    runs one unit of work when ``_submit(payload)`` is called; returns to
    idle. The shared ``_stop_event`` and BN's ``cancelled`` flag terminate
    the loop at the next safe boundary — same semantics as
    :class:`CancellableTask`, just applied across many work units instead of
    once.

    **Newest-payload-wins** (no queue): callers always ``wait_idle()`` before
    submitting (e.g. the Coordinator's drain dance for Download), so the
    "submit while already working" path should not arise in practice. If it
    does, the new payload replaces any prior pending payload, and the
    running ``_do_work`` finishes to completion before the new payload runs
    — there is no per-unit interrupt. Tasks that need cooperative interrupt
    while running (Download's drain) own their own signal (see
    ``DownloadInferencesTask.request_drain``).

    Public surface (used by the Coordinator):
      * ``start()`` / ``join(timeout)`` — lifecycle.
      * ``is_idle()`` — ``True`` iff not in ``_do_work`` and nothing pending.
      * ``wait_idle(timeout)`` — block until idle; returns final idle value.

    Subclass-protected protocol:
      * ``_submit(payload)`` — replace pending payload (queues a new unit of
        work to run after the current one, or immediately if idle).
      * ``check_cancelled()`` / ``sleep_or_cancel()`` — inherited from
        :class:`CancellableTask`.
      * ``_do_work(payload)`` — subclass implementation.
    """

    def __init__(
        self,
        title: str,
        stop: threading.Event,
        logger: Logger | None = None,
    ) -> None:
        super().__init__(title, stop, logger)
        self._cv = threading.Condition()
        self._working = False
        self._pending: Any = _NO_PAYLOAD

    # ── Caller-side protocol ──────────────────────────────────────────────────

    def is_idle(self) -> bool:
        with self._cv:
            return not self._working and self._pending is _NO_PAYLOAD

    def wait_idle(self, timeout: float | None = None) -> bool:
        with self._cv:
            return self._cv.wait_for(
                lambda: not self._working and self._pending is _NO_PAYLOAD,
                timeout=timeout,
            )

    # ── Subclass-protected protocol ───────────────────────────────────────────

    def _submit(self, payload: Any) -> None:
        with self._cv:
            self._pending = payload
            self._cv.notify_all()

    # ── Subclass hook ─────────────────────────────────────────────────────────

    def _do_work(self, payload: Any) -> None:
        raise NotImplementedError

    # ── Loop ──────────────────────────────────────────────────────────────────

    def _run(self) -> None:
        # Periodic wake (0.5s) so the loop re-checks self.is_cancelled() even
        # when _stop_event / BN's `cancelled` flip without notifying this cv.
        while not self.is_cancelled():
            with self._cv:
                self._cv.wait_for(
                    lambda: self.is_cancelled()
                    or self._pending is not _NO_PAYLOAD,
                    timeout=0.5,
                )
                if self.is_cancelled():
                    return
                if self._pending is _NO_PAYLOAD:
                    continue
                payload = self._pending
                self._pending = _NO_PAYLOAD
                self._working = True
            try:
                self._do_work(payload)
            except TaskCancelled:
                pass
            except Exception as e:
                self._on_error(e)
            finally:
                with self._cv:
                    self._working = False
                    self._cv.notify_all()
