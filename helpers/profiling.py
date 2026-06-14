"""Flag-gated memory/timing instrumentation for the extraction pipeline.

Enabled only when the ``ZENYARD_PROFILE`` environment variable is truthy;
otherwise every method is a no-op, so production runs pay nothing.

``tracemalloc`` is used because it tracks the *Python* heap — exactly where
the heavy ``Function`` objects, their ``code`` strings and ``ranges`` lists
live. ``reset_peak()`` between phases isolates each phase's transient peak
(e.g. the JSON buffers built while hashing) from the running total.
"""

from __future__ import annotations

import os
import resource
import sys
import time
import tracemalloc

from .log import log_info


def _enabled() -> bool:
    return os.environ.get("ZENYARD_PROFILE", "").strip().lower() not in (
        "",
        "0",
        "false",
        "no",
    )


def _process_peak_rss_bytes() -> int:
    # ru_maxrss is bytes on macOS, kilobytes on Linux.
    maxrss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return maxrss if sys.platform == "darwin" else maxrss * 1024


def _mb(n: int) -> str:
    return f"{n / (1024 * 1024):.1f} MB"


class MemoryProfiler:
    """Records Python-heap usage and elapsed time across labelled phases."""

    def __init__(self, name: str) -> None:
        self._enabled = _enabled()
        self._name = name
        self._phases: list[tuple[str, int, int, float]] = []
        self._started: float = 0.0
        if self._enabled:
            tracemalloc.start(1)
            self._started = time.monotonic()
            log_info(f"profile[{name}]: started")

    def phase(self, label: str) -> None:
        """Snapshot heap usage at a boundary, then reset the transient peak."""
        if not self._enabled:
            return
        current, peak = tracemalloc.get_traced_memory()
        elapsed = time.monotonic() - self._started
        self._phases.append((label, current, peak, elapsed))
        log_info(
            f"profile[{self._name}]: {label} — "
            f"heap={_mb(current)} peak={_mb(peak)} t={elapsed:.1f}s"
        )
        tracemalloc.reset_peak()

    def summary(self) -> None:
        if not self._enabled:
            return
        log_info(f"profile[{self._name}]: ── summary ──")
        for label, current, peak, elapsed in self._phases:
            log_info(
                f"profile[{self._name}]:   {label:<28} "
                f"heap={_mb(current):>10} peak={_mb(peak):>10} "
                f"t={elapsed:.1f}s"
            )
        log_info(
            f"profile[{self._name}]: process peak RSS = "
            f"{_mb(_process_peak_rss_bytes())}"
        )
        tracemalloc.stop()
