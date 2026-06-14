"""Make ``mcp`` importable on Windows without the ``pywin32`` distribution.

This module does its work as an import side effect, so it must be imported
before the first ``import mcp`` anywhere in the plugin — hence it is the first
import in the package ``__init__``.

Why it is needed: ``mcp``'s top-level package ``__init__`` eagerly imports its
stdio *client*, which on Windows pulls in ``mcp.os.win32.utilities`` ->
``import pywintypes`` (a module of the ``pywin32`` distribution). Binary Ninja's
bundled Python does not ship ``pywin32``, and wiring it into BN's ``--target``
install is fragile: ``pywintypes`` is a DLL-backed bootstrap that relies on
``pywin32``'s post-install step to place ``pywintypes3XX.dll`` on the loader
path. We only ever run mcp's FastMCP *HTTP* server, so that win32 code path is
dead for us — yet the import still executes at load time and, unhandled, aborts
the whole plugin (``ModuleNotFoundError: No module named 'pywintypes'``).

The fix: register empty stand-in modules for the ``pywin32`` modules mcp imports
so the import chain survives. They are never called — the code that would use
them belongs to the stdio client, which we do not use. If a real ``pywin32`` is
already importable (e.g. installed for another plugin), we leave it untouched.
"""

from __future__ import annotations

import sys
import types

# The pywin32 modules mcp's stdio client imports at module load on Windows.
_PYWIN32_MODULES = ("pywintypes", "win32api", "win32con", "win32job")


def _ensure_pywin32_importable() -> None:
    # ``pywintypes`` is the linchpin (the others depend on it and ship in the
    # same distribution); if it imports, the real pywin32 is present and usable.
    try:
        import pywintypes  # type: ignore  # noqa: F401
    except ImportError:
        for name in _PYWIN32_MODULES:
            sys.modules.setdefault(name, types.ModuleType(name))


if sys.platform == "win32":
    _ensure_pywin32_importable()
