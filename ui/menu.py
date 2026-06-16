from __future__ import annotations

from binaryninjaui import UIActionContext, UIAction, UIActionHandler, Menu  # type: ignore[import]

from ..coordinator.coordinator import get_coordinator_for_bv
from ..web_ui import open_agent_for_bv
from .settings_dialog import show_settings_dialog

SETTING_ACTION = "Zenyard Settings"
OPEN_AGENT_ACTION = "Zenyard Agent"
CREATE_REVISION_ACTION = "CreateRevision"
CHECK_INFERENCES_ACTION = "CheckInferences"

_menu_registered: bool = False


def register_menu() -> None:
    global _menu_registered
    if _menu_registered:
        return
    _menu_registered = True

    UIAction.registerAction(SETTING_ACTION)
    UIActionHandler.globalActions().bindAction(
        SETTING_ACTION,
        UIAction(_settings_handler),
    )

    # Opens the backend-hosted agent chat for the active binary; disabled until
    # this binary's MCP relay is up (mirrors the IDA action's enable gating).
    UIAction.registerAction(OPEN_AGENT_ACTION, "Ctrl+Alt+C")
    UIActionHandler.globalActions().bindAction(
        OPEN_AGENT_ACTION,
        UIAction(_agent_handler, _agent_is_valid),
    )

    menu = Menu.mainMenu("Zenyard")
    menu.addAction(SETTING_ACTION, "Zenyard", 0)
    menu.addAction(OPEN_AGENT_ACTION, "Zenyard", 1)


# --- handlers ---


def _settings_handler(context: UIActionContext) -> None:
    show_settings_dialog(context.context)


def _agent_handler(context: UIActionContext) -> None:
    bv = context.binaryView
    if bv is not None:
        open_agent_for_bv(bv)


def _agent_is_valid(context: UIActionContext) -> bool:
    bv = context.binaryView
    if bv is None:
        return False
    coord = get_coordinator_for_bv(bv)
    return coord is not None and coord.agent_upstream_id() is not None
