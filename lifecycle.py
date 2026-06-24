from __future__ import annotations

from binaryninjaui import UIContext, UIContextNotification, FileContext  # type: ignore[import]
from .coordinator.coordinator import shutdown_coordinator_on_close


class _ZenyardContextNotification(UIContextNotification):
    def OnBeforeCloseFile(
        self,
        context: UIContext,
        file: FileContext,
        frame: object = None,
        *args: object,
    ) -> bool:
        # Tear down the coordinator for the closing file. The registry keys by a
        # per-view session key, so prefer resolving the closing view from the
        # frame; fall back to the filename when no frame is available.
        bv = None
        if frame is not None:
            try:
                bv = frame.getCurrentBinaryView()  # type: ignore[attr-defined]
            except Exception:
                bv = None
        filename = file.getFilename() or None
        shutdown_coordinator_on_close(bv, str(filename) if filename else None)
        return True


_notification = _ZenyardContextNotification()


def register_lifecycle_notifications() -> None:
    UIContext.registerNotification(_notification)
