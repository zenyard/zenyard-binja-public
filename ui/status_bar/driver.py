"""Wiring between the run loop and the status-bar widget.

This is the only status-bar module that imports both Qt and ``binaryninjaui``.
It mounts a single widget in the main-window status bar and drives it from two
sources:

- a ~200ms ``QTimer`` that polls the active BinaryView's ``Coordinator``
  (which has no notification surface) for run progress, and
- a background daemon thread that polls the account-usage endpoint on the
  configured period (default 1s) — the network call must never run on the Qt
  main thread, so results are marshalled back via ``execute_on_main_thread``.

Menu clicks arrive as ``actionTriggered`` and are routed to the existing
run-loop entry points.
"""

from __future__ import annotations

import threading

from binaryninja import BinaryView  # type: ignore[import]
from binaryninja import execute_on_main_thread  # type: ignore[import]
from binaryninja.interaction import show_message_box  # type: ignore[import]
from binaryninjaui import UIContext  # type: ignore[import]
from PySide6.QtCore import QTimer  # type: ignore[import]

from ...api_client import make_client
from ...configuration import get_api_key
from ...coordinator.classes import UserAction
from ...coordinator.coordinator import get_coordinator_for_bv
from ...helpers.log import log_debug, log_warn
from ...zenyard_client import (
    ExpiredUsage,
    LimitedUsage,
    UnlimitedUsage,
    UserApi,
)
from .state import UsageInfo, derive_view_state
from .widget import ZenyardStatusWidget

_POLL_MS = 200
POLL_USAGE_S = 3

_controller: "_StatusBarController | None" = None


def install_status_bar() -> None:
    """Mount and start the status-bar widget. Idempotent; main-thread safe.

    Setup is scheduled (non-blocking) onto the Qt main thread so the QWidget /
    QTimer are created there, and so this is safe to call from plugin import
    regardless of which thread that import runs on (no wait → no deadlock).
    """

    def _setup() -> None:
        global _controller
        if _controller is not None:
            return
        _controller = _StatusBarController()
        _controller.start()

    execute_on_main_thread(_setup)


def _map_usage(resp: object) -> UsageInfo:
    """Map a server ``UsageResponse`` to the widget's plain ``UsageInfo``."""

    inst = getattr(resp, "actual_instance", None)
    if isinstance(inst, LimitedUsage):
        # `usage_percentage` is a fraction: 1.0 == at budget, >1.0 == over.
        return UsageInfo("limited", round((inst.usage_percentage or 0) * 100))
    if isinstance(inst, UnlimitedUsage):
        return UsageInfo("unlimited", None)
    if isinstance(inst, ExpiredUsage):
        return UsageInfo("expired", None)
    return UsageInfo("none", None)


class _StatusBarController:
    def __init__(self) -> None:
        self._widget = ZenyardStatusWidget()
        self._widget.actionTriggered.connect(self._on_action)
        self._mounted = False
        # Last *successfully* resolved bv (sticky) used when zenyard popups are blocking UI
        self._current_bv: BinaryView | None = None
        self._last_usage = UsageInfo()

        self._timer = QTimer()
        self._timer.setInterval(_POLL_MS)
        self._timer.timeout.connect(self._tick)

        self._stop = threading.Event()
        self._usage_thread = threading.Thread(
            target=self._usage_loop, daemon=True
        )

    def start(self) -> None:
        self._timer.start()
        self._usage_thread.start()

    # ── Mounting ──────────────────────────────────────────────────────────────

    def _ensure_mounted(self) -> bool:
        if self._mounted:
            return True
        ctx = UIContext.activeContext()
        if ctx is None:
            return False
        main_window = ctx.mainWindow()
        if main_window is None:
            return False
        status_bar = main_window.statusBar()
        if status_bar is None:
            return False
        status_bar.addPermanentWidget(self._widget)
        self._mounted = True
        log_debug("status-bar widget mounted")
        return True

    # ── Coordinator poll tick ───────────────────────────────────────────────

    def _active_bv(self) -> BinaryView | None:
        ctx = UIContext.activeContext()
        if ctx is None:
            return None
        vf = ctx.getCurrentViewFrame()
        if vf is None:
            return None
        return vf.getCurrentBinaryView()

    def _tick(self) -> None:
        try:
            if not self._ensure_mounted():
                return
            # Sticky last-resolved bv
            resolved = self._active_bv()
            if resolved is not None:
                self._current_bv = resolved
            bv = self._current_bv
            coord = get_coordinator_for_bv(bv) if bv is not None else None
            if coord is None:
                self._widget.set_pause_reason(None)
                self._widget.set_state("idle")
                return
            vs = derive_view_state(coord.progress_snapshot(), self._last_usage)
            self._widget.set_pause_reason(vs.pause_reason)
            self._widget.set_state(vs.state)
            if vs.pct is not None:
                self._widget.set_progress(vs.pct)
            self._widget.set_counts(**vs.counts)
        except Exception as e:
            log_warn(f"status-bar tick failed: {e}")

    # ── Usage background poll ─────────────────────────────────────────────────

    def _usage_loop(self) -> None:
        """Poll the usage endpoint off the main thread until stopped."""

        poll_failing = False
        while not self._stop.is_set():
            info: UsageInfo | None = None
            if get_api_key():
                try:
                    resp = UserApi(make_client()).get_user_plans_usage()
                    info = _map_usage(resp)
                    if poll_failing:
                        log_debug("usage poll recovered")
                    poll_failing = False
                except Exception as e:
                    # During an outage this fires every period — warn once on
                    # the ok→fail edge, then stay quiet until recovery.
                    if poll_failing:
                        log_debug(f"usage poll failed: {e}")
                    else:
                        log_warn(f"usage poll failed: {e}")
                    poll_failing = True
            execute_on_main_thread(lambda i=info: self._apply(i))
            if self._stop.wait(POLL_USAGE_S):
                break

    def _apply(
        self,
        usage: UsageInfo | None,
    ) -> None:
        """Push poll-cadence updates to the widget (runs on the main thread).

        ``usage`` is ``None`` when no poll happened this tick (unconfigured);
        the local accent / read-out prefs are applied regardless.
        """

        if usage is not None:
            self._last_usage = usage
            self._widget.set_usage(usage)

    def stop(self) -> None:
        self._stop.set()
        self._timer.stop()

    # ── Actions ───────────────────────────────────────────────────────────────

    def _on_action(self, key: str) -> None:
        bv = self._current_bv
        coord = get_coordinator_for_bv(bv) if bv is not None else None
        if key == "analyze":
            # `unregistered` state: register + analyze. The coordinator's
            # create-revision handler re-runs bring-up to register precisely
            # because binary_id is None. coord may be None if the file closed
            # since the tick.
            if coord is not None:
                coord.post(UserAction("create_revision"))
        elif key == "check_inferences":
            if coord is not None and coord.model.binary_id is not None:
                coord.post(UserAction("check_inferences"))
        elif key in ("pause", "resume"):
            show_message_box("Zenyard", "Pause / resume is not available yet.")
        else:  # usage · view · warn · log · panel — no destination wired yet
            show_message_box("Zenyard", f"'{key}' is not wired up yet.")
