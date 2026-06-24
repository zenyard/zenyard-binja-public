"""Native PySide6 status-bar widget for Binary Ninja — stock-widget rebuild.

A deliberately boring projection of the design: a ``QHBoxLayout`` of stock
widgets, no popups, no context menu.

    [QLabel logo] [QLabel label] [_BounceBar]  —stretch—  [QLabel usage]

Colours come from the host (Binary Ninja) palette — the widget sets almost no
colour itself. The only overrides are the two semantic accents (amber for the
warning label + a high usage read-out, red for usage at/over budget) and the
``Highlight`` role for the actionable ``unregistered`` / ``ready`` labels. The
only motion — and the only hand-painted element — is the progress bar
(``_BounceBar``): while
progress is unknown or still at 0% a single segment bounces back and forth on a
small ``QTimer``, then it fills to the percentage once it climbs above 0
(``paused`` is the exception — a frozen, greyed fill, never a bounce). The bar is
hand-painted because a travelling segment genuinely can't be done with a stock
``QProgressBar`` (which only fills from one edge), and Binary Ninja's Qt style
renders an indeterminate (``setRange(0, 0)``) bar as a *static* full chunk — no
built-in marquee to lean on. One paint routine draws both looks (fill + bounce)
so the bounce→fill hand-off the busy states make has no style seam.

The host drives it through the same contract methods as before
(``set_state`` / ``set_progress`` / ``set_counts`` / ``set_warning_count`` /
``set_pause_reason`` / ``set_usage``) and listens to ``actionTriggered`` — but
that signal now fires only on a left-click in an actionable state
(``unregistered`` / ``ready``). There is no menu.

All copy/label/usage logic still lives in the Qt-free ``state.py`` so it stays
unit-testable; this module only wires those strings into widgets.
"""

from __future__ import annotations

import math
from pathlib import Path

