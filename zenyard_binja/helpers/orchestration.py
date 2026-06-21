import typing as ty
from collections import deque

from ..configuration import (
    MAX_OBJECTS_IN_REVISION,
    MAX_UPLOAD_BYTES,
)

_T = ty.TypeVar("_T")


def topo_sort_addrs(graph: ty.Mapping[int, ty.Iterable[int]]) -> list[int]:
    """Bottom-up topological order (callees before callers) of addresses.

    ``graph`` maps each in-scope function address to its callee addresses
    """
    addr_set = set(graph)
    out_degree: dict[int, int] = {a: 0 for a in graph}
    callers: dict[int, list[int]] = {a: [] for a in graph}
    for addr, callees in graph.items():
        for callee in callees:
            if callee in addr_set:
                out_degree[addr] += 1
                callers[callee].append(addr)
    q: deque[int] = deque(a for a, deg in out_degree.items() if deg == 0)
    result: list[int] = []
    while q:
        addr = q.popleft()
        result.append(addr)
        for caller in callers[addr]:
            out_degree[caller] -= 1
            if out_degree[caller] == 0:
                q.append(caller)
    seen = set(result)
    result.extend(a for a in graph if a not in seen)
    return result


def with_last(items: ty.Iterable[_T]) -> ty.Iterator[tuple[_T, bool]]:
    """Yield ``(item, is_last)``; ``is_last`` is True only for the final item.

    A one-item lookahead over any iterator — lets a streaming consumer treat the
    last element specially (here: run global analysis only on the final batch)
    without materialising the whole stream.
    """
    it = iter(items)
    try:
        prev = next(it)
    except StopIteration:
        return
    for item in it:
        yield prev, False
        prev = item
    yield prev, True


def iter_batches(
    objects: ty.Iterable[_T],
    size_of: ty.Callable[[_T], int],
) -> ty.Iterator[tuple[list[_T], int]]:
    """Stream ``objects`` into ``(batch, byte_size)`` chunks.

    A batch is flushed when adding the next object would exceed
    ``MAX_OBJECTS_IN_REVISION`` or ``MAX_UPLOAD_BYTES``
    """
    current: list[_T] = []
    current_bytes = 0
    for obj in objects:
        obj_bytes = size_of(obj)
        if current and (
            len(current) == MAX_OBJECTS_IN_REVISION
            or current_bytes + obj_bytes > MAX_UPLOAD_BYTES
        ):
            yield current, current_bytes
            current = []
            current_bytes = 0
        current.append(obj)
        current_bytes += obj_bytes
    if current:
        yield current, current_bytes
