import contextlib
import contextvars
import traceback

from collections import Counter
from binaryninja.log import Logger
from ..zenyard_client.exceptions import ApiException

_MAX_SECTION_COUNTS_LOGGED = 60

# Fallback logger for code not bound to a specific session: plugin startup,
# menu registration, and work marshalled onto the shared Qt main thread.
# session_id 0 is BN's default session — the same target everything used
# before per-session loggers existed.
_GLOBAL = Logger(0, "Zenyard")

# The logger for the current thread / async context. A freshly started thread
# begins with this at its default (``_GLOBAL``) — never a copy of the spawning
# thread's value — so each background thread binds its own session logger once
# at entry and a binding in one thread cannot bleed into another. Within an
# asyncio loop the value propagates across ``await``, so one bind at loop entry
# covers every coroutine on that loop.
_current: contextvars.ContextVar[Logger] = contextvars.ContextVar(
    "zenyard_log", default=_GLOBAL
)


def bind_logger(logger: Logger | None) -> None:
    """Bind ``logger`` as the session logger for the current thread/context.

    Set-and-forget: call once at the top of a background thread's entry point
    (a task ``run``, the coordinator ``run``, the MCP server loop, the relay
    drain thread). ``None`` leaves the current binding untouched, so unbound
    threads keep falling back to the global session-0 logger.
    """
    if logger is not None:
        _current.set(logger)


@contextlib.contextmanager
def use_logger(logger: Logger | None):
    """Scoped bind for work marshalled onto a thread shared across sessions.

    Used around ``execute_on_main_thread_and_wait`` callbacks: the work runs on
    the Qt main thread, which serves every tab, so we bind for the callback and
    revert on exit rather than leaving a stale session bound between callbacks.
    ``None`` is a no-op (keeps the global fallback).
    """
    if logger is None:
        yield
        return
    token = _current.set(logger)
    try:
        yield
    finally:
        _current.reset(token)


def log_debug(message: str) -> None:
    _current.get().log_debug(message)


def log_info(message: str) -> None:
    _current.get().log_info(message)


def log_warn(message: str) -> None:
    _current.get().log_warn(message)


def log_error(message: str) -> None:
    _current.get().log_error(message)


def log_api_error(prefix: str, e: ApiException) -> None:
    lines = [
        f"{prefix} (HTTP {e.status} {e.reason})",
        f"  response body: {e.body}",
    ]
    if e.headers:
        lines.append(f"  response headers: {dict(e.headers)}")
    lines.append(traceback.format_exc())
    log_error("\n".join(lines))


def log_request_error(prefix: str, e: Exception) -> None:
    log_error(f"{prefix}: {e}\n{traceback.format_exc()}")


def _format_section_counts(counts: Counter[str]) -> str:
    """Render a section→count breakdown, highest first, capped for log size."""
    top = counts.most_common(_MAX_SECTION_COUNTS_LOGGED)
    rendered = ", ".join(f"{name}={n}" for name, n in top)
    extra = len(counts) - len(top)
    if extra > 0:
        rendered += f", … (+{extra} more sections)"
    return rendered
