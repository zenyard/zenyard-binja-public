from __future__ import annotations

import typing as ty
import webbrowser
from pathlib import Path
from urllib.parse import quote

from PySide6.QtCore import Qt, QTimer  # type: ignore[import]
from PySide6.QtGui import QFontMetrics, QPixmap  # type: ignore[import]
from PySide6.QtWidgets import (  # type: ignore[import]
    QApplication,
    QCheckBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)


# Reuse the status-bar logo so every dialog keeps the Zenyard branding.
_ICON_PATH = Path(__file__).parent / "status_bar" / "icons" / "zenyard_icon.png"

_INTRO_MESSAGE = (
    "Looks like it's your first time opening this file — "
    "Zenyard can analyze it now to save you time and effort."
)

_BINARY_INSTRUCTIONS_PROMPT = (
    "To improve the analysis, add any details you know "
    "(source, purpose, structure, etc.) or just click OK to continue.\n\n"
    "Only share what you're sure about."
)

_UPLOAD_COMPLETE_TITLE = "Zenyard Is Now Analyzing in the Background"
_UPLOAD_COMPLETE_BODY = (
    "The initial processing is complete. Zenyard will continue analyzing"
    " remotely in the background."
)

# Same contact details and copy as the IDA plugin's size-limit warning.
_CONTACT_EMAIL = "access@zenyard.ai"
_CONTACT_SUBJECT = "Zenyard trial ended - set up continued access"
_CONTACT_BODY = """Hi Zenyard team,
My Zenyard trial ended and I'd like to continue.

Organization: [COMPANY NAME]
Team size: [# USERS]
Deployment: Cloud / Private cloud / On-prem / Not sure
Constraints: [air-gapped, compliance, sensitive binaries, etc.]

Thanks,
[NAME]
"""

_AUTH_ERROR_TITLE = "Zenyard Authentication Failed"
_AUTH_ERROR_BODY = (
    "Zenyard couldn't authenticate with the server — your API key is"
    " missing, invalid, or expired. Analysis for this binary is disabled"
    " until it's fixed.\n\nUpdate your API key in Zenyard settings, then"
    " reopen the binary to retry."
)

_SIZE_LIMIT_TITLE = "Binary Size Exceeded"
_SIZE_LIMIT_BODY = (
    "Oops, this binary is over the {limit} MB limit for the Zenyard free"
    " trial. The full version supports larger files with no size limit.\n\n"
    f"Need larger-file support? Contact us: {_CONTACT_EMAIL}"
)

_EXTRACTION_TITLE = "Preparing Your Data"
# Mirrors the IDA plugin's wait-box copy, minus the inline percent — the bar
# carries the number here.
_EXTRACTION_BODY = (
    "Zenyard is preparing your data for analysis — this may take a little"
    " while.\n\nOnce it's done, Zenyard will keep working its magic in the"
    " background and let you know as soon as your results are ready."
)

# How often the progress dialog polls its counter source (ms). Same shape as
# the status-bar driver: the dialog reads ints the extraction loop writes on a
# background thread; int read/write is atomic under the GIL, so no lock.
_PROGRESS_POLL_MS = 100

# Every colour is a ``palette(...)`` reference — the same convention Binary
# Ninja's own stylesheets use — so the dialog tracks whatever theme (dark or
# light) is active. Only the *shape* is pinned, which is what makes the dialog
# look identical across macOS / Windows / Linux instead of deferring to each
# platform's native QStyle.
_STYLESHEET = """
#zenyardCard {
    background-color: palette(window);
    border: 1px solid palette(mid);
    border-radius: 16px;
}
QPushButton#primary {
    background-color: palette(highlight);
    color: palette(highlighted-text);
    border: none;
    border-radius: 8px;
    padding: 6px 18px;
    font-weight: 600;
}
QPushButton#secondary {
    background-color: palette(button);
    color: palette(button-text);
    border: 1px solid palette(mid);
    border-radius: 8px;
    padding: 6px 18px;
}
QPlainTextEdit {
    background-color: palette(base);
    color: palette(text);
    border: 1px solid palette(mid);
    border-radius: 8px;
    padding: 6px;
}
QProgressBar {
    background-color: palette(base);
    color: palette(text);
    border: 1px solid palette(mid);
    border-radius: 6px;
    min-height: 5px;
    text-align: center;
}
QProgressBar::chunk {
    background-color: palette(highlight);
    border-radius: 6px;
}
"""


def _zenyard_logo(target: int, widget: QWidget) -> QPixmap | None:
    """The Zenyard logo scaled to ``target`` logical points.

    DPI-corrected for ``widget``'s screen (2.0 on Retina, 1.0 otherwise) so it
    stays crisp. Returns None if the icon file is missing.
    """
    pixmap = QPixmap(str(_ICON_PATH))
    if pixmap.isNull():
        return None
    dpr = widget.devicePixelRatio()
    scaled = pixmap.scaledToWidth(
        round(target * dpr), Qt.TransformationMode.SmoothTransformation
    )
    scaled.setDevicePixelRatio(dpr)
    return scaled


