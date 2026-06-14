from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import Iterator

from binaryninja import BinaryDataNotification, BinaryView  # type: ignore[import]

from .model import Model
from .helpers.log import log_debug


class ChangeTracker(BinaryDataNotification):
    """
    Observes Binary Ninja's edit/delete notifications for Functions and
    Globals and marks the affected addresses Dirty or Removed in the Model.

    The single filter against false positives is the ``paused()`` refcounted
    context, wrapped around AI inference batches so the BV writes the plugin
    performs aren't re-observed as user edits.

    Noisy BN notifications (analysis-completion ``function_updated`` calls
    that don't change user-visible state) are not filtered here — they are
    de-duped at upload time by ``BringUpTask``'s content-hash check against
    ``Model.uploaded_hash``. Marking a function dirty is cheap; the hash
    check at extract time absorbs any spurious marks.
    """

    def __init__(self, model: Model) -> None:
        super().__init__()
        self._model = model
        self._pause_count = 0
        self._pause_lock = threading.Lock()

    # ── Pause primitive ───────────────────────────────────────────────────────

    @contextmanager
    def paused(self) -> Iterator[None]:
        with self._pause_lock:
            self._pause_count += 1
        try:
            yield
        finally:
            with self._pause_lock:
                self._pause_count -= 1

    def _is_paused(self) -> bool:
        with self._pause_lock:
            return self._pause_count > 0

    # ── Transitive marking ────────────────────────────────────────────────────

    def _mark_global_xrefs_dirty(self, view: BinaryView, var_addr: int) -> None:
        """Mark every function that references this global as dirty.

        A global's user-visible state (name, type) appears in the HLIL
        of any function that reads or writes it, so a change to the
        global changes those functions' upload payload too. Mirrors the
        IDA plugin's xref propagation rule
        (``decompai-ida``'s ``track_changes_task._extract_changed_object_addresses``).
        """
        try:
            marked = 0
            for ref in view.get_code_refs(var_addr):
                for fn in view.get_functions_containing(ref.address):
                    self._model.mark_function_dirty(fn.start)
                    marked += 1
            if marked:
                log_debug(
                    f"changeTracker: marked {marked} caller(s) "
                    f"of global {var_addr:#x} dirty"
                )
        except Exception as ex:
            log_debug(
                f"changeTracker: xref walk failed for {var_addr:#x}: {ex}"
            )

    # ── Callbacks ─────────────────────────────────────────────────────────────

    def function_updated(self, view: BinaryView, func: object) -> None:
        if self._is_paused():
            return
        self._model.mark_function_dirty(func.start)  # type: ignore[union-attr]

    def function_added(self, view: BinaryView, func: object) -> None:
        if self._is_paused():
            return
        self._model.mark_function_dirty(func.start)  # type: ignore[union-attr]

    def function_removed(self, view: BinaryView, func: object) -> None:
        if self._is_paused():
            return
        self._model.mark_function_removed(func.start)  # type: ignore[union-attr]

    def data_var_updated(self, view: BinaryView, var: object) -> None:
        if self._is_paused():
            return
        addr = var.address  # type: ignore[union-attr]
        self._model.mark_global_dirty(addr)
        self._mark_global_xrefs_dirty(view, addr)

    def data_var_added(self, view: BinaryView, var: object) -> None:
        if self._is_paused():
            return
        addr = var.address  # type: ignore[union-attr]
        self._model.mark_global_dirty(addr)
        self._mark_global_xrefs_dirty(view, addr)

    def data_var_removed(self, view: BinaryView, var: object) -> None:
        if self._is_paused():
            return
        addr = var.address  # type: ignore[union-attr]
        self._model.mark_global_removed(addr)
        self._mark_global_xrefs_dirty(view, addr)
