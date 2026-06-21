from __future__ import annotations

import threading
from collections.abc import Callable
from uuid import UUID

from ..api_client import LARGE_UPLOAD_TIMEOUT
from .log import log_debug, log_warn
from .retry import GAVE_UP, RetryPolicy, call_backend

from ..zenyard_client import BinariesApi
from ..zenyard_client.models import (
    AddObjectsToCurrentRevisionParams,
    CreateRevisionParams,
    FinishAndAnalyzeCurrentRevisionBody,
    Object,
)
from .inference_types import BvObject


def submit_revision(
    binary_id: UUID,
    api: BinariesApi,
    current_revision: int,
    objects: list[BvObject],
    finish_body: FinishAndAnalyzeCurrentRevisionBody,
    label: str,
    policy: RetryPolicy,
    post_add: Callable[[], bool] | None = None,
    stop: threading.Event | None = None,
) -> tuple[bool, int]:
    next_rev = current_revision + 1
    policy = policy

    addresses = [
        o["address"] if isinstance(o, dict) else o.address for o in objects
    ]
    seen_for_detect: set[str] = set()
    duplicates = [
        a
        for a in addresses
        if a in seen_for_detect or seen_for_detect.add(a)  # type: ignore[func-returns-value]
    ]
    if duplicates:
        log_warn(
            f"duplicate addresses in objects list for revision {next_rev} ({label}):"
            f" {duplicates}"
        )
    seen_for_dedup: set[str] = set()
    deduped: list[BvObject] = [
        o
        for o in objects
        if (addr := (o["address"] if isinstance(o, dict) else o.address))
        not in seen_for_dedup
        and not seen_for_dedup.add(addr)  # type: ignore[func-returns-value]
    ]

    log_debug(f"creating revision {next_rev} ({label})")
    if (
        call_backend(
            f"POST create_revision ({label})",
            lambda: api.create_revision(
                str(binary_id), CreateRevisionParams(number=next_rev)
            ),
            policy,
        )
        is GAVE_UP
    ):
        return False, current_revision

    log_debug(f"adding {len(deduped)} objects to revision {next_rev} ({label})")
    if (
        call_backend(
            f"POST add_objects_to_current_revision ({label})",
            lambda: api.add_objects_to_current_revision(
                str(binary_id),
                AddObjectsToCurrentRevisionParams(
                    objects=[Object(o) for o in deduped]
                ),
                # Large request bodies need a *total* budget — the default
                # (connect, read) pair doesn't bound the send phase.
                _request_timeout=LARGE_UPLOAD_TIMEOUT,
            ),
            policy,
        )
        is GAVE_UP
    ):
        return False, current_revision

    if post_add is not None and not post_add():
        return False, current_revision

    log_debug(f"finishing revision {next_rev} ({label})")
    if (
        call_backend(
            f"POST finish_and_analyze_current_revision ({label})",
            lambda: api.finish_and_analyze_current_revision(
                str(binary_id), finish_body
            ),
            policy,
        )
        is GAVE_UP
    ):
        return False, current_revision

    return True, next_rev