def _build_card(dialog: QDialog, title: str) -> QVBoxLayout:
    """Apply the shared Zenyard chrome to ``dialog`` and return its card layout.

    Pins the frameless, translucent, palette-styled card with the logo+title
    header (the part every Zenyard dialog shares) and hands back the card's
    inner ``QVBoxLayout`` so the caller can append its own body, content and
    buttons. Keeping only the *shape* fixed is what makes the dialogs look
    identical across macOS / Windows / Linux instead of deferring to each
    platform's native QStyle.
    """
    dialog.setWindowFlags(
        Qt.WindowType.FramelessWindowHint | Qt.WindowType.Dialog
    )
    # Translucent window so the rounded card's corners show through.
    dialog.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
    dialog.setMinimumWidth(480)
    dialog.setStyleSheet(_STYLESHEET)

    outer = QVBoxLayout(dialog)
    outer.setContentsMargins(0, 0, 0, 0)

    card = QWidget()
    card.setObjectName("zenyardCard")
    outer.addWidget(card)

    layout = QVBoxLayout(card)
    layout.setContentsMargins(24, 24, 24, 24)
    layout.setSpacing(12)

    title_label = QLabel(title)
    title_font = title_label.font()
    title_font.setBold(True)
    title_font.setPointSize(title_font.pointSize() + 3)
    title_label.setFont(title_font)

    # Logo and title share one line, the logo sized to the title's line height
    # so the two read as the same size.
    header = QHBoxLayout()
    header.setSpacing(10)
    logo = QLabel()
    pixmap = _zenyard_logo(QFontMetrics(title_font).height(), dialog)
    if pixmap is not None:
        logo.setPixmap(pixmap)
    header.addWidget(logo)
    header.addWidget(title_label)
    header.addStretch(1)
    layout.addLayout(header)

    # Extra breathing room between the title and the body.
    layout.addSpacing(8)
    return layout


class ZenyardDialog(QDialog):
    """A frameless, palette-styled Zenyard prompt. Main-thread only.

    All three Zenyard prompts route through this so they look identical on
    every platform. It is a custom QDialog rather than a QMessageBox because
    the platform decides a QMessageBox's appearance (and a QMessageBox also
    drops injected input widgets when Binary Ninja re-lays it out on show).

    There is no drop shadow: Binary Ninja's PySide6 ships without the
    QGraphicsEffect framework. The hairline card border stands in for it,
    delineating the frameless card from the disassembly behind it.
    """

    def __init__(
        self,
        title: str,
        body: str,
        *,
        content: QWidget | None = None,
        checkbox_label: str | None = None,
        checkbox_checked: bool = False,
        show_cancel: bool = True,
        ok_label: str = "OK",
        cancel_label: str = "Cancel",
    ) -> None:
        super().__init__()
        layout = _build_card(self, title)

        body_label = QLabel(body)
        body_label.setWordWrap(True)
        layout.addWidget(body_label)

        if content is not None:
            layout.addWidget(content)

        self._checkbox: QCheckBox | None = None
        if checkbox_label is not None:
            self._checkbox = QCheckBox(checkbox_label)
            self._checkbox.setChecked(checkbox_checked)
            layout.addWidget(self._checkbox)

        row = QHBoxLayout()
        row.addStretch(1)
        if show_cancel:
            cancel = QPushButton(cancel_label)
            cancel.setObjectName("secondary")
            cancel.clicked.connect(self.reject)
            row.addWidget(cancel)
        ok = QPushButton(ok_label)
        ok.setObjectName("primary")
        ok.setDefault(True)
        ok.clicked.connect(self.accept)
        row.addWidget(ok)
        layout.addLayout(row)

        # Type immediately when there's an input; otherwise rest focus on OK
        # so the checkbox doesn't grab the focus ring.
        (content if content is not None else ok).setFocus()

    @property
    def checkbox_checked(self) -> bool:
        return self._checkbox is not None and self._checkbox.isChecked()


