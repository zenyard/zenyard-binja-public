from __future__ import annotations

# Register pywin32 stand-ins before any `import mcp` (see module docstring).
from . import _pywin32_shim  # noqa: F401
from binaryninja import BinaryViewEvent, BinaryViewEventType  # type: ignore[import]
from .plugin import on_bv_created
from .ui import menu  # noqa: F401
from .pseudo_swift.swift_representation import (
    PseudoSwiftLanguageRepresentationType,
)
from .helpers.log import log_error
from .lifecycle import register_lifecycle_notifications
from .ui.onboarding import schedule_onboarding
from .ui.status_bar.driver import install_status_bar
from .ui.symbol_overlay.driver import install_symbol_overlay

try:
    menu.register_menu()
except Exception as _e:
    log_error(f"failed to register menu: {_e}")

try:
    install_status_bar()
except Exception as _e:
    log_error(f"failed to install status bar: {_e}")

try:
    install_symbol_overlay()
except Exception as _e:
    log_error(f"failed to install symbol overlay: {_e}")

try:
    schedule_onboarding()
except Exception as _e:
    log_error(f"failed to schedule onboarding: {_e}")

try:
    register_lifecycle_notifications()
except Exception as _e:
    log_error(f"failed to register lifecycle notifications: {_e}")

try:
    PseudoSwiftLanguageRepresentationType().register()
except Exception as _e:
    log_error(f"failed to register Pseudo Swift language representation: {_e}")

BinaryViewEvent.register(
    BinaryViewEventType.BinaryViewInitialAnalysisCompletionEvent, on_bv_created
)
