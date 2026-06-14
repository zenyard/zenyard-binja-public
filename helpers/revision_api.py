from __future__ import annotations

import threading
from collections.abc import Callable
from uuid import UUID

from .log import log_debug, log_api_error, log_request_error
from .retry import GAVE_UP, retry_with_backoff

from ..zenyard_client import ApiException, BinariesApi
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
    post_add: Callable[[], bool] | None = None,
    stop: threading.Event | None = None,
) -> tuple[bool, int]:
    """Create a revision, add objects, optionally run post_add, then finish+analyze.

    Returns (True, new_revision) on success, (False, current_revision) on failure.
    Does not write to Model — callers are responsible for updating model state.
    Each API call is retried with exponential backoff.
    """
    next_rev = current_revision + 1

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
        from .log import log_warn

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

    def _create() -> None:
        try:
            api.create_revision(
                str(binary_id), CreateRevisionParams(number=next_rev)
            )
        except ApiException as e:
            log_api_error(f"POST create_revision failed ({label})", e)
            raise
        except Exception as e:
            log_request_error(
                f"POST create_revision request failed ({label})", e
            )
            raise

    if retry_with_backoff(_create, stop=stop) is GAVE_UP:
        return False, current_revision

    log_debug(f"adding {len(deduped)} objects to revision {next_rev} ({label})")

    def _add_objects() -> None:
        try:
            api.add_objects_to_current_revision(
                str(binary_id),
                AddObjectsToCurrentRevisionParams(
                    objects=[Object(o) for o in deduped]
                ),
            )
        except ApiException as e:
            log_api_error(
                f"POST add_objects_to_current_revision failed ({label})", e
            )
            raise
        except Exception as e:
            log_request_error(
                f"POST add_objects_to_current_revision request failed ({label})",
                e,
            )
            raise

    if retry_with_backoff(_add_objects, stop=stop) is GAVE_UP:
        return False, current_revision

    if post_add is not None and not post_add():
        return False, current_revision

    # M-07: retry finish_and_analyze.
    log_debug(f"finishing revision {next_rev} ({label})")

    def _finish() -> None:
        try:
            api.finish_and_analyze_current_revision(str(binary_id), finish_body)
        except ApiException as e:
            log_api_error(
                f"POST finish_and_analyze_current_revision failed ({label})", e
            )
            raise
        except Exception as e:
            log_request_error(
                f"POST finish_and_analyze_current_revision request failed ({label})",
                e,
            )
            raise

    if retry_with_backoff(_finish, stop=stop) is GAVE_UP:
        return False, current_revision

    return True, next_rev