class ZenyardProgressDialog(QDialog):
    """Application-modal extraction-progress dialog. Main-thread only.

    Long-lived, unlike the one-shot ``prompt_*`` dialogs: the caller ``show()``s
    it, lets it poll, then ``close()``s it. It holds no task reference — only a
    ``get_progress`` source and an ``on_cancel`` callback — so it can be driven
    entirely from the main thread while extraction runs on a background thread.
    Cancelling just invokes ``on_cancel``; the task stops at its next
    ``check_cancelled`` boundary.

    Shown with ``show()`` (not ``exec()``, which would block the caller) plus
    application modality, so it blocks interaction with Binary Ninja — both to
    mirror the IDA plugin and to keep the user from mutating the binary while
    it's being read on the background thread.
    """

    def __init__(
        self,
        get_progress: ty.Callable[[], tuple[int, int]],
        on_cancel: ty.Callable[[], None],
    ) -> None:
        super().__init__()
        self._get_progress = get_progress
        self._on_cancel = on_cancel
        self._centered = False
        self.setWindowModality(Qt.WindowModality.ApplicationModal)

        layout = _build_card(self, _EXTRACTION_TITLE)

        body_label = QLabel(_EXTRACTION_BODY)
        body_label.setWordWrap(True)
        layout.addWidget(body_label)

        self._bar = QProgressBar()

        self._bar.setFixedHeight(12)
        self._bar.setRange(0, 100)
        self._bar.setValue(0)
        self._bar.setTextVisible(True)
        self._bar.setFormat("%p%")
        layout.addWidget(self._bar)

        row = QHBoxLayout()
        row.addStretch(1)
        cancel = QPushButton("Cancel")
        cancel.setObjectName("secondary")
        cancel.clicked.connect(self._handle_cancel)
        row.addWidget(cancel)
        layout.addLayout(row)

        self._timer = QTimer(self)
        self._timer.setInterval(_PROGRESS_POLL_MS)
        self._timer.timeout.connect(self._poll)
        self._timer.start()

    def _poll(self) -> None:
        extracted, total = self._get_progress()
        pct = round(100 * extracted / total) if total > 0 else 0
        self._bar.setValue(max(0, min(100, pct)))

    def _handle_cancel(self) -> None:
        self._on_cancel()
        self.close()

    def showEvent(self, event: object) -> None:  # noqa: N802 (Qt override)
        super().showEvent(event)  # type: ignore[arg-type]
        # Centre once on first show — a frameless ``show()`` dialog (unlike an
        # ``exec()`` one) isn't placed by Qt, so it would otherwise land at the
        # top-left corner.
        if not self._centered:
            self._centered = True
            screen = self.screen() or QApplication.primaryScreen()
            if screen is not None:
                geo = self.frameGeometry()
                geo.moveCenter(screen.availableGeometry().center())
                self.move(geo.topLeft())

    def closeEvent(self, event: object) -> None:  # noqa: N802 (Qt override)
        self._timer.stop()
        super().closeEvent(event)  # type: ignore[arg-type]


def prompt_intro_message() -> bool | None:
    """First-analysis prompt. Returns the auto-apply choice, or None if the
    user cancels (mirroring ``prompt_binary_instructions``). The caller
    persists the choice per-binary. Must be called on the main thread.
    """
    dialog = ZenyardDialog(
        "Run Zenyard Analysis",
        _INTRO_MESSAGE,
        checkbox_label="Auto apply results when ready",
        checkbox_checked=True,
    )
    if dialog.exec() != QDialog.DialogCode.Accepted:
        return None
    return dialog.checkbox_checked


def prompt_binary_instructions() -> str | None:
    """Optional binary-instructions prompt. Must be called on the main thread.

    Returns the entered text (possibly empty) when accepted, or ``None`` when
    the user cancels — so the caller can distinguish "no instructions given"
    from "cancel the analysis".
    """
    text = QPlainTextEdit()
    text.setMinimumHeight(140)
    text.setPlaceholderText(
        "Optional — e.g. source language, compiler, what the binary does…"
    )
    dialog = ZenyardDialog(
        "Zenyard Analysis", _BINARY_INSTRUCTIONS_PROMPT, content=text
    )
    if dialog.exec() != QDialog.DialogCode.Accepted:
        return None
    return text.toPlainText().strip()


def show_size_limit_exceeded(max_size_mb: int) -> None:
    """Hard-block notice for a Binary over the size limit.

    "Contact Us" opens the default mail client pre-filled with the upgrade
    request (same mechanism as the IDA plugin). Must be called on the main
    thread.
    """
    dialog = ZenyardDialog(
        _SIZE_LIMIT_TITLE,
        _SIZE_LIMIT_BODY.format(limit=max_size_mb),
        ok_label="Contact Us",
        cancel_label="Close",
    )
    if dialog.exec() == QDialog.DialogCode.Accepted:
        webbrowser.open(
            f"mailto:{_CONTACT_EMAIL}"
            f"?subject={quote(_CONTACT_SUBJECT)}&body={quote(_CONTACT_BODY)}"
        )


def show_auth_error() -> None:
    """One-shot notice shown when the server rejects our credentials (401/403).

    Analysis is disabled for the binary until the key is fixed — the
    Coordinator's auth-blocked posture, mirroring the IDA plugin's disabled
    state. Must be called on the main thread.
    """
    ZenyardDialog(
        _AUTH_ERROR_TITLE,
        _AUTH_ERROR_BODY,
        show_cancel=False,
    ).exec()


def show_upload_complete() -> bool:
    """Show the "analyzing in the background" dialog.

    Returns True if the user ticked "Don't show this again" (the caller then
    persists that choice to the bndb). Must be called on the main thread.
    """
    dialog = ZenyardDialog(
        _UPLOAD_COMPLETE_TITLE,
        _UPLOAD_COMPLETE_BODY,
        checkbox_label="Don't show this again",
        show_cancel=False,
    )
    dialog.exec()
    return dialog.checkbox_checked
