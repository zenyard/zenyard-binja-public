from __future__ import annotations

import threading
from urllib.parse import urlparse

from binaryninjaui import UIContext  # type: ignore[import]
from binaryninja import execute_on_main_thread_and_wait  # type: ignore[import]
from PySide6.QtWidgets import (  # type: ignore[import]
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)


from ..configuration import (
    get_api_key,
    get_api_url,
    save_settings,
)
from ..zenyard_client import ApiException


class ApiConfigPanel(QWidget):
    """Reusable API URL + key fields with a Test Connection button.

    Shared by the standalone settings dialog and the onboarding wizard's config
    step (mirrors the Ghidra plugin's ``LicenseConfigPanel``). The key field is
    masked (``Password`` echo) like Ghidra's ``JPasswordField``.

    The Test Connection HTTP call runs on a background thread (L-07) so the
    Binary Ninja / Qt UI thread is never blocked; the button is disabled while
    the test runs and UI updates are dispatched back to the main thread via
    ``execute_on_main_thread_and_wait()``.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)

        form = QFormLayout()

        self._api_url = QLineEdit()
        self._api_url.setText(get_api_url())
        form.addRow("API URL:", self._api_url)

        self._api_key = QLineEdit()
        self._api_key.setEchoMode(QLineEdit.EchoMode.Password)
        self._api_key.setPlaceholderText("zen-...")
        self._api_key.setText(get_api_key())
        form.addRow("API Key:", self._api_key)
        layout.addLayout(form)

        # Test connection button
        self._test_button = QPushButton("Test Connection")
        self._test_button.clicked.connect(self._test_connection)
        layout.addWidget(self._test_button)

    def api_url(self) -> str:
        return self._api_url.text().strip()

    def api_key(self) -> str:
        return self._api_key.text().strip()

    def validate(self) -> str | None:
        """Return an error message if the inputs are unusable, else ``None``."""
        if not self.api_url():
            return "Please enter an API URL."
        if not self.api_key():
            return "Please enter your API key."
        parsed = urlparse(self.api_url())
        if not parsed.scheme or not parsed.netloc:
            return "Please enter a valid API URL (e.g. https://api.zenyard.ai)."
        return None

    def _test_connection(self) -> None:
        """Test the API connection with current form values."""
        api_url = self.api_url()
        api_key = self.api_key()

        if not api_url:
            QMessageBox.warning(
                self, "Test Connection", "Please enter an API URL."
            )
            return

        if not api_key:
            QMessageBox.warning(
                self, "Test Connection", "Please enter an API key."
            )
            return

        # Disable the button immediately so repeated clicks are prevented.
        self._test_button.setEnabled(False)
        self._test_button.setText("Testing…")

        # Capture values for the background thread (avoid reading Qt widgets there).
        captured_url = api_url
        captured_key = api_key

        def run_test() -> None:
            # Determine the message to show: (title, is_warning, message).
            msg_title = "Test Connection"
            is_warning: bool
            msg_body: str
            try:
                from ..zenyard_client import Configuration, ApiClient, UserApi

                cfg = Configuration(
                    host=captured_url.rstrip("/"),
                    api_key={"APIKeyHeader": captured_key},
                )
                import certifi

                cfg.verify_ssl = True
                cfg.ssl_ca_cert = certifi.where()
                UserApi(ApiClient(configuration=cfg)).get_user_config()
                is_warning = False
                msg_body = (
                    "Connection successful! The API is reachable and your"
                    " credentials are valid."
                )
            except ApiException as e:
                is_warning = True
                if e.status in (401, 403):
                    msg_body = (
                        "Authentication failed. Please check your API key."
                    )
                else:
                    msg_body = (
                        f"Server returned status {e.status}."
                        " Please check the API server status."
                    )
            except Exception as e:
                is_warning = True
                msg_body = f"Connection test failed: {e}"

            def update_ui() -> None:
                self._test_button.setEnabled(True)
                self._test_button.setText("Test Connection")
                if is_warning:
                    QMessageBox.warning(self, msg_title, msg_body)
                else:
                    QMessageBox.information(self, msg_title, msg_body)

            execute_on_main_thread_and_wait(update_ui)

        threading.Thread(target=run_test, daemon=True).start()


class ZenyardSettingsDialog(QDialog):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Zenyard Settings")
        self.setMinimumWidth(400)

        layout = QVBoxLayout()
        layout.setSpacing(12)

        self._panel = ApiConfigPanel(self)
        layout.addWidget(self._panel)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Cancel
            | QDialogButtonBox.StandardButton.Save
        )
        buttons.accepted.connect(self._on_save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self.setLayout(layout)

    def _on_save(self) -> None:
        save_settings(
            api_url=self._panel.api_url(),
            api_key=self._panel.api_key(),
        )
        self.accept()


def show_settings_dialog(ctx: UIContext | None) -> None:
    """Show the Zenyard settings dialog. Must be called on the main thread."""
    dialog = ZenyardSettingsDialog()
    dialog.exec()
