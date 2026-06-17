"""Fire-and-forget analytics for the BinaryNinja plugin.

Scoped to a single event for now: the ``"File - Open"`` event (IDA's
``DatabaseOpened`` equivalent), fired once per opened binary from the
coordinator. Best-effort — errors are swallowed and never block user work.

Mirrors ``decompai-ida``'s ``analytics_task.py``: per-open ``session_id``,
OS-enum mapping, ``disableAnalytics`` gate, and an ``"unknown"`` plugin-version
fallback.
"""

from __future__ import annotations

import importlib.metadata
import os
import platform
import re
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

import binaryninja  # type: ignore[import]
from binaryninja import BinaryView  # type: ignore[import]

from ..api_client import make_client
from ..configuration import (
    get_analytics_disabled,
    get_api_key,
    get_or_create_install_id,
)
from ..zenyard_client import (
    AnalyticsApi,
    DatabaseOpenedEvent,
    DecompilerEnum,
    Event,
    ExtraDetails,
    OSEnum,
    TrackEventRequest,
)
from .log import log_debug, log_warn

_OS_MAPPING = {
    "WINDOWS": OSEnum.WINDOWS,
    "DARWIN": OSEnum.MAC_OS,
    "LINUX": OSEnum.LINUX,
}


def _os_enum() -> OSEnum:
    return _OS_MAPPING.get(platform.system().upper(), OSEnum.UNKNOWN)


def _os_version() -> str | None:
    try:
        system = platform.system()
        if system == "Darwin":
            return f"macOS {platform.mac_ver()[0]}"
        if system == "Linux":
            return f"Linux {platform.release()}"
        if system == "Windows":
            return f"Windows {platform.release()}"
        return platform.release()
    except Exception:
        return None


def _plugin_version() -> str:
    try:
        return importlib.metadata.version("zenyard-binja")
    except importlib.metadata.PackageNotFoundError:
        pass
    try:
        pyproject = Path(__file__).resolve().parents[2] / "pyproject.toml"
        match = re.search(
            r'(?m)^\s*version\s*=\s*["\']([^"\']+)["\']',
            pyproject.read_text(),
        )
        if match:
            return match.group(1)
    except Exception:
        pass
    return "unknown"


def _build_environment(session_id: str) -> ExtraDetails:
    return ExtraDetails(
        decompiler=DecompilerEnum.BINARY_NINJA,
        decompiler_version=binaryninja.core_version() or "unknown",
        os_type=_os_enum(),
        os_version=_os_version(),
        plugin_version=_plugin_version(),
        install_id=get_or_create_install_id(),
        session_id=session_id,
    )


def _analytics_timestamp() -> int:
    return int(datetime.now(timezone.utc).timestamp())


def _send_file_open(file_name: str, file_size: int, session_id: str) -> None:
    """Synchronous send — the testable core. Swallows all errors."""
    try:
        api = AnalyticsApi(make_client())
        request = TrackEventRequest(
            event=Event(
                actual_instance=DatabaseOpenedEvent(
                    timestamp=_analytics_timestamp(),
                    file_name=file_name,
                    file_size=file_size,
                )
            ),
            environment=_build_environment(session_id),
        )
        log_debug(f"analytics: File - Open '{file_name}' ({file_size} bytes)")
        api.track_event(request)
    except Exception as e:
        log_warn(f"analytics file-open send failed (ignored): {e}")


def track_file_open(bv: BinaryView) -> None:
    """Fire-and-forget ``"File - Open"`` analytics. Best-effort; never blocks.

    Gathers the ``bv``-derived primitives on the caller's thread, then hands the
    network send to a daemon thread so the coordinator's run loop is never held
    up. No-op when analytics is disabled or no API key is configured.
    """
    if get_analytics_disabled() or not get_api_key():
        return
    filename = bv.file.filename
    file_name = os.path.basename(filename)
    try:
        file_size = os.path.getsize(filename)
    except OSError:
        file_size = 0
    session_id = str(uuid.uuid4())
    threading.Thread(
        target=_send_file_open,
        args=(file_name, file_size, session_id),
        name="zenyard-analytics",
        daemon=True,
    ).start()
