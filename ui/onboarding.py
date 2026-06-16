"""First-run onboarding wizard (Welcome → Terms of Use → API config).

A branded, once-per-machine modal that fires eagerly at Binary Ninja startup
and is also reachable as a backstop from the coordinator's ``ensure_setup``
via the same main-thread show function.
"""

from __future__ import annotations

import time

from binaryninja import (  # type: ignore[import]
    execute_on_main_thread,
    execute_on_main_thread_and_wait,
)
from binaryninjaui import UIContext  # type: ignore[import]
from PySide6.QtWidgets import (  # type: ignore[import]
    QCheckBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from ..ui.eula_text import EULA_TEXT, EULA_VERSION

from ..configuration import (
    get_accepted_eula_version,
    get_api_key,
    save_accepted_eula_version,
    save_settings,
)
from ..helpers.log import log_debug
from .dialogs import _build_card
from .settings_dialog import ApiConfigPanel, show_settings_dialog
from PySide6.QtCore import QTimer  # type: ignore[import]

# Bump this whenever the Terms of Use change; users on an older accepted version
# are re-prompted. Persisted (machine-global) as ``acceptedEulaVersion``.

_BRAND_TITLE = "Zenyard Setup"

_PLUGIN_DESCRIPTION = (
    "Zenyard integrates with Binary Ninja to provide AI-powered analysis of"
    " binaries. Functions, globals, and other objects are analyzed by the"
    " Zenyard backend, which returns inferred names, comments, and type"
    " information directly into your binary."
)

_EULA_INTRO = "Please review and accept the following Terms of Use:"

# The dialog opens at this size; the content area still resizes from here, down
# to whatever the header + footer + _CONTENT_HEIGHT floor allow.
_DEFAULT_WIDTH = 380
_DEFAULT_HEIGHT = 250

_CONTENT_HEIGHT = 130
_READY_POLL_MS = 200
_MAX_READY_POLLS = 150
_WAIT_POLL_S = 0.1


def _page_header(text: str) -> QLabel:
    """A bold sub-header label for a wizard page (under the branded title)."""
    label = QLabel(text)
    font = label.font()
    font.setBold(True)
    font.setPointSize(font.pointSize() + 1)
    label.setFont(font)
    return label


class OnboardingDialog(QDialog):
    """Three-step onboarding wizard. Main-thread only.

    Reuses the shared Zenyard card chrome (``_build_card`` / ``_STYLESHEET``) so
    it matches every other Zenyard dialog. A ``QStackedWidget`` holds the three
    steps; a hand-built Back / Cancel / Next(→Finish) footer drives navigation
    (``ZenyardDialog`` only offers Cancel + OK, which is not enough here).
    """

    def __init__(self) -> None:
        super().__init__()
        layout = _build_card(self, _BRAND_TITLE)

        self._stack = QStackedWidget()
        self._stack.setMinimumHeight(_CONTENT_HEIGHT)
        self._stack.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Expanding
        )
        self._stack.addWidget(self._build_welcome_page())
        self._eula_index = self._stack.addWidget(self._build_eula_page())
        self._stack.addWidget(self._build_config_page())
        layout.addWidget(self._stack, 1)

        layout.addLayout(self._build_footer())

        self._stack.currentChanged.connect(lambda _i: self._update_buttons())
        self._update_buttons()
        self._next.setFocus()

        self.resize(_DEFAULT_WIDTH, _DEFAULT_HEIGHT)

    # ── Pages ───────────────────────────────────────────────────────────────

    def _make_page(
        self, header: str, content: QWidget, *, fill: bool
    ) -> QWidget:
        """A wizard page: step header pinned at the top, then ``content``.

        ``fill=True`` lets the content expand to use the full page height (the
        EULA terms box). ``fill=False`` anchors the content directly under the
        header and lets the slack sit *below* it, so when the dialog is resized
        the empty space grows in the content region — never between the header
        and the content. The header position is identical across every step.
        """
        page = QWidget()
        col = QVBoxLayout(page)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(12)
        if header:
            col.addWidget(_page_header(header))
        if fill:
            col.addWidget(content, 1)
        else:
            col.addWidget(content)
            col.addStretch(1)
        return page

    def _build_welcome_page(self) -> QWidget:
        body = QLabel(_PLUGIN_DESCRIPTION)
        body.setWordWrap(True)
        return self._make_page("Welcome to Zenyard", body, fill=True)

    def _build_eula_page(self) -> QWidget:
        content = QWidget()
        col = QVBoxLayout(content)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(12)
        intro = QLabel(_EULA_INTRO)
        intro.setWordWrap(True)
        col.addWidget(intro)

        terms = QPlainTextEdit()
        terms.setPlainText(EULA_TEXT)
        terms.setReadOnly(True)
        terms.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        col.addWidget(terms, 1)

        self._accept_checkbox = QCheckBox(
            "I have read and accept the Terms of Use"
        )
        col.addWidget(self._accept_checkbox)
        return self._make_page("", content, fill=True)

    def _build_config_page(self) -> QWidget:
        self._config_panel = ApiConfigPanel()
        return self._make_page("Wire It Up", self._config_panel, fill=False)

    # ── Footer ────────────────────────────────────────────────────────────────

    def _build_footer(self) -> QHBoxLayout:
        row = QHBoxLayout()
        self._back = QPushButton("Back")
        self._back.setObjectName("secondary")
        self._back.clicked.connect(self._go_back)
        row.addWidget(self._back)

        row.addStretch(1)

        cancel = QPushButton("Cancel")
        cancel.setObjectName("secondary")
        cancel.clicked.connect(self.reject)
        row.addWidget(cancel)

        self._next = QPushButton("Next")
        self._next.setObjectName("primary")
        self._next.setDefault(True)
        self._next.clicked.connect(self._go_next)
        row.addWidget(self._next)
        return row

    # ── Navigation ──────────────────────────────────────────────────────────

    def _is_last(self) -> bool:
        return self._stack.currentIndex() == self._stack.count() - 1

    def _update_buttons(self) -> None:
        index = self._stack.currentIndex()
        self._back.setHidden(index == 0)
        self._back.setEnabled(index > 0)
        self._next.setText("Finish" if self._is_last() else "Next")
        self._next.setEnabled(True)

    def _go_back(self) -> None:
        self._stack.setCurrentIndex(max(0, self._stack.currentIndex() - 1))

    def _go_next(self) -> None:
        if (
            self._stack.currentIndex() == self._eula_index
            and not self._accept_checkbox.isChecked()
        ):
            QMessageBox.warning(
                self, _BRAND_TITLE, "Please review and accept the Terms of Use."
            )
            return
        if not self._is_last():
            self._stack.setCurrentIndex(self._stack.currentIndex() + 1)
            return
        error = self._config_panel.validate()
        if error is not None:
            QMessageBox.warning(self, _BRAND_TITLE, error)
            return
        save_settings(
            api_url=self._config_panel.api_url(),
            api_key=self._config_panel.api_key(),
        )
        save_accepted_eula_version(EULA_VERSION)
        self.accept()


