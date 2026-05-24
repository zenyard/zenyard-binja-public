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


class Preferences(BaseModel):
    model_config = ConfigDict(frozen=True)

    auto_apply: bool
    allow_preprocessing: bool
    setup_complete: bool
    show_initial_upload_message: bool


def get_preferences() -> Preferences:
    cfg = _load_config()
    return Preferences(
        auto_apply=cfg.get("autoApply", True),
        allow_preprocessing=cfg.get("allowPreprocessing", False),
        setup_complete=cfg.get("setupComplete", False),
        show_initial_upload_message=cfg.get("showInitialUploadMessage", True),
    )


def save_initial_setup(
    api_key: str,
    auto_apply: bool,
    allow_preprocessing: bool,
) -> None:
    """Save initial setup and preferences."""
    cfg = _load_config()
    cfg.update(
        {
            "apiKey": api_key,
            "autoApply": auto_apply,
            "allowPreprocessing": allow_preprocessing,
            "setupComplete": True,
        }
    )
    _save_config(cfg)


def save_settings(
    api_url: str,
    api_key: str,
    auto_apply: bool,
    allow_preprocessing: bool,
) -> None:
    """Save all settings."""
    cfg = _load_config()
    cfg.update(
        {
            "apiUrl": api_url,
            "apiKey": api_key,
            "autoApply": auto_apply,
            "allowPreprocessing": allow_preprocessing,
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
