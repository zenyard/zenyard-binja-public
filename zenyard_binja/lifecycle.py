from __future__ import annotations

from binaryninjaui import UIContext, UIContextNotification, FileContext  # type: ignore[import]
from .coordinator.coordinator import shutdown_coordinators_for_file


class _ZenyardContextNotification(UIContextNotification):
    def OnBeforeCloseFile(
        self, context: UIContext, file: FileContext, *args: object
    ) -> bool:
        filename = file.getFilename() or None
        if filename:
            shutdown_coordinators_for_file(str(filename))
        return True


_notification = _ZenyardContextNotification()


def register_lifecycle_notifications() -> None:
    UIContext.registerNotification(_notification)
