"""MCP server port allocation.

A single process-wide :class:`PortAllocator` hands out loopback ports for the
per-binary :class:`~zenyard_binja.mcp_server.server.BinaryMcpServer` instances,
drawn from the configured range (``mcpPortRange``, default ``17801–17900``).
"""

from __future__ import annotations

import threading


class PortRangeExhausted(RuntimeError):
    pass


class PortAllocator:
    """Thread-safe lowest-free-port allocator over an inclusive range."""

    def __init__(self, port_range: tuple[int, int]) -> None:
        lo, hi = port_range
        self._range = range(lo, hi + 1)
        self._in_use: set[int] = set()
        self._lock = threading.Lock()

    def allocate(self) -> int:
        with self._lock:
            for p in self._range:
                if p not in self._in_use:
                    self._in_use.add(p)
                    return p
            raise PortRangeExhausted(
                f"no free ports in [{self._range.start}, {self._range.stop - 1}]"
            )

    def release(self, port: int) -> None:
        with self._lock:
            self._in_use.discard(port)


_pool: PortAllocator | None = None
_pool_lock = threading.Lock()


def get_port_pool() -> PortAllocator:
    """Lazy process-singleton allocator over the configured MCP port range."""
    global _pool
    with _pool_lock:
        if _pool is None:
            from ..configuration import get_mcp_port_range

            _pool = PortAllocator(get_mcp_port_range())
        return _pool
