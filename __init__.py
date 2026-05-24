from __future__ import annotations

from binaryninja import BinaryViewEvent, BinaryViewEventType
from binaryninja import log_error
from .ui import menu  # noqa: F401

try:
    menu.register_menu()
except Exception as _e:
    log_error(f"failed to install status bar: {_e}")
