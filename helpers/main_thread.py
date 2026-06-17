from __future__ import annotations

import typing as ty

from binaryninja import execute_on_main_thread_and_wait  # type: ignore[import]

T = ty.TypeVar("T")


def run_on_main_thread(fn: ty.Callable[[], T]) -> T:
    """Run ``fn`` on Binary Ninja's main thread and return its result.

    ``execute_on_main_thread_and_wait`` cannot return a value or surface an
    exception to the caller, so capture both across the thread boundary and
    re-raise here — letting callers get a plain, typed return value.
    """
    result: list[T] = []
    error: list[BaseException] = []

    def runner() -> None:
        try:
            result.append(fn())
        except BaseException as exc:  # noqa: BLE001 — surfaced to caller below
            error.append(exc)

    execute_on_main_thread_and_wait(runner)
    if error:
        raise error[0]
    return result[0]
