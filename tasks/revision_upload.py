from __future__ import annotations

import json
import typing as ty
from dataclasses import dataclass

from ..helpers.hashing import Uploadable, hash_uploadable
from ..helpers.log import log_info, log_debug
from ..helpers.orchestration import iter_batches, topo_sort_addrs, with_last
from ..helpers.revision_api import submit_revision
from ..objects import (
    call_target_addrs,
    extract_one_function,
    extract_one_global,
    global_user_addrs,
    partition_addrs,
)
from ..zenyard_client.models import (
    FinishAndAnalyzeCurrentRevisionBody,
    Function,
)

if ty.TYPE_CHECKING:
    from .bring_up import BringUpTask

_HashedPair = tuple[Uploadable, bytes]


def _uploadable_size(obj: Uploadable) -> int:
    return len(json.dumps(obj.to_dict()).encode())


def _pair_size(pair: _HashedPair) -> int:
    return _uploadable_size(pair[0])


@dataclass(frozen=True)
class UploadResult:
    """Outcome of a ``RevisionUploader.run()`` — durably-uploaded objects only."""

    failed: bool
    uploaded_function_addrs: frozenset[int]
    uploaded_global_addrs: frozenset[int]
    revision: int
    batches: int

    @property
    def uploaded_count(self) -> int:
        return len(self.uploaded_function_addrs) + len(
            self.uploaded_global_addrs
        )


