"""Open the backend-hosted Zenyard Agent chat for a binary.

The agent chat UI lives on the Zenyard backend, not in this plugin. Opening it
means minting a one-time web login link (``WebApi.create_link``) whose redirect
targets the chat wired to this binary's MCP relay
(``chats?upstreams=<device_relay_id>:<upstream_id>``), then handing that URL to
the browser. The cloud agent reaches back into this Binary Ninja session over
the already-running ``zenyard-relay`` tunnel + local MCP server.

This is the Binary Ninja counterpart of the IDA plugin's ``web_ui.WebUI``; the
IDA-specific action handler is replaced by a ``UIAction`` in ``ui.menu``.
"""

from __future__ import annotations

import socket
import threading
import webbrowser

from binaryninja import BinaryView  # type: ignore[import]

from .api_client import make_client
from .configuration import get_or_create_install_id
from .coordinator.coordinator import get_coordinator_for_bv
from .helpers.log import log_request_error, log_warn
from .relay import get_device_relay_id
from .zenyard_client import WebApi
from .zenyard_client.models.create_link_request import CreateLinkRequest


def open_agent_for_bv(bv: BinaryView) -> None:
    """Open the Zenyard Agent chat for ``bv`` in the browser.

    Resolves the binary's relay upstream id, then runs the network call on a
    daemon thread so the Qt main thread never blocks. The caller (menu action)
    gates on the relay being up; the ``None`` check here is defensive.
    """
    coord = get_coordinator_for_bv(bv)
    upstream_id = coord.agent_upstream_id() if coord is not None else None
    if upstream_id is None:
        log_warn("Zenyard Agent unavailable: MCP relay not running")
        return
    threading.Thread(
        target=_open_agent,
        args=(upstream_id,),
        name="zenyard-open-agent",
        daemon=True,
    ).start()


def _open_agent(upstream_id: str) -> None:
    relay_id = get_device_relay_id()
    if relay_id is None:
        log_warn("Zenyard Agent unavailable: could not resolve device relay id")
        return
    try:
        client = make_client()
        response = WebApi(client).create_link(
            CreateLinkRequest(
                redirect_to=f"chats?upstreams={relay_id}:{upstream_id}",
                token_id=get_or_create_install_id(),
                description=f"Login from Binary Ninja at {socket.gethostname()}",
            )
        )
        webbrowser.open(response.url, new=2, autoraise=True)
    except Exception as e:
        log_request_error("failed to open Zenyard Agent", e)
