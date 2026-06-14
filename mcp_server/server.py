from __future__ import annotations

import asyncio
import errno
import threading
import typing as ty

from binaryninja import execute_on_main_thread_and_wait  # type: ignore[import-not-found]
from binaryninja.log import Logger
from hypercorn.asyncio import serve  # type: ignore[import-untyped]
from hypercorn.config import Config as HypercornConfig  # type: ignore[import-untyped]
from mcp.server.fastmcp import FastMCP  # type: ignore[import-untyped]

from ..helpers.log import bind_logger, log_debug, log_warn

# Bind/startup OSErrors are fatal — re-serving can never fix them, so we let
# them propagate instead of spinning the restart loop.
_FATAL_OS_ERRNOS = frozenset(
    {errno.EADDRINUSE, errno.EACCES, errno.EADDRNOTAVAIL}
)


def _is_recoverable(exc: BaseException) -> bool:
    """True if re-serving might recover (transient transport faults).

    Recoverable: cancellation, client disconnects, and ASGI/connection-reset
    errors that kill one ``serve()`` but not the bind. Fatal: bind-time OS
    errors (port in use, permission) and anything unrecognized — surfacing
    those is safer than an infinite restart loop. Exception groups are
    recoverable only if every leaf is.
    """
    if isinstance(exc, asyncio.CancelledError):
        return True
    nested = getattr(exc, "exceptions", None)
    if nested is not None:  # BaseExceptionGroup / anyio TaskGroup
        return bool(nested) and all(_is_recoverable(e) for e in nested)
    if isinstance(exc, OSError):
        return exc.errno not in _FATAL_OS_ERRNOS
    msg = str(exc).lower()
    markers = (
        "connection",
        "closed",
        "reset",
        "broken pipe",
        "asgi",
        "unexpected message",
        "cancelled",
    )
    return any(m in msg for m in markers)


def bn_write(fn: ty.Callable[[], ty.Any]) -> ty.Any:
    """Run a BV mutation on BN's main thread; return its result or re-raise."""
    box: list = []
    err: list = []

    def runner() -> None:
        try:
            box.append(fn())
        except BaseException as e:
            err.append(e)

    execute_on_main_thread_and_wait(runner)
    if err:
        raise err[0]
    return box[0]


from .tools import register_tools  # noqa: E402


class BinaryMcpServer:
    """A per-Binary FastMCP server listening on `127.0.0.1:{port}`.

    Construction is cheap; the server is reachable only after `start()` returns.
    """

    def __init__(
        self,
        bv: ty.Any,
        port: int,
        *,
        host: str = "127.0.0.1",
        logger: Logger | None = None,
    ) -> None:
        self._bv = bv
        self._host = host
        self._port = port
        self._logger = logger
        self._mcp = FastMCP(
            name=f"zenyard-{port}",
            stateless_http=True,
            json_response=True,
        )
        register_tools(self._mcp, bv)
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._shutdown_event: asyncio.Event | None = None
        self._ready = threading.Event()
        self._start_error: BaseException | None = None

    @property
    def url(self) -> str:
        return f"http://{self._host}:{self._port}"

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("BinaryMcpServer already started")
        self._thread = threading.Thread(
            target=self._run, name=f"zenyard-mcp-{self._port}", daemon=True
        )
        self._thread.start()
        if not self._ready.wait(timeout=10.0):
            raise TimeoutError("BinaryMcpServer failed to come up within 10s")
        if self._start_error is not None:
            raise self._start_error

    def stop(self, *, timeout: float = 10.0) -> None:
        if self._thread is None:
            return
        loop = self._loop
        evt = self._shutdown_event
        if loop is not None and evt is not None and loop.is_running():
            loop.call_soon_threadsafe(evt.set)
        self._thread.join(timeout=timeout)
        self._thread = None
        self._loop = None
        self._shutdown_event = None

    def _run(self) -> None:
        # Bound once here for the loop thread; propagates across awaits so all
        # coroutines on this loop (serve, tool handlers) log to this tab.
        bind_logger(self._logger)
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self._loop = loop
            self._shutdown_event = asyncio.Event()
            cfg = HypercornConfig()
            cfg.bind = [f"{self._host}:{self._port}"]
            cfg.accesslog = None
            cfg.errorlog = None
            app = self._mcp.streamable_http_app()
            self._ready.set()
            loop.run_until_complete(self._serve_with_restart(app, cfg))
        except BaseException as e:  # surface to start()
            self._start_error = e
            self._ready.set()
        finally:
            if self._loop is not None and not self._loop.is_closed():
                self._loop.close()

    async def _serve_with_restart(self, app: ty.Any, cfg: ty.Any) -> None:
        """Serve, re-entering ``serve()`` on transient transport faults.

        A single failed request or client disconnect can bubble out of
        ``serve()`` and would otherwise kill this thread, leaving the binary's
        MCP server dead until the binary is reopened. We re-serve on recoverable
        faults (brief pause to avoid a tight loop) and stop only on shutdown or
        a fatal error (e.g. bind failure), which propagates to ``_run`` and thus
        to ``start()``.
        """
        assert self._shutdown_event is not None
        while not self._shutdown_event.is_set():
            try:
                await serve(
                    app, cfg, shutdown_trigger=self._shutdown_event.wait
                )
                return  # clean shutdown via the trigger
            except BaseException as e:
                if self._shutdown_event.is_set():
                    return
                if not _is_recoverable(e):
                    raise
                # Re-serve on the same bind. If the socket was not released the
                # retry hits EADDRINUSE, which _is_recoverable treats as fatal —
                # so this recovers transient request/connection faults, not every
                # conceivable serve() failure.
                log_warn(
                    f"zenyard-mcp:{self._port} serve() hit a recoverable error "
                    f"({e!r}); restarting server loop"
                )
                await asyncio.sleep(0.1)
        log_debug(f"zenyard-mcp:{self._port} server loop exited (shutdown)")
