from __future__ import annotations

import threading
import time
import typing as ty

from .log import log_error, log_warn

T = ty.TypeVar("T")


# Internal sentinel used to signal that all retry attempts were exhausted.
# Callers should never need to reference this directly.
class _GaveUp:
    """Singleton sentinel returned by retry_with_backoff when all retries fail."""

    _instance: _GaveUp | None = None

    def __new__(cls) -> _GaveUp:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance


GAVE_UP = _GaveUp()


def retry_with_backoff(
    fn: ty.Callable[[], T],
    *,
    max_retries: int = 5,
    base_delay: float = 2.0,
    max_delay: float = 60.0,
    stop: threading.Event | None = None,
) -> T | _GaveUp:
    """Call *fn* up to *max_retries* times with exponential backoff.

    Returns the result of *fn* on the first success.  Returns the ``GAVE_UP``
    sentinel (an instance of ``_GaveUp``) if all attempts fail or *stop* is set
    between attempts.  Callers should check ``isinstance(result, _GaveUp)`` to
    detect failure.
    """
    delay = base_delay
    for attempt in range(max_retries):
        if stop is not None and stop.is_set():
            return GAVE_UP
        try:
            return fn()
        except Exception as e:
            if attempt == max_retries - 1:
                log_error(
                    f"Zenyard: giving up after {max_retries} attempts: {e}"
                )
                return GAVE_UP
            log_warn(
                f"Zenyard: attempt {attempt + 1} failed: {e}; retrying in {delay:.0f}s"
            )
            if stop is not None:
                stop.wait(delay)
            else:
                time.sleep(delay)
            delay = min(delay * 2, max_delay)
    return GAVE_UP