def is_onboarded() -> bool:
    """True when the current EULA is accepted and an API key is configured."""
    return get_accepted_eula_version() == EULA_VERSION and bool(get_api_key())


def run_onboarding_wizard() -> None:
    """Show the onboarding wizard modally. Main-thread only."""
    OnboardingDialog().exec()


# Main-thread-only re-entrancy guard. A plain bool (not a cross-thread lock) is
# deliberate: the eager path runs the modal on the main thread while the lazy
# backstop waits on the main thread from a coordinator background thread — a
# lock held across the modal would deadlock the two. All showing happens on the
# main thread, so a bool is enough to serialise "show once".
_showing = False


def show_onboarding() -> None:
    """
    Show onboarding / settings if the gate isn't satisfied. Main-thread only.
    """
    global _showing
    if is_onboarded() or _showing:
        return
    _showing = True
    try:
        if get_accepted_eula_version() != EULA_VERSION:
            run_onboarding_wizard()
        elif not get_api_key():
            show_settings_dialog(None)
    finally:
        _showing = False


def ensure_onboarded_blocking() -> bool:
    """
    Ensure onboarding has run, blocking until it settles. Background-thread only.
    """
    if is_onboarded():
        return True
    execute_on_main_thread_and_wait(show_onboarding)
    while _showing:
        time.sleep(_WAIT_POLL_S)
    return is_onboarded()


_poll_timer = None  # QTimer | None — module-held so it isn't garbage collected.
_poll_attempts = 0


def _stop_poll() -> None:
    global _poll_timer
    if _poll_timer is not None:
        _poll_timer.stop()
        _poll_timer = None


def _poll_ready() -> None:
    global _poll_attempts
    # Stop early if the gate got satisfied meanwhile (e.g. the lazy backstop).
    if is_onboarded():
        _stop_poll()
        return
    _poll_attempts += 1
    ctx = UIContext.activeContext()
    if ctx is None or ctx.mainWindow() is None:
        if _poll_attempts >= _MAX_READY_POLLS:
            _stop_poll()  # UI never mounted — leave it to the lazy backstop.
        return  # UI not ready yet — try again next tick.
    # Stop before showing: exec() spins a nested loop, and we want exactly one.
    _stop_poll()
    show_onboarding()


def schedule_onboarding() -> None:
    """
    Show onboarding once the BN UI is ready (eager, once per machine).
    """

    def _setup() -> None:
        global _poll_timer, _poll_attempts
        if _poll_timer is not None or is_onboarded():
            return

        _poll_attempts = 0
        _poll_timer = QTimer()
        _poll_timer.setInterval(_READY_POLL_MS)
        _poll_timer.timeout.connect(_poll_ready)
        _poll_timer.start()
        log_debug("onboarding: waiting for UI to mount")

    execute_on_main_thread(_setup)
