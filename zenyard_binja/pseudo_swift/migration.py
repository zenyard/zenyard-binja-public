from __future__ import annotations

import typing as ty

from binaryninja import (  # type: ignore[import]
    BackgroundTaskThread,
    BinaryView,
    core_ui_enabled,
)

from ..helpers.apply_inferences import _get_function_at
from ..helpers.log import log_info, log_warn
from .metadata_keys import (
    LEGACY_NOT_SWIFT_BLOB_KEY,
    LEGACY_SWIFT_BLOB_KEY,
    NOT_SWIFT_FUNCTION_METADATA_KEY,
    SWIFT_FUNCTION_METADATA_KEY,
)

_TITLE = "Zenyard: migrating Swift data…"
_STRIDE = 256  # report progress / poll cancel every N entries
ProgressFn = ty.Callable[[int, int], bool]


def maybe_migrate_swift_metadata(bv: BinaryView) -> None:
    """Migrate the legacy single-blob Swift metadata to per-function metadata.

    Idempotent: a no-op when the legacy blobs are absent (already migrated or
    never present). Does not auto-save — the per-function writes and the blob
    removal persist on the user's next normal ``.bndb`` save.

    Threading: this is called from the
    ``BinaryViewInitialAnalysisCompletionEvent`` callback, which runs on a
    worker thread that holds the BinaryView lock. The per-function
    ``store_metadata`` writes need that same lock, so they must run *after* this
    callback returns. In the UI we therefore hand the work to a
    :class:`BackgroundTaskThread` (which starts independently and reports
    progress in the status bar); blocking the callback thread on the main
    thread instead — e.g. ``execute_on_main_thread_and_wait`` around a modal
    progress dialog — deadlocks (the dialog's task waits for the lock the
    blocked callback still holds). Headless we run synchronously.
    """
    swift = bv.get_metadata(LEGACY_SWIFT_BLOB_KEY)
    not_swift = bv.get_metadata(LEGACY_NOT_SWIFT_BLOB_KEY)
    swift = swift if isinstance(swift, dict) else None
    not_swift = not_swift if isinstance(not_swift, dict) else None
    if not swift and not not_swift:
        return
    items = [
        (SWIFT_FUNCTION_METADATA_KEY, k, v) for k, v in (swift or {}).items()
    ] + [
        (NOT_SWIFT_FUNCTION_METADATA_KEY, k, v)
        for k, v in (not_swift or {}).items()
    ]
    log_info(
        f"Zenyard: migrating {len(items)} legacy Swift entries to "
        "per-function metadata"
    )

    if core_ui_enabled():
        _MigrationTask(bv, items, swift, not_swift).start()
    else:
        # Headless: run synchronously, never cancel.
        _migrate(bv, items, swift, not_swift, lambda cur, mx: True)


class _MigrationTask(BackgroundTaskThread):
    """Runs the migration off the analysis-completion callback thread.

    Reports progress in the status bar and honours user cancellation, without
    ever blocking the callback thread on the main thread (see
    :func:`maybe_migrate_swift_metadata` for why that deadlocks).
    """

    def __init__(
        self,
        bv: BinaryView,
        items: list[tuple[str, str, dict]],
        swift: dict | None,
        not_swift: dict | None,
    ) -> None:
        super().__init__(_TITLE, can_cancel=True)
        self._bv = bv
        self._items = items
        self._swift = swift
        self._not_swift = not_swift

    def run(self) -> None:
        def progress(cur: int, mx: int) -> bool:
            self.progress = f"{_TITLE} {cur}/{mx}"
            return not self.cancelled

        _migrate(self._bv, self._items, self._swift, self._not_swift, progress)


def _migrate(
    bv: BinaryView,
    items: list[tuple[str, str, dict]],
    swift: dict | None,
    not_swift: dict | None,
    progress: ProgressFn,
) -> None:
    fn_cache: dict = {}
    total = len(items)
    for i, (key, addr_str, payload) in enumerate(items):
        if i % _STRIDE == 0 and not progress(i, total):
            log_info("Zenyard: Swift migration cancelled; legacy blob retained")
            return
        try:
            addr = int(addr_str)  # legacy keys are decimal str(func.start)
        except (ValueError, TypeError):
            continue
        func = _get_function_at(bv, addr, fn_cache)
        if func is None:
            continue
        try:
            func.store_metadata(key, payload)  # isAuto=False → persists on save
        except Exception as e:
            log_warn(f"Zenyard: failed to migrate entry at {addr_str}: {e}")
    progress(total, total)
    # Completed without cancel → drop legacy blobs (persists on next save).
    if swift is not None:
        bv.remove_metadata(LEGACY_SWIFT_BLOB_KEY)
    if not_swift is not None:
        bv.remove_metadata(LEGACY_NOT_SWIFT_BLOB_KEY)
    log_info("Zenyard: Swift migration complete; legacy blob removed")