from PySide6.QtCore import (  # type: ignore[import]
    QRectF,
    QSize,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import QPainter, QPalette, QPixmap  # type: ignore[import]
from PySide6.QtWidgets import (  # type: ignore[import]
    QHBoxLayout,
    QLabel,
    QWidget,
)

from . import state as st

_ICON_DIR = Path(__file__).parent / "icons"

# Fixed so the segment never reflows its status-bar neighbours as the label
# length changes. (Just setFixedWidth — still a "high-level" construct.) Sized
# for the longest label ("Click to analyze with Zenyard") plus the icon,
# the usage read-out and margins, so nothing elides.
_WIDTH = 400
_ICON_PX = 14
_BAR_W = 84
_BAR_H = 6

_AMBER = "#e0a93b"
_CRIT = "#e2685f"

# States that show the progress bar. The bar runs one unified behaviour: it
# bounces (a hand-painted segment travelling wall-to-wall while progress is
# unknown or still at 0%
_BAR_STATES = frozenset({"uploading", "queued", "server", "paused"})

# Busy-bounce cadence. `_busy_phase` walks 0.._BUSY_PERIOD; one full out-and-back
# (0→1→0) is _BUSY_PERIOD / _BUSY_ANIM_STEP ticks ≈ 1.3 s at this interval.
_BUSY_ANIM_MS = 33
_BUSY_ANIM_STEP = 5
_BUSY_PERIOD = 200

# Width of the bouncing segment as a fraction of the track.
_SEG_FRAC = 0.32


def _scaled(name: str) -> QPixmap:
    return QPixmap(str(_ICON_DIR / name)).scaled(
        _ICON_PX,
        _ICON_PX,
        Qt.AspectRatioMode.KeepAspectRatio,
        Qt.TransformationMode.SmoothTransformation,
    )


def _busy_pos(phase: int) -> float:
    """Eased 0→1→0 segment position over a 0.._BUSY_PERIOD phase.

    A raised cosine: zero velocity at both walls (the segment eases to a stop
    and reverses) and max speed mid-track — one full out-and-back per period."""

    return 0.5 - 0.5 * math.cos(2 * math.pi * phase / _BUSY_PERIOD)


class _BounceBar(QWidget):
    """The status bar's progress bar, hand-painted (~84×6 px).

    Two looks from one paint routine: a determinate *fill* (left edge → ``pct``)
    or a *bounce* — a single fixed-width segment that travels wall-to-wall and
    back. Both draw the same rounded accent chunk against the same muted track,
    so the bounce→fill hand-off the busy states make (bounce at 0%, fill once %
    climbs) has no style seam. Colours come from the palette: ``Highlight`` for
    the chunk, a dimmed ``WindowText`` for the track; a paused bar uses the
    disabled-group ``Highlight`` so it reads greyed."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedSize(_BAR_W, _BAR_H)
        self._bouncing = False
        self._pos = 0.0  # eased segment position, 0..1 (bounce mode)
        self._pct: float = 0  # fill fraction, 0..100 (determinate mode)
        self._enabled_look = True  # False ⇒ paused/greyed chunk

    def set_fill(self, pct: float, *, enabled: bool = True) -> None:
        self._bouncing = False
        self._pct = max(0, min(100, pct))
        self._enabled_look = enabled
        self.update()

    def set_bounce_pos(self, pos: float) -> None:
        self._bouncing = True
        self._enabled_look = True
        self._pos = pos
        self.update()

    def paintEvent(self, ev: object) -> None:  # noqa: N802 (Qt override)
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.setPen(Qt.PenStyle.NoPen)

        w, h = float(self.width()), float(self.height())
        r = h / 2

        track = self.palette().color(
            QPalette.ColorGroup.Disabled, QPalette.ColorRole.WindowText
        )
        track.setAlpha(60)
        p.setBrush(track)
        p.drawRoundedRect(QRectF(0, 0, w, h), r, r)

        group = (
            QPalette.ColorGroup.Normal
            if self._enabled_look
            else QPalette.ColorGroup.Disabled
        )
        if self._bouncing:
            x = self._pos * (1.0 - _SEG_FRAC) * w
            cw = _SEG_FRAC * w
        else:
            x = 0.0
            cw = (self._pct / 100.0) * w
        if cw > 0:
            p.setBrush(
                self.palette().color(group, QPalette.ColorRole.Highlight)
            )
            p.drawRoundedRect(QRectF(x, 0, cw, h), r, r)


def _is_determinate(state: str, pct: float | None) -> bool:
    """Fill (determinate) vs sweep for a bar state. `paused` is always a frozen
    fill; the active states fill once their percentage climbs above 0 and sweep
    until then — so `extracting` / `applying` (pct always None) always sweep."""

    if state == "paused":
        return True
    return pct is not None and pct > 0


class ZenyardStatusWidget(QWidget):
    """Status-bar segment built entirely from stock widgets."""

    actionTriggered = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)

        self._state = "idle"
        self._pct: float | None = None
        self._counts: dict[str, int] = {}
        self._warning_count = 0
        self._pause_reason: str | None = None
        self._usage = st.UsageInfo()
        self._usage_stale = False

        self._logo = _scaled("zenyard_icon.png")
        self._warn_pm = _scaled("warning_icon.png")

        self._icon = QLabel()
        self._icon.setPixmap(self._logo)

        self._label = QLabel("Zenyard")

        self._bar = _BounceBar()
        self._bar.hide()

        # Drives the busy-state bounce (Binary Ninja's style won't animate an
        # indeterminate bar for us). Runs only while a busy state is showing.
        self._busy_phase = 0
        self._busy_timer = QTimer(self)
        self._busy_timer.setInterval(_BUSY_ANIM_MS)
        self._busy_timer.timeout.connect(self._advance_busy)

        self._usage_lbl = QLabel()
        self._usage_lbl.setObjectName("zyUsage")

        lay = QHBoxLayout(self)
        lay.setContentsMargins(10, 0, 10, 0)
        lay.setSpacing(7)
        lay.addWidget(self._icon)
        lay.addWidget(self._label)
        lay.addWidget(self._bar)
        lay.addStretch(1)
        lay.addWidget(self._usage_lbl)

        self.setFixedWidth(_WIDTH)
        self._render()

    # ── Contract surface ──────────────────────────────────────────────────────

    def set_state(self, state: str) -> None:
        if state in st.STATES and state != self._state:
            self._state = state
            if state not in ("uploading", "paused"):
                self._pct = None
            self._render()

    def set_progress(self, pct: float) -> None:
        # Keep the fraction (don't truncate to int): the `server` read-out shows
        # one decimal — "Analyzing on server 99.9%" — so a near-target revision
        # never reads as a misleading "100%".
        self._pct = max(0.0, min(100.0, pct))
        state = self._effective_state()
        if state == "server":
            # The live % rides the label suffix as well as the bar. Update just
            # the text each poll tick — the colour role is constant mid-state,
            # and a full _render would redo the icon / usage for nothing.
            self._label.setText(
                st.state_label(state, self._pct, self._warning_count)[0]
            )
        if state in _BAR_STATES:
            # Re-apply the whole bar: crossing 0% flips it between its bounce
            # and its determinate fill, not just the fill value.
            self._apply_bar(state)
        self._update_tooltip()

    def set_counts(self, **counts: int) -> None:
        self._counts = dict(counts)
        if self._effective_state() == "queued":
            # The live queue position rides the label — update just the text
            # each tick, mirroring `server`'s % in set_progress.
            self._label.setText(
                st.state_label(
                    "queued",
                    self._pct,
                    self._warning_count,
                    queue_position=counts.get("queue_position"),
                )[0]
            )
        self._update_tooltip()

    def set_warning_count(self, n: int) -> None:
        self._warning_count = n
        if self._state == "warning":
            self._render()

    def set_pause_reason(self, reason: str | None) -> None:
        if reason != self._pause_reason:
            self._pause_reason = reason
            self._render()

    def set_usage(self, usage: st.UsageInfo, *, stale: bool = False) -> None:
        if usage == self._usage and stale == self._usage_stale:
            return
        self._usage = usage
        self._usage_stale = stale
        self._render()  # usage can flip the effective state to paused

    # ── Interaction (no menu) ─────────────────────────────────────────────────

    _CLICK_ACTIONS = {
        "unregistered": "analyze",
        "ready": "check_inferences",
    }

    def mousePressEvent(self, ev: object) -> None:
        key = self._CLICK_ACTIONS.get(self._state)
        actionable = key is not None and not st.quota_blocks(self._usage)
        if ev.button() == Qt.MouseButton.LeftButton and actionable:  # type: ignore[attr-defined]
            self.actionTriggered.emit(key)
            return
        super().mousePressEvent(ev)  # type: ignore[arg-type]

    # ── Rendering ─────────────────────────────────────────────────────────────

    def _effective_state(self) -> str:
        # Quota/expired usage freezes the widget in `paused`, same as before.
        return "paused" if st.quota_blocks(self._usage) else self._state

    def _pause_reason_effective(self) -> str | None:
        if st.quota_blocks(self._usage):
            return "expired" if self._usage.kind == "expired" else "quota"
        return self._pause_reason

    def _apply_label(self, state: str) -> None:
        text, role = st.state_label(
            state,
            self._pct,
            self._warning_count,
            queue_position=self._counts.get("queue_position"),
        )
        self._label.setText(text)
        self._label.setStyleSheet(self._label_qss(role))

    def _render(self) -> None:
        state = self._effective_state()

        self._apply_label(state)

        self._icon.setPixmap(
            self._warn_pm if state == "warning" else self._logo
        )

        self._apply_bar(state)
        self._apply_usage()
        self._update_tooltip()

        # Single-sourced from _CLICK_ACTIONS so the pointing-hand cursor and the
        # click handler can never drift apart.
        actionable = state in self._CLICK_ACTIONS
        self.setCursor(
            Qt.CursorShape.PointingHandCursor
            if actionable
            else Qt.CursorShape.ArrowCursor
        )

    def _label_qss(self, role: str) -> str:
        if role == "accent":
            return f"color: {self.palette().highlight().color().name()};"
        if role == "amber":
            return f"color: {_AMBER};"
        if role == "dim":
            c = self.palette().color(
                QPalette.ColorGroup.Disabled, QPalette.ColorRole.WindowText
            )
            return f"color: {c.name()};"
        return ""  # normal → inherit WindowText

    def _apply_bar(self, state: str) -> None:
        if state not in _BAR_STATES:
            self._busy_timer.stop()
            self._bar.hide()
            return
        if _is_determinate(state, self._pct):
            self._busy_timer.stop()
            # Paused is a frozen, greyed fill (drawn muted, not just disabled).
            self._bar.set_fill(self._pct or 0, enabled=state != "paused")
        else:
            # Self-animated bounce: a timer walks `_busy_phase` and the bar
            # repaints a travelling segment, since the host style won't animate
            # setRange(0, 0). Prime the segment now so bounce mode shows without
            # a stale frame; the `isActive` guard keeps re-entry (every poll
            # tick) from resetting `_busy_phase`.
            self._bar.set_bounce_pos(_busy_pos(self._busy_phase))
            if not self._busy_timer.isActive():
                self._busy_timer.start()
        self._bar.show()

    def _advance_busy(self) -> None:
        self._busy_phase = (self._busy_phase + _BUSY_ANIM_STEP) % _BUSY_PERIOD
        self._bar.set_bounce_pos(_busy_pos(self._busy_phase))

    def _apply_usage(self) -> None:
        text = st.usage_text(self._usage)
        tone = st.usage_tone(self._usage)
        self._usage_lbl.setText(f"usage {text}")
        if tone == "crit":
            color = _CRIT
        elif tone == "amber":
            color = _AMBER
        else:
            color = (
                self.palette()
                .color(
                    QPalette.ColorGroup.Disabled, QPalette.ColorRole.WindowText
                )
                .name()
            )
        # A stale poll dims the read-out via the widget's effect-free opacity
        # hook (no per-paint work; reads cleanly against any palette).
        self._usage_lbl.setStyleSheet(f"#zyUsage {{ color: {color}; }}")
        self._usage_lbl.setProperty("stale", self._usage_stale)

    def _update_tooltip(self) -> None:
        state = self._effective_state()
        title, subtitle = st.tooltip_copy(
            state,
            self._pct,
            self._counts,
            warning_count=self._warning_count,
            pause_reason=self._pause_reason_effective(),
            usage=self._usage,
        )
        extra = ""
        if state == "applying":
            c = self._counts
            extra = (
                "<br><span style='color:#9a9ca1'>download "
                f"{c.get('downloaded', 0)} · apply {c.get('applied', 0)} · "
                f"{c.get('queued', 0)} queued</span>"
            )
        self.setToolTip(f"<b>{title}</b><br>{subtitle}{extra}")

    # ── Sizing ────────────────────────────────────────────────────────────────

    def sizeHint(self) -> QSize:  # noqa: N802 (Qt override)
        return QSize(_WIDTH, 28)
