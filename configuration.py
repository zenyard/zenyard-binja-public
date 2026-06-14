from __future__ import annotations

import json
import os
import typing as ty

from pydantic import BaseModel, ConfigDict


_CONFIG_PATH = os.path.expanduser("~/.binja/zenyard.json")

MAX_OBJECTS_IN_REVISION = 64
MAX_UPLOAD_BYTES = 2 * 1024 * 1024


def _ensure_config_dir() -> None:
    """Ensure ~/.binja/ directory exists."""
    config_dir = os.path.dirname(_CONFIG_PATH)
    os.makedirs(config_dir, exist_ok=True)


def _load_config() -> dict[str, ty.Any]:
    """Load config from JSON file. Return empty dict if missing."""
    try:
        with open(_CONFIG_PATH) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except Exception:
        return {}


def _save_config(config: dict[str, ty.Any]) -> None:
    """Save config to JSON file with owner-only permissions (M-14)."""
    _ensure_config_dir()
    with open(_CONFIG_PATH, "w") as f:
        json.dump(config, f, indent=2)
    # Restrict to owner read/write only so the API key is not world-readable.
    os.chmod(_CONFIG_PATH, 0o600)


def get_api_url() -> str:
    cfg = _load_config()
    return cfg.get("apiUrl", "https://api.zenyard.ai").rstrip("/")


def get_api_key() -> str:
    cfg = _load_config()
    return cfg.get("apiKey", "")


def get_accepted_eula_version() -> int:
    """The EULA version the user last accepted, or 0 if never accepted."""
    cfg = _load_config()
    raw = cfg.get("acceptedEulaVersion", 0)
    return raw if isinstance(raw, int) else 0


def save_accepted_eula_version(version: int) -> None:
    """Record the EULA version the user accepted (machine-global)."""
    cfg = _load_config()
    cfg["acceptedEulaVersion"] = version
    _save_config(cfg)


DEFAULT_MAX_BINARY_SIZE_MB = 10


def get_cached_max_binary_size_mb() -> int | None:
    """The last max-binary-size limit fetched from the server, or None."""
    cfg = _load_config()
    raw = cfg.get("maxBinarySizeMb")
    if isinstance(raw, int) and raw > 0:
        return raw
    return None


def save_max_binary_size_mb(mb: int) -> None:
    """Cache the server-provided max-binary-size limit (machine-global)."""
    cfg = _load_config()
    cfg["maxBinarySizeMb"] = mb
    _save_config(cfg)


_DEFAULT_PORT_RANGE: ty.Final[tuple[int, int]] = (17801, 17900)


def get_mcp_port_range() -> tuple[int, int]:
    cfg = _load_config()
    raw = cfg.get("mcpPortRange")
    if (
        isinstance(raw, list)
        and len(raw) == 2
        and all(isinstance(v, int) for v in raw)
        and raw[0] < raw[1]
        and raw[0] > 0
        and raw[1] < 65536
    ):
        return (int(raw[0]), int(raw[1]))
    return _DEFAULT_PORT_RANGE


class Preferences(BaseModel):
    model_config = ConfigDict(frozen=True)

    allow_preprocessing: bool
    setup_complete: bool
    show_initial_upload_message: bool


def get_preferences() -> Preferences:
    cfg = _load_config()
    return Preferences(
        allow_preprocessing=cfg.get("allowPreprocessing", False),
        setup_complete=cfg.get("setupComplete", False),
        show_initial_upload_message=cfg.get("showInitialUploadMessage", True),
    )


def save_initial_setup(
    api_key: str,
    allow_preprocessing: bool,
) -> None:
    """Save initial setup and preferences."""
    cfg = _load_config()
    cfg.update(
        {
            "apiKey": api_key,
            "allowPreprocessing": allow_preprocessing,
            "setupComplete": True,
        }
    )
    _save_config(cfg)


def save_settings(
    api_url: str,
    api_key: str,
) -> None:
    """Save all settings."""
    cfg = _load_config()
    cfg.update(
        {
            "apiUrl": api_url,
            "apiKey": api_key,
            "setupComplete": True,
        }
    )
    _save_config(cfg)


def save_show_initial_upload_message(show: bool) -> None:
    """Persist the showInitialUploadMessage preference."""
    cfg = _load_config()
    cfg["showInitialUploadMessage"] = show
    _save_config(cfg)


def reset_setup() -> None:
    """Reset setup state so the dialog appears again on next open."""
    cfg = _load_config()
    cfg["setupComplete"] = False
    _save_config(cfg)
