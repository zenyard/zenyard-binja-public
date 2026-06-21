"""Per-binary ``zenyard-relay`` subprocess management.

Each open binary gets one :class:`RelayProcess`. It launches the bundled
``zenyard-relay serve`` binary, which opens its own outbound WebSocket to the
Zenyard backend and reverse-proxies incoming MCP requests to the local MCP
server at ``mcp_url``. The relay self-reconnects on network blips, so the
process is stable as long as it is alive; we tear it down by closing stdin
(graceful, cross-platform) with SIGTERM/kill fallbacks.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import typing as ty
from pathlib import Path
from uuid import UUID

from binaryninja.log import Logger  # type: ignore[import]

from ..helpers.log import bind_logger, log_debug, log_error, log_info, log_warn
import zenyard_relay

_RELAY_EXE = "zenyard-relay.exe" if sys.platform == "win32" else "zenyard-relay"

_CREATE_NO_WINDOW = (
    subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
)

# This device's stable relay id, resolved lazily and cached for the session.
_device_relay_id: str | None = None


def get_device_relay_id() -> str | None:
    """This device's stable relay id (``zenyard-relay relay-id``), cached.

    Distinct from the per-binary upstream id passed to ``serve --id``: the
    backend routes the agent to a specific binary via ``<relay_id>:<upstream>``.
    Returns ``None`` if the relay binary is missing or the command fails; the
    agent action is gated on a non-``None`` value, and a failed lookup is
    retried on the next call (rare, user-triggered).
    """
    global _device_relay_id
    if _device_relay_id is not None:
        return _device_relay_id
    try:
        binary = zenyard_relay.binary_path()
    except FileNotFoundError:
        binary = None
    if binary is None:
        return None
    try:
        result = subprocess.run(
            [str(binary), "relay-id"],
            capture_output=True,
            text=True,
            timeout=10,
            creationflags=_CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError) as e:
        log_debug(f"failed to read device relay-id: {e}")
        return None
    _device_relay_id = result.stdout.strip() or None
    return _device_relay_id


class RelayBinaryNotFound(RuntimeError):
    """Raised when the ``zenyard-relay`` executable cannot be located."""


class RelayProcess:
    """Manages a single ``zenyard-relay serve`` subprocess for one binary."""

    def __init__(
        self,
        *,
        relay_id: str,
        mcp_url: str,
        display_name: str,
        description: str,
        api_url: str,
        token: str,
        tags: dict[str, str],
        logger: Logger | None = None,
    ) -> None:
        self._relay_id = relay_id
        self._mcp_url = mcp_url
        self._display_name = display_name
        self._description = description
        self._api_url = api_url
        self._token = token
        self._tags = dict(tags)
        # Bound at the top of the stderr-drain and monitor threads so relay
        # output lands in this binary's tab.
        self._logger = logger
        self._proc: subprocess.Popen[str] | None = None
        self._stopping = False
        self._lock = threading.Lock()

    def _build_argv(self, binary: Path) -> list[str]:
        argv = [
            str(binary),
            "serve",
            "--id",
            self._relay_id,
            "--url",
            self._mcp_url,
            "--display-name",
            self._display_name,
            "--description",
            self._description,
            "--api-url",
            self._api_url,
        ]
        for k, v in self._tags.items():
            argv += ["--tag", f"{k}={v}"]
        return argv

    def start(self) -> None:
        """Locate and spawn the relay. Raises RelayBinaryNotFound if missing."""
        if self._proc is not None:
            return
        binary = zenyard_relay.binary_path()
        if binary is None:
            raise RelayBinaryNotFound(
                f"could not locate {_RELAY_EXE!r}; set relayBinaryPath in "
                "~/.binja/zenyard.json or bundle the binary under "
                "zenyard_binja/bin/<platform>/"
            )
        # Bundled binaries can lose the exec bit (e.g. via a zip); restore it.
        if os.name == "posix" and not os.access(binary, os.X_OK):
            try:
                os.chmod(binary, 0o755)
            except OSError as e:
                log_warn(f"could not chmod relay binary {binary}: {e}")

        env = {**os.environ, "ZENYARD_RELAY_TOKEN": self._token}
        argv = self._build_argv(binary)
        log_info(
            f"starting zenyard-relay id={self._relay_id} url={self._mcp_url}"
        )

        self._proc = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            env=env,
            text=True,
            bufsize=1,
            creationflags=_CREATE_NO_WINDOW,
        )
        threading.Thread(
            target=self._drain_stderr, name="relay-stderr", daemon=True
        ).start()
        threading.Thread(
            target=self._monitor, name="relay-monitor", daemon=True
        ).start()

    def _drain_stderr(self) -> None:
        bind_logger(self._logger)
        # The relay logs only to stderr; an unread pipe fills (~64KB) and blocks
        # the relay, so we must consume it.
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        try:
            for line in proc.stderr:
                log_debug(f"[relay {self._relay_id}] {line.rstrip()}")
        except Exception:
            pass

    def _monitor(self) -> None:
        bind_logger(self._logger)
        proc = self._proc
        if proc is None:
            return
        code = proc.wait()
        if not self._stopping:
            log_error(
                f"zenyard-relay id={self._relay_id} exited unexpectedly "
                f"(code {code}); MCP server still running locally"
            )

    def set_binary_id(self, binary_id: UUID) -> None:
        """Push the backend ``binary_id`` to the running relay as a tag.

        The relay replaces its tag map wholesale on each update, so we re-send
        the complete map (existing tags + binary_id).
        """
        with self._lock:
            self._tags["binary_id"] = str(binary_id)
            self._write_update({"tags": dict(self._tags)})

    def _write_update(self, fields: dict[str, ty.Any]) -> None:
        proc = self._proc
        if proc is None or proc.stdin is None or proc.poll() is not None:
            log_debug(
                f"relay {self._relay_id} not writable; skipping stdin update"
            )
            return
        line = json.dumps({"op": "update", **fields}) + "\n"
        try:
            proc.stdin.write(line)
            proc.stdin.flush()
        except (BrokenPipeError, ValueError, OSError) as e:
            log_debug(f"relay {self._relay_id} stdin write failed: {e}")

    def stop(self, *, timeout: float = 5.0) -> None:
        """Terminate the relay. Idempotent and safe if never started."""
        with self._lock:
            already = self._stopping
            self._stopping = True
            proc = self._proc
        if proc is None:
            return
        if proc.stdin is not None:
            try:
                proc.stdin.close()  # EOF → graceful shutdown
            except Exception:
                pass
        if proc.poll() is None and self._wait(proc, timeout):
            return
        if proc.poll() is None:
            proc.terminate()
            if self._wait(proc, timeout):
                return
        if proc.poll() is None:
            proc.kill()
            if not self._wait(proc, timeout) and not already:
                log_warn(f"relay {self._relay_id} did not exit after kill")

    @staticmethod
    def _wait(proc: subprocess.Popen[str], timeout: float) -> bool:
        try:
            proc.wait(timeout=timeout)
            return True
        except subprocess.TimeoutExpired:
            return False