class RevisionUploader:
    """Streams the planned functions + globals to the backend in revisions.

    Extracts each function lazily in the planned (topo) order, drops objects
    whose content is unchanged since the last upload, batches the rest by
    size/count, and submits each batch as a revision — only the final batch
    triggers global analysis (matching the pre-streaming behaviour). Content
    hashes are persisted per acked batch; on failure the caller re-queues
    whatever never durably uploaded.
    """

    def __init__(
        self,
        task: BringUpTask,
        *,
        planned_addrs: list[int],
        last_uploaded_hashes: dict[int, bytes],
        inference_seq: int | None,
    ) -> None:
        binary_id = task._model.binary_id
        assert binary_id is not None  # caller guarantees registration
        self._task = task
        self._bv = task._bv
        self._api = task._api
        self._model = task._model
        self._binary_id = binary_id
        self._planned_addrs = planned_addrs
        self._last_uploaded_hashes = last_uploaded_hashes
        self._inference_seq = inference_seq
        self._revision = task._model.last_submitted_revision
        self._batch_count = 0
        self._uploaded_function_addrs: set[int] = set()
        self._uploaded_global_addrs: set[int] = set()

    def run(self) -> UploadResult:
        for (hashed_objects, batch_bytes), is_last in with_last(
            iter_batches(self._changed_objects(), _pair_size)
        ):
            if not self._upload_batch(hashed_objects, batch_bytes, is_last):
                return self._build_result(failed=True)
        return self._build_result(failed=False)

    # ── streaming pipeline ────────────────────────────────────────────────

    def _build_upload_order(
        self,
    ) -> tuple[list[int], dict[int, set[int]], set[int], set[int]]:
        """Order all planned addresses into one interleaved, callee/use-first
        stream.

        Functions are ordered callees-first; each global is placed before every
        function that references it (matching the IDA plugin's unified topo
        sort). Thunks are dropped (accepted but never uploaded). The ordering
        ``graph`` is kept separate from ``calls_by_addr`` so a global never
        leaks into a function's hashed ``calls`` payload.

        Returns ``(ordered, calls_by_addr, function_addrs, global_addrs)``.
        """
        fn_addrs, gl_addrs, _thunks = partition_addrs(
            self._bv, self._planned_addrs
        )
        calls_by_addr: dict[int, set[int]] = {}
        for a in fn_addrs:
            func = self._bv.get_function_at(a)
            if func is None:  # partition guarantees a function; defensive only
                continue
            calls_by_addr[a] = call_target_addrs(self._bv, func)

        function_addrs, global_addrs = set(calls_by_addr), set(gl_addrs)
        graph: dict[int, set[int]] = {
            a: set(callees) for a, callees in calls_by_addr.items()
        }
        for g in global_addrs:
            graph[g] = set()  # global: a zero-dependency leaf node
        for g in global_addrs:
            for f in global_user_addrs(self._bv, g) & function_addrs:
                graph[f].add(g)  # global emitted before its consuming function
        ordered = topo_sort_addrs(graph)
        return ordered, calls_by_addr, function_addrs, global_addrs

    def _extract_in_order(self) -> ty.Iterator[Uploadable]:
        ordered, calls_by_addr, _function_addrs, global_addrs = (
            self._build_upload_order()
        )
        # Denominator for the extraction progress dialog — the post-thunk-drop
        # count, so the bar reaches exactly 100% when this loop finishes.
        self._task.objects_extract_total = len(ordered)
        # Computing function starts touches every function, so only do it when
        # there are globals to extract (extract_one_global needs the set).
        log_debug(f"ordered upload: [{len(ordered)}] -> {ordered}")
        function_addresses = (
            {f.start for f in self._bv.functions} if global_addrs else set()
        )
        for i, addr in enumerate(ordered):
            # Per-object check: Cancel is a user-facing button now (extraction
            # progress dialog), so it must respond promptly. The cost is two
            # boolean reads, dwarfed by per-function HLIL extraction below.
            self._task.check_cancelled()
            self._task.objects_extracted = i + 1
            if addr in global_addrs:
                gl = extract_one_global(
                    self._bv, addr, function_addresses, self._inference_seq
                )
                if gl is not None:
                    yield gl
            else:
                fn = extract_one_function(
                    self._bv,
                    addr,
                    self._inference_seq,
                    # Payload comes from calls_by_addr (functions only), never
                    # from the ordering graph which also holds global edges.
                    calls=sorted(f"{t:016x}" for t in calls_by_addr[addr]),
                )
                if fn is not None:
                    yield fn

    def _changed_objects(self) -> ty.Iterator[_HashedPair]:
        """Yield ``(obj, hash)`` for every changed object.

        Drops objects whose canonical payload matches what we last uploaded.
        Pairing the hash with its object lets it ride through ``iter_batches``
        so each batch persists its own hashes once acked — no shared bookkeeping.
        """
        for obj in self._extract_in_order():
            h = hash_uploadable(obj)
            if h == self._last_uploaded_hashes.get(int(obj.address, 16)):
                continue
            yield obj, h

    def _upload_batch(
        self,
        hashed_objects: list[_HashedPair],
        batch_bytes: int,
        is_last: bool,
    ) -> bool:
        self._task.check_cancelled()
        self._batch_count += 1
        label = f"batch {self._batch_count}, revision {self._revision + 1}"
        self._task.progress = (
            f"Zenyard: uploading objects (batch {self._batch_count})…"
        )
        log_info(
            f"uploading {label} ({len(hashed_objects)} objects,"
            f" ~{batch_bytes // 1024} KB)"
        )
        ok, self._revision = submit_revision(
            self._binary_id,
            self._api,
            self._revision,
            [o for o, _ in hashed_objects],  # type: ignore[arg-type]
            FinishAndAnalyzeCurrentRevisionBody(
                analyze_dependents=True,
                perform_global_analysis=is_last,
                swift_only=None,
            ),
            label=label,
            policy=self._task._upload_policy(),
        )
        if not ok:
            return False
        self._model.update_uploaded_hashes(
            {int(o.address, 16): h for o, h in hashed_objects}
        )
        for o, _ in hashed_objects:
            addr_int = int(o.address, 16)
            if isinstance(o, Function):
                self._uploaded_function_addrs.add(addr_int)
            else:
                self._uploaded_global_addrs.add(addr_int)
        # Advance the status-bar progress per acked batch (otherwise the bar
        # sits at 0 until the whole upload finishes).
        self._task.objects_uploaded = len(self._uploaded_function_addrs) + len(
            self._uploaded_global_addrs
        )
        log_info(f"uploaded {label}")
        return True

    def _build_result(self, *, failed: bool) -> UploadResult:
        return UploadResult(
            failed=failed,
            uploaded_function_addrs=frozenset(self._uploaded_function_addrs),
            uploaded_global_addrs=frozenset(self._uploaded_global_addrs),
            revision=self._revision,
            batches=self._batch_count,
        )
