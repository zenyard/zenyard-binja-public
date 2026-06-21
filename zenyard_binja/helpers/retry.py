from __future__ import annotations

import enum
import http.client
import threading
import time
import typing as ty
from dataclasses import dataclass

import urllib3

from ..zenyard_client import ApiException
from .log import concise_error, log_call_error, log_error, log_warn

T = ty.TypeVar("T")


class Disposition(enum.Enum):
    TRANSIENT = "transient"  # outage/timeout/5xx — retry with backoff
    AUTH = "auth"  # 401/403 — stop; the user must fix the API key / plan
    STALE_BINARY = "stale_binary"  # 404 — Binary gone server-side; stop
    FATAL_BUG = "fatal_bug"  # contract drift / coding bug — stop, don't spin


class _GaveUp:
    """Singleton sentinel returned by retry_with_backoff when all retries fail."""

    _instance: _GaveUp | None = None

    def __new__(cls) -> _GaveUp:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance


GAVE_UP = _GaveUp()


@dataclass(frozen=True)
class RetryPolicy:
    max_retries: int | None = 5
    base_delay: float = 2.0
    max_delay: float = 60.0
    stop: threading.Event | None = None
    should_stop: ty.Callable[[], bool] | None = None
    on_permanent: ty.Callable[[Disposition], None] | None = None
    on_failure_count: ty.Callable[[int], None] | None = None


def classify(exc: BaseException) -> Disposition:
    """
    Map an exception from a backend call to its ``Disposition``.
    """
    # A malformed host/URL raises a LocationValueError, which subclasses
    # urllib3's HTTPError — a config bug, not an outage; it must not be
    # retried as if the server were unreachable.
    if isinstance(exc, urllib3.exceptions.LocationValueError):
        return Disposition.FATAL_BUG

    if isinstance(exc, ApiException):
        status = exc.status or 0
        if status in (401, 403):
            return Disposition.AUTH
        if status == 404:
            return Disposition.STALE_BINARY
        if status == 0 or status == 429 or status >= 500:
            return Disposition.TRANSIENT
        return Disposition.FATAL_BUG

    if isinstance(
        exc,
        (urllib3.exceptions.HTTPError, OSError, http.client.HTTPException),
    ):
        # Connection refused/reset, DNS failure, connect/read timeouts,
        # protocol errors, SSL — everything a reconnect can heal.
        return Disposition.TRANSIENT

    return Disposition.FATAL_BUG


def is_transient(exc: BaseException) -> bool:
    return classify(exc) is Disposition.TRANSIENT


def retry_with_backoff(
    fn: ty.Callable[[], T],
    *,
    max_retries: int | None = 5,
    base_delay: float = 2.0,
    max_delay: float = 60.0,
    stop: threading.Event | None = None,
    should_stop: ty.Callable[[], bool] | None = None,
    retry_if: ty.Callable[[BaseException], bool] | None = None,
    on_attempt_failed: ty.Callable[[int, BaseException], None] | None = None,
) -> T | _GaveUp:
    """
    Call *fn* with exponential backoff until it succeeds.
    """

    def _stopped() -> bool:
        return (stop is not None and stop.is_set()) or (
            should_stop is not None and should_stop()
        )

    def _wait(seconds: float) -> bool:
        """Sliced backoff sleep. True if a stop signal fired during it."""
        deadline = time.monotonic() + seconds
        while (remaining := deadline - time.monotonic()) > 0:
            if _stopped():
                return True
            slice_ = min(0.5, remaining)
            if stop is not None:
                stop.wait(slice_)
            else:
                time.sleep(slice_)
        return _stopped()

    delay = base_delay
    attempt = 0
    while True:
        if _stopped():
            return GAVE_UP
        try:
            return fn()
        except Exception as e:
            attempt += 1
            if on_attempt_failed is not None:
                on_attempt_failed(attempt, e)
            if retry_if is not None and not retry_if(e):
                return GAVE_UP
            if max_retries is not None and attempt >= max_retries:
                return GAVE_UP
            if _wait(delay):
                return GAVE_UP
            delay = min(delay * 2, max_delay)


def _backoff_delay(attempt: int, policy: RetryPolicy) -> float:
    return min(policy.base_delay * 2 ** (attempt - 1), policy.max_delay)


def call_backend(
    label: str,
    fn: ty.Callable[[], T],
    policy: RetryPolicy,
) -> T | _GaveUp:
    attempts = 0
    permanent = False

    def _on_attempt_failed(attempt: int, exc: BaseException) -> None:
        nonlocal attempts, permanent
        attempts = attempt
        if is_transient(exc):
            if policy.on_failure_count is not None:
                policy.on_failure_count(attempt)
            log_warn(
                f"{label} transient ({concise_error(exc)});"
                f" retry {attempt} in {_backoff_delay(attempt, policy):.1f}s"
            )
            return
        permanent = True
        log_call_error(f"{label} failed", exc)
        log_error(f"{label} not retried ({classify(exc).value}); stopping")
        if policy.on_permanent is not None:
            policy.on_permanent(classify(exc))

    result = retry_with_backoff(
        fn,
        max_retries=policy.max_retries,
        base_delay=policy.base_delay,
        max_delay=policy.max_delay,
        stop=policy.stop,
        should_stop=policy.should_stop,
        retry_if=is_transient,
        on_attempt_failed=_on_attempt_failed,
    )
    if isinstance(result, _GaveUp):
        if (
            not permanent
            and policy.max_retries is not None
            and attempts >= policy.max_retries
        ):
            log_error(f"{label}: gave up after {attempts} attempts")
        return result
    if policy.on_failure_count is not None:
        policy.on_failure_count(0)
    return result
