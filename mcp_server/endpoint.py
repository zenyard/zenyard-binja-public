"""Per-binary MCP exposure: the one object that owns both halves.

``BinaryMcpEndpoint`` composes the local :class:`BinaryMcpServer` (an
in-process Hypercorn thread bound to one ``BinaryView``) and, when an API key
is configured, the :class:`RelayProcess` subprocess that tunnels it to the
Zenyard backend. It holds the allocated port and enforces the lifecycle
invariants in one place:

- port allocated ⟺ server exists,
- a relay implies a server (never the reverse),
- teardown order is always relay → server → release port.

The server always comes up; the relay is optional and best-effort — a relay
failure leaves the server reachable locally. ``start()`` / ``stop()`` are
idempotent. The two halves stay separate classes by design: they manage
different resources (a thread vs. an OS subprocess) with different teardown and
failure semantics, and the server must run with no relay at all.
"""

from __future__ import annotations

import hashlib
import os
import typing as ty
from uuid import UUID

from binaryninja import BinaryView  # type: ignore[import]
from binaryninja.log import Logger  # type: ignore[import]

from ..configuration import get_api_key, get_api_url
from ..helpers.log import log_debug, log_error, log_info, log_request_error
from ..helpers.misc import canonical_db_name
from ..relay import RelayBinaryNotFound, RelayProcess
from .ports import PortAllocator
from .server import BinaryMcpServer


def _relay_id_for(bv: ty.Any) -> str:
    """Stable, immediately-available routing id derived from the file path."""
    digest = hashlib.sha256(
        canonical_db_name(bv.file.filename).encode("utf-8")
    ).hexdigest()
    return f"binja-{digest}"


class BinaryMcpEndpoint:
    """Owns the MCP server + optional relay subprocess for one ``BinaryView``."""

    def __init__(
        self,
        bv: BinaryView,
        *,
        ports: PortAllocator,
        server_factory: ty.Callable[..., ty.Any] = BinaryMcpServer,
        relay_factory: ty.Callable[..., ty.Any] = RelayProcess,
        logger: Logger | None = None,
    ) -> None:
        self._bv = bv
        self._ports = ports
        self._server_factory = server_factory
        self._relay_factory = relay_factory
        # Per-session logger, forwarded to the server thread and relay drain
        # thread so their output routes to this binary's tab.
        self._logger = logger
        self._server: BinaryMcpServer | None = None
        self._relay: RelayProcess | None = None
        self._port: int | None = None

    @property
    def upstream_id(self) -> str:
        """The per-binary relay routing id (the relay's ``serve --id``).

        Deterministic from the file path, so it's available before the relay
        starts; the backend addresses this binary via
        ``<device_relay_id>:<upstream_id>``.
        """
        return _relay_id_for(self._bv)

    @property
    def relay_running(self) -> bool:
        """True once the relay subprocess has been spawned for this binary."""
        return self._relay is not None

    def start(self, *, binary_id: UUID | None) -> None:
        """Bring up the server and (if an API key is set) the relay.

        NOT gated on ``binary_id``: both come up as soon as the binary is open.
        Idempotent — a no-op once the server is running. An MCP server start
        failure releases the port and propagates; a relay failure is swallowed,
        leaving the server reachable locally.
        """
        if self._server is not None:
            return
        port = self._ports.allocate()
        try:
            server = self._server_factory(self._bv, port, logger=self._logger)
            server.start()
        except BaseException:
            self._ports.release(port)
            raise
        self._server = server
        self._port = port
        self._maybe_start_relay(binary_id)

    def set_binary_id(self, binary_id: UUID) -> None:
        """Idempotent: push the backend ``binary_id`` tag to a running relay."""
        if self._relay is None:
            return
        try:
            self._relay.set_binary_id(binary_id)
        except Exception as e:
            log_debug(f"failed to push binary_id to relay: {e}")

    def stop(self) -> None:
        """Idempotent teardown in order: relay → server → release port."""
        if self._relay is not None:
            try:
                self._relay.stop()
            except Exception:
                log_debug("error stopping zenyard-relay")
            self._relay = None
        if self._server is not None:
            self._server.stop()
            self._server = None
        if self._port is not None:
            self._ports.release(self._port)
            self._port = None

    def _maybe_start_relay(self, binary_id: UUID | None) -> None:
        # The relay uses the API key as its auth token; without one it would
        # just fail to connect, so we skip it and run the server locally.
        token = get_api_key()
        if not token:
            log_info("no API key; MCP server running locally without relay")
            return
        assert self._server is not None
        tags = {"decompiler": "binja"}
        if binary_id is not None:
            tags["binary_id"] = str(binary_id)
        name = os.path.basename(self._bv.file.filename)
        proc = self._relay_factory(
            relay_id=self.upstream_id,
            mcp_url=self._server.url + "/mcp",
            display_name=name,
            description=f"Binary Ninja decompiler MCP for {name}",
            api_url=get_api_url(),
            token=token,
            tags=tags,
            logger=self._logger,
        )
        try:
            proc.start()
        except RelayBinaryNotFound as e:
            log_error(f"zenyard-relay not started: {e}")
            return
        except Exception as e:
            log_request_error("failed to start zenyard-relay", e)
            return
        self._relay = proc
