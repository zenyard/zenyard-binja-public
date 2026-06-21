from __future__ import annotations

import hashlib
import json
from typing import Iterator, Optional, Union
from binaryninja import BinaryView  # type: ignore[import]

from ..zenyard_client.models import (
    AddressDetail,
    Function,
    GlobalVariable,
    Range,
)

Uploadable = Union[Function, GlobalVariable]


def hash_uploadable(obj: Uploadable, bv: BinaryView | None = None) -> bytes:
    """Stable content hash of an upload-ready object, for upload dedupe only.

    - ``inference_seq_number`` is dropped: it advances as inferences flow but
      is not part of the object's content.
    - For functions, references to *other* objects in the code are reduced to
      ``[obj]``, so renaming a callee or global doesn't change every caller's hash.

    Returns a 32-byte blake2b digest — used for equality only, never decoded.
    """
    if isinstance(obj, Function):
        obj = _reduce_object_references(obj, bv)
    payload = obj.to_dict()
    payload.pop("inference_seq_number", None)
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":")
    ).encode()
    return hashlib.blake2b(encoded, digest_size=32).digest()


def _reduce_object_references(
    func: Function, bv: Optional[BinaryView]
) -> Function:
    """Replace references to *other* objects in the code with ``[obj]``.

    only the referenced object's *name text* is neutralized; the range's
    ``AddressDetail``(the referenced address) is preserved, so retargeting a
    reference still changes the hash while renaming the referenced object does not.

    A reference is reduced only when its target is a *real object* — a function
    entry or data variable that we upload (and the backend names)
    """
    reduced_parts: list[str] = []
    reduced_ranges: list[Range] = []
    current_start = 0

    for text, rng in _code_slices(func.code, func.ranges or ()):
        if rng is not None:
            if _is_reducible_reference(
                rng.detail.actual_instance, func.address, bv
            ):
                text = "[obj]"
            reduced_ranges.append(
                Range(detail=rng.detail, start=current_start, length=len(text))
            )
        reduced_parts.append(text)
        current_start += len(text)

    return func.model_copy(
        update={"code": "".join(reduced_parts), "ranges": reduced_ranges}
    )


def _is_reducible_reference(
    detail: object, func_address: str, bv: Optional[BinaryView]
) -> bool:
    """Whether a range's reference should be neutralized to ``[obj]``.

    True only for references to *other* objects (functions / data vars). The
    function's own address is never reduced. With no ``bv`` to classify the
    target, fall back to treating every foreign address reference as reducible.
    """
    if not isinstance(detail, AddressDetail) or detail.address == func_address:
        return False
    if bv is None:
        return True
    address = int(detail.address, 16)
    return (
        bv.get_function_at(address) is not None
        or bv.get_data_var_at(address) is not None
    )


def _code_slices(
    code: str, ranges: "tuple[Range, ...] | list[Range]"
) -> Iterator[tuple[str, Optional[Range]]]:
    """Yield ``(text, range)`` slices covering ``code`` in order.

    Slices that fall inside a ``Range`` yield that range; gaps between ranges
    yield ``None``. Port of the IDA plugin's ``transform_code._code_slices``.
    """
    sorted_ranges = sorted(ranges, key=lambda r: r.start)
    last_end = 0

    for r in sorted_ranges:
        if r.start > last_end:
            yield code[last_end : r.start], None
        yield code[r.start : r.start + r.length], r
        last_end = r.start + r.length

    if last_end < len(code):
        yield code[last_end:], None
