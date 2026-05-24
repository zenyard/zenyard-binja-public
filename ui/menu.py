from __future__ import annotations

from binaryninjaui import UIActionContext, UIAction, UIActionHandler, Menu  # type: ignore[import]
from binaryninja.interaction import show_message_box  # type: ignore[import]

from .settings_dialog import show_settings_dialog

SETTING_ACTION = "Zenyard:Settings"

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

    menu = Menu.mainMenu("Zenyard")
    menu.addAction(SETTING_ACTION, "Zenyard", 0)


# --- handlers ---


def _settings_handler(context: UIActionContext) -> None:
    show_settings_dialog(context.context)
