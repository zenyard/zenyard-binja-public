from __future__ import annotations

import typing as ty
import re
from contextlib import nullcontext
from dataclasses import dataclass, field

from binaryninja import BinaryView, FunctionParameter, Type  # type: ignore[import]
from binaryninja import Function as BnFunction  # type: ignore[import]

from .log import log_debug, log_info, log_warn

from ..pseudo_swift.metadata_keys import (
    NOT_SWIFT_FUNCTION_METADATA_KEY,
    SWIFT_FUNCTION_METADATA_KEY,
)
from ..zenyard_client.models import (
    FunctionOverview,
    Name,
    NotSwift,
    ParameterType,
    ParametersMapping,
    ReturnType,
    SwiftFunction,
    TranslationProfile,
    VariablesMapping,
)
from .inference_types import InferenceItem
from ..model import Model

_BATCH_SIZE = 16


def _batched(
    it: ty.Iterable[InferenceItem], n: int
) -> ty.Iterator[list[InferenceItem]]:
    batch: list[InferenceItem] = []
    for item in it:
        batch.append(item)
        if len(batch) == n:
            yield batch
            batch = []
    if batch:
        yield batch


@dataclass(frozen=True)
class HandlerSpec:
    apply: ty.Callable[..., None]
    scope: ty.Literal["bv", "address"]
    undoable: bool = field(default=True)


_HANDLERS: dict[type, HandlerSpec] = {}


def _register(
    inf_type: type,
    scope: ty.Literal["bv", "address"],
    undoable: bool = True,
) -> ty.Callable[[ty.Callable[..., None]], ty.Callable[..., None]]:
    def decorator(fn: ty.Callable[..., None]) -> ty.Callable[..., None]:
        _HANDLERS[inf_type] = HandlerSpec(fn, scope, undoable)
        return fn

    return decorator


def apply_inferences(
    bv: BinaryView,
    inferences: ty.Iterable[InferenceItem],
    model: Model | None = None,
) -> int:
    """Apply a batch of inferences to the BinaryView.

    Returns the number of inferences actually applied — items with no
    registered handler are skipped and not counted.

    Must be called on the main thread. The trailing ``update_analysis()`` only
    kicks off analysis; the caller then waits for it to drain on the background
    thread (BN forbids the UI thread from calling ``update_analysis_and_wait``).
    """
    fn_cache: dict[int, BnFunction | None] = {}
    applied = 0
    # Addresses whose symbol visibly changed — recorded so the Symbols-sidebar
    # overlay can tint them. Swift/NotSwift are excluded below (they only write
    # Model state, no visible symbol change).
    touched: set[int] = set()
    for batch in _batched(inferences, _BATCH_SIZE):
        func_type_groups: dict[int, list[ParameterType | ReturnType]] = {}
        other: list[InferenceItem] = []
        for inf in batch:
            if (
                isinstance(inf, (ParameterType, ReturnType))
                and not inf.struct_id
            ):
                addr = int(inf.address, 16)
                func_type_groups.setdefault(addr, []).append(inf)
            else:
                other.append(inf)

        for inf in other:
            spec = _HANDLERS.get(type(inf), None)
            if spec is None:
                log_debug(f"skipping inference type {type(inf).__name__}")
                continue
            addr = getattr(inf, "address", "bv-level")
            log_debug(f"applying {type(inf).__name__} at {addr}")
            ctx = (
                bv.undoable_transaction()  # pyright: ignore[reportGeneralTypeIssues]
                if spec.undoable
                else nullcontext()
            )
            with ctx:
                spec.apply(bv, inf, fn_cache)
            applied += 1
            if not isinstance(inf, (SwiftFunction, NotSwift)):
                addr_str = getattr(inf, "address", None)
                if isinstance(addr_str, str):
                    try:
                        touched.add(int(addr_str, 16))
                    except ValueError:
                        pass

        for addr, type_infs in func_type_groups.items():
            _apply_function_types_batch(bv, addr, type_infs, fn_cache)
            applied += len(type_infs)
            touched.add(addr)

    if model is not None and touched:
        model.add_applied_addresses(touched)
    bv.update_analysis()
    return applied


def normalize_open_arrays(type_str: str) -> str:
    """Normalize zero-length array types (e.g. ``uint8_t[]``) to pointer types."""
    return re.sub(r"([\w][\w\s*]*)\[\s*\]", r"\1*", type_str.strip())


def _apply_function_types_batch(
    bv: BinaryView,
    addr: int,
    inferences: list[ParameterType | ReturnType],
    fn_cache: dict[int, BnFunction | None] | None = None,
) -> None:
    func = _get_function_at(bv, addr, fn_cache)
    if func is None:
        return
    params = list(func.type.parameters)
    return_type = func.return_type
    changed = False
    with bv.undoable_transaction():  # pyright: ignore[reportGeneralTypeIssues]
        for inf in inferences:
            annotation = normalize_open_arrays(inf.type_annotation)
            try:
                parsed_type, _ = bv.parse_type_string(annotation)
            except Exception as e:
                log_warn(f"failed to parse type at {inf.address}: {e}")
                continue
            if isinstance(inf, ReturnType):
                return_type = parsed_type
                changed = True
                log_debug(
                    f"batched return type at {inf.address} -> {annotation}"
                )
            elif isinstance(inf, ParameterType):
                if inf.parameter_index >= len(params):
                    log_debug(
                        f"parameter index {inf.parameter_index} out of range at {inf.address}"
                    )
                    continue
                old = params[inf.parameter_index]
                params[inf.parameter_index] = FunctionParameter(
                    parsed_type, old.name, old.location
                )
                changed = True
                log_debug(
                    f"batched param[{inf.parameter_index}] type at {inf.address} -> {annotation}"
                )
        if changed:
            try:
                func.set_user_type(Type.function(return_type, params))
                log_info(
                    f"set function types at {hex(addr)}: {[type(i).__name__ for i in inferences]}"
                )
            except Exception as e:
                log_warn(f"failed to set function type at {hex(addr)}: {e}")


