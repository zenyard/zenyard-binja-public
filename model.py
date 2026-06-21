from __future__ import annotations

import threading
from typing import Any, ClassVar, Iterable
from uuid import UUID

from binaryninja import BinaryView  # type: ignore[import]
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr

# Sentinel placed on inference_queue to signal ApplyInferencesTask to exit.
INFERENCE_DRAIN_SENTINEL = object()


class Model(BaseModel):
    """
    Source of truth for all per-BinaryView state.

    Writes are serialised by an internal RLock so any Task can mutate the
    Model directly (the Coordinator mailbox is no longer the single writer).
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    bv: BinaryView
    status: str = "Zenyard: ready"
    binary_id: UUID | None = None
    last_submitted_revision: int = 0
    last_completed_revision: int = 0
    inference_cursor: int | None = None
    sections_uploaded_revision: int = 0
    applied_count: int = 0

    auto_apply: bool = True

    # Addresses whose function/global received a visible inference
    applied_addresses: set[int] = Field(default_factory=set)

    uploaded_hash: dict[int, bytes] = Field(default_factory=dict)

    _initialized: bool = PrivateAttr(default=False)
    _lock: threading.RLock = PrivateAttr(default_factory=threading.RLock)

    _PERSISTED_KEYS: ClassVar[dict[str, str]] = {
        "binary_id": "zenyard.binary_id",
        "last_completed_revision": "zenyard.final_revision",
        "applied_count": "zenyard.applied_count",
        "inference_cursor": "zenyard.inference_cursor",
        "sections_uploaded_revision": "zenyard.sections_revision",
        "auto_apply": "zenyard.auto_apply",
        "function_original_annotations": "zenyard.function_original_annotations",
        "applied_addresses": "zenyard.applied_addresses",
    }

    @staticmethod
    def _serialize(name: str, value: Any) -> Any:
        if name == "binary_id":
            return str(value)
        if name == "applied_addresses":
            return sorted(value)
        return value

    @staticmethod
    def _deserialize(name: str, raw: Any) -> Any:
        if name == "binary_id":
            return UUID(str(raw))
        if name == "applied_addresses":
            return {int(x) for x in raw}
        return raw

    def model_post_init(self, __context: Any) -> None:
        self._initialized = True

    def __setattr__(self, name: str, value: Any) -> None:
        if name.startswith("_"):
            super().__setattr__(name, value)
            return
        with self._lock:
            super().__setattr__(name, value)
            if not self._initialized:
                return
            key = self._PERSISTED_KEYS.get(name)
            if key is None or value is None:
                return
            self.bv.store_metadata(key, self._serialize(name, value))

    def add_applied(self, n: int) -> None:
        """Increment the cumulative applied-inference counter (lock-safe)."""
        if n <= 0:
            return
        with self._lock:
            self.applied_count += n

    def add_applied_addresses(self, addrs: Iterable[int]) -> None:
        """Record addresses that just had a visible inference applied.

        Persisted explicitly: an in-place ``set.update`` does not trip
        ``__setattr__``, so we write the metadata here directly. The
        Symbols-sidebar overlay reads these (resolved to the symbols' current
        names) to tint applied rows.

        Known limitation: addresses are never removed, so undoing an applied
        rename leaves the address here and the row stays tinted (resolved to the
        reverted name) — a cosmetic false positive, not a correctness issue.
        """
        addrs = [int(a) for a in addrs]
        if not addrs:
            return
        with self._lock:
            self.applied_addresses.update(addrs)
            self.bv.store_metadata(
                self._PERSISTED_KEYS["applied_addresses"],
                self._serialize("applied_addresses", self.applied_addresses),
            )

    def applied_addresses_snapshot(self) -> frozenset[int]:
        """Lock-safe immutable snapshot for the overlay paint controller."""
        with self._lock:
            return frozenset(self.applied_addresses)

    def uploaded_hash_snapshot(self) -> dict[int, bytes]:
        """Lock-safe copy of the per-object content hashes already uploaded.

        The uploader uses this to skip re-sending objects whose content is
        unchanged since the last batch within a single bring-up.
        """
        with self._lock:
            return dict(self.uploaded_hash)

    def update_uploaded_hashes(self, hashes: dict[int, bytes]) -> None:
        """Record content hashes for objects whose upload was just acked."""
        if not hashes:
            return
        with self._lock:
            self.uploaded_hash.update(hashes)

    @classmethod
    def create(cls, bv: BinaryView) -> "Model":
        loaded: dict[str, Any] = {}
        for field_name, key in cls._PERSISTED_KEYS.items():
            try:
                raw = bv.query_metadata(key)
                if raw is not None:
                    loaded[field_name] = cls._deserialize(field_name, raw)
            except Exception:
                pass
        return cls(bv=bv, **loaded)