def _get_function_at(
    bv: BinaryView,
    address: int,
    fn_cache: dict[int, BnFunction | None] | None = None,
) -> BnFunction | None:
    if fn_cache is not None and address in fn_cache:
        return fn_cache[address]
    funcs = bv.get_functions_at(address)
    if not funcs:
        result: BnFunction | None = None
    else:
        bv_platform = bv.platform
        result = None
        if bv_platform is not None:
            for f in funcs:
                if f.platform == bv_platform:
                    result = f
                    break
        if result is None:
            result = funcs[0]
    if fn_cache is not None:
        fn_cache[address] = result
    return result


@_register(Name, "address")
def _apply_name(
    bv: BinaryView,
    inf: Name,
    fn_cache: dict[int, BnFunction | None] | None = None,
) -> None:
    address = int(inf.address, 16)
    func = _get_function_at(bv, address, fn_cache)
    if func is not None:
        func.name = inf.name
        log_info(f"renamed function {inf.address} -> {inf.name}")
        return
    data_var = bv.get_data_var_at(address)
    if data_var is not None:
        data_var.name = inf.name
        log_info(f"renamed data variable {inf.address} -> {inf.name}")
        return
    log_debug(
        f"no function or data variable at {inf.address} for Name inference"
    )


@_register(FunctionOverview, "address")
def _apply_overview(
    bv: BinaryView,
    inf: FunctionOverview,
    fn_cache: dict[int, BnFunction | None] | None = None,
) -> None:
    address = int(inf.address, 16)
    text = inf.full_description if inf.full_description else inf.overview
    func = _get_function_at(bv, address, fn_cache)
    if func is None:
        log_debug(f"no function at {inf.address} for FunctionOverview")
        return

    func.comment = text
    log_debug(f"set function overview at {func.start}")


@_register(ParametersMapping, "address")
def _apply_parameters_mapping(
    bv: BinaryView,
    inf: ParametersMapping,
    fn_cache: dict[int, BnFunction | None] | None = None,
) -> None:
    address = int(inf.address, 16)
    func = _get_function_at(bv, address, fn_cache)
    if func is None:
        log_debug(f"no function at {inf.address} for ParametersMapping")
        return
    mapping = inf.parameters_mapping
    try:
        params = list(func.type.parameters)
        new_params = []
        renamed = []
        for p in params:
            new_name = mapping.get(p.name)
            if new_name is not None:
                new_params.append(
                    FunctionParameter(p.type, new_name, p.location)
                )
                renamed.append(f"{p.name}->{new_name}")
            else:
                new_params.append(p)
        if renamed:
            new_func_type = Type.function(func.return_type, new_params)
            func.set_user_type(new_func_type)
            log_info(
                f"renamed parameters at {inf.address}: {', '.join(renamed)}"
            )
    except Exception as e:
        log_warn(f"failed to apply parameters mapping at {inf.address}: {e}")


@_register(VariablesMapping, "address")
def _apply_variables_mapping(
    bv: BinaryView,
    inf: VariablesMapping,
    fn_cache: dict[int, BnFunction | None] | None = None,
) -> None:
    address = int(inf.address, 16)
    func = _get_function_at(bv, address, fn_cache)
    if func is None:
        log_debug(f"no function at {inf.address} for VariablesMapping")
        return
    mapping = inf.variables_mapping
    param_names = {v.name for v in func.parameter_vars.vars}
    renamed = []
    for var in func.vars:
        if var.name in param_names:
            continue
        new_name = mapping.get(var.name)
        if new_name is None:
            continue
        if var.type is None:
            continue
        try:
            func.create_user_var(var, var.type, new_name)
            renamed.append(f"{var.name}->{new_name}")
        except SyntaxError:
            log_debug(
                f"type parse failed for {var.name!r} -> {new_name!r}, SKIP!"
            )
        except Exception as e:
            log_warn(
                f"failed to rename variable {var.name!r} -> {new_name!r} at {inf.address}: {e}"
            )
    if renamed:
        log_info(f"renamed variables at {inf.address}: {', '.join(renamed)}")


@_register(SwiftFunction, "address", undoable=False)
def _apply_swift_function(
    bv: BinaryView,
    inf: SwiftFunction,
    fn_cache: dict[int, BnFunction | None] | None = None,
) -> None:
    if inf.profile != TranslationProfile.BALANCED:
        log_info(
            f"skipping SwiftFunction at {inf.address} with profile {inf.profile}"
        )
        return
    addr = int(inf.address, 16)
    func = _get_function_at(bv, addr, fn_cache)
    if func is None:
        log_debug(f"no function at {inf.address} for SwiftFunction")
        return
    func.store_metadata(SWIFT_FUNCTION_METADATA_KEY, inf.to_dict())
    log_info(f"stored Swift source for function at {inf.address}")


@_register(NotSwift, "address", undoable=False)
def _apply_not_swift(
    bv: BinaryView,
    inf: NotSwift,
    fn_cache: dict[int, BnFunction | None] | None = None,
) -> None:
    addr = int(inf.address, 16)
    func = _get_function_at(bv, addr, fn_cache)
    if func is None:
        log_debug(f"no function at {inf.address} for NotSwift")
        return
    func.store_metadata(NOT_SWIFT_FUNCTION_METADATA_KEY, inf.to_dict())
    log_info(f"stored NotSwift ({inf.reason}) at {inf.address}")
