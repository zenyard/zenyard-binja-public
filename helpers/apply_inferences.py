from __future__ import annotations

import typing as ty
import re
from contextlib import AbstractContextManager, nullcontext
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


@dataclass(frozen=True)
class _UserBaseline:
    name_is_user: bool
    had_comment: bool
    had_user_type: bool
    user_param_indices: frozenset[int]
    user_var_names: frozenset[str]


BaselineCache: ty.TypeAlias = dict[int, _UserBaseline]


def _capture_baseline(
    bv: BinaryView,
    address: int,
    func: BnFunction | None,
) -> _UserBaseline:
    if func is not None:
        sym = func.symbol
        return _UserBaseline(
            name_is_user=sym is not None and not sym.auto,
            had_comment=bool((func.comment or "").strip()),
            had_user_type=func.has_user_type,
            user_param_indices=frozenset(
                i
                for i, v in enumerate(func.parameter_vars.vars)
                if func.is_var_user_defined(v)
            ),
            user_var_names=frozenset(
                v.name for v in func.vars if func.is_var_user_defined(v)
            ),
        )
    # Data-variable address (Name inference) — only the symbol name matters.
    sym = bv.get_symbol_at(address)
    return _UserBaseline(
        name_is_user=sym is not None and not sym.auto,
        had_comment=False,
        had_user_type=False,
        user_param_indices=frozenset(),
        user_var_names=frozenset(),
    )


def _baseline_for(
    bv: BinaryView,
    address: int,
    func: BnFunction | None,
    cache: BaselineCache | None,
) -> _UserBaseline:
    """Return the capture-once baseline for ``address``, snapshotting it now if
    this is the first touch. ``cache`` is None only when a handler runs
    standalone (e.g. a unit test); then the baseline is captured live."""
    if cache is None:
        return _capture_baseline(bv, address, func)
    cached = cache.get(address)
    if cached is not None:
        return cached
    bl = _capture_baseline(bv, address, func)
    cache[address] = bl
    return bl


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
    apply: ty.Callable[..., bool]
    scope: ty.Literal["bv", "address"]
    undoable: bool = field(default=True)


_HANDLERS: dict[type, HandlerSpec] = {}


def _register(
    inf_type: type,
    scope: ty.Literal["bv", "address"],
    undoable: bool = True,
) -> ty.Callable[[ty.Callable[..., bool]], ty.Callable[..., bool]]:
    def decorator(fn: ty.Callable[..., bool]) -> ty.Callable[..., bool]:
        _HANDLERS[inf_type] = HandlerSpec(fn, scope, undoable)
        return fn

    return decorator


def apply_inferences(
    bv: BinaryView,
    inferences: ty.Iterable[InferenceItem],
    model: Model | None = None,
    baseline: BaselineCache | None = None,
) -> int:
    """Apply a batch of inferences to the BinaryView.

    Returns the number of inferences *processed* (handled by a registered
    handler) — including ones skipped because the user already owns that
    attribute, so the caller's progress count still reaches completion. Items
    with no registered handler are not counted.

    ``baseline`` is the run-scoped capture-before-write cache that lets us tell
    the user's pre-analysis edits from zenyard's own writes; pass the same dict
    across all pages of a single analysis. When omitted, a per-call cache is
    used (correct for one-shot/test calls, where every address is first-touched
    within the call anyway).

    Must be called on the main thread. The trailing ``update_analysis()`` only
    kicks off analysis; the caller then waits for it to drain on the background
    thread (BN forbids the UI thread from calling ``update_analysis_and_wait``).
    """
    if baseline is None:
        baseline = {}
    fn_cache: dict[int, BnFunction | None] = {}
    applied = 0
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
                ty.cast(AbstractContextManager[None], bv.undoable_transaction())
                if spec.undoable
                else nullcontext()
            )
            with ctx:
                changed = spec.apply(bv, inf, fn_cache, baseline)
            applied += 1
            if changed and not isinstance(inf, (SwiftFunction, NotSwift)):
                addr_str = getattr(inf, "address", None)
                if isinstance(addr_str, str):
                    try:
                        touched.add(int(addr_str, 16))
                    except ValueError:
                        pass

        for addr, type_infs in func_type_groups.items():
            changed = _apply_function_types_batch(
                bv, addr, type_infs, fn_cache, baseline
            )
            applied += len(type_infs)
            if changed:
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
    baseline: BaselineCache | None = None,
) -> bool:
    func = _get_function_at(bv, addr, fn_cache)
    if func is None:
        return False
    bl = _baseline_for(bv, addr, func, baseline)
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
                # No return-type-specific user flag exists; gate on whole-
                # signature user state (conservative: preserve the user's work).
                if bl.had_user_type:
                    log_debug(
                        f"skip return type at {inf.address}: user signature"
                    )
                    continue
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
                if inf.parameter_index in bl.user_param_indices:
                    log_debug(
                        f"skip param[{inf.parameter_index}] type at "
                        f"{inf.address}: user-defined"
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
                log_debug(
                    f"set function types at {hex(addr)}: {[type(i).__name__ for i in inferences]}"
                )
            except Exception as e:
                log_warn(f"failed to set function type at {hex(addr)}: {e}")
                return False
    return changed


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
    baseline: BaselineCache | None = None,
) -> bool:
    address = int(inf.address, 16)
    func = _get_function_at(bv, address, fn_cache)
    bl = _baseline_for(bv, address, func, baseline)
    if bl.name_is_user:
        log_debug(f"skip name at {inf.address}: user-defined")
        return False
    if func is not None:
        func.name = inf.name
        log_debug(f"renamed function {inf.address} -> {inf.name}")
        return True
    data_var = bv.get_data_var_at(address)
    if data_var is not None:
        data_var.name = inf.name
        log_debug(f"renamed data variable {inf.address} -> {inf.name}")
        return True
    log_info(
        f"no function or data variable at {inf.address} for Name inference"
    )
    return False


@_register(FunctionOverview, "address")
def _apply_overview(
    bv: BinaryView,
    inf: FunctionOverview,
    fn_cache: dict[int, BnFunction | None] | None = None,
    baseline: BaselineCache | None = None,
) -> bool:
    address = int(inf.address, 16)
    text = inf.full_description if inf.full_description else inf.overview
    func = _get_function_at(bv, address, fn_cache)
    if func is None:
        log_debug(f"no function at {inf.address} for FunctionOverview")
        return False

    bl = _baseline_for(bv, address, func, baseline)
    if bl.had_comment:
        # Comments are always user-owned (BN has no auto-comment concept), so a
        # pre-existing comment is the human's — don't clobber it.
        log_debug(f"skip overview at {inf.address}: user comment present")
        return False

    func.comment = text
    log_debug(f"set function overview at {func.start}")
    return True


@_register(ParametersMapping, "address")
def _apply_parameters_mapping(
    bv: BinaryView,
    inf: ParametersMapping,
    fn_cache: dict[int, BnFunction | None] | None = None,
    baseline: BaselineCache | None = None,
) -> bool:
    address = int(inf.address, 16)
    func = _get_function_at(bv, address, fn_cache)
    if func is None:
        log_debug(f"no function at {inf.address} for ParametersMapping")
        return False
    bl = _baseline_for(bv, address, func, baseline)
    mapping = inf.parameters_mapping
    try:
        params = list(func.type.parameters)
        new_params = []
        renamed = []
        for i, p in enumerate(params):
            if i in bl.user_param_indices:
                # User named this parameter — keep it untouched.
                new_params.append(p)
                continue
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
            log_debug(
                f"renamed parameters at {inf.address}: {', '.join(renamed)}"
            )
            return True
        return False
    except Exception as e:
        log_warn(f"failed to apply parameters mapping at {inf.address}: {e}")
        return False


@_register(VariablesMapping, "address")
def _apply_variables_mapping(
    bv: BinaryView,
    inf: VariablesMapping,
    fn_cache: dict[int, BnFunction | None] | None = None,
    baseline: BaselineCache | None = None,
) -> bool:
    address = int(inf.address, 16)
    func = _get_function_at(bv, address, fn_cache)
    if func is None:
        log_debug(f"no function at {inf.address} for VariablesMapping")
        return False
    bl = _baseline_for(bv, address, func, baseline)
    mapping = inf.variables_mapping
    param_names = {v.name for v in func.parameter_vars.vars}
    renamed = []
    for var in func.vars:
        if var.name in param_names:
            continue
        if var.name in bl.user_var_names:
            # User renamed/retyped this local — leave it alone.
            log_debug(
                f"skip variable {var.name!r} at {inf.address}: user-defined"
            )
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
        log_debug(f"renamed variables at {inf.address}: {', '.join(renamed)}")
        return True
    return False


@_register(SwiftFunction, "address", undoable=False)
def _apply_swift_function(
    bv: BinaryView,
    inf: SwiftFunction,
    fn_cache: dict[int, BnFunction | None] | None = None,
    baseline: BaselineCache | None = None,
) -> bool:
    if inf.profile != TranslationProfile.BALANCED:
        log_debug(
            f"skipping SwiftFunction at {inf.address} with profile {inf.profile}"
        )
        return False
    addr = int(inf.address, 16)
    func = _get_function_at(bv, addr, fn_cache)
    if func is None:
        log_debug(f"no function at {inf.address} for SwiftFunction")
        return False
    func.store_metadata(SWIFT_FUNCTION_METADATA_KEY, inf.to_dict())
    log_debug(f"stored Swift source for function at {inf.address}")
    return True


@_register(NotSwift, "address", undoable=False)
def _apply_not_swift(
    bv: BinaryView,
    inf: NotSwift,
    fn_cache: dict[int, BnFunction | None] | None = None,
    baseline: BaselineCache | None = None,
) -> bool:
    addr = int(inf.address, 16)
    func = _get_function_at(bv, addr, fn_cache)
    if func is None:
        log_debug(f"no function at {inf.address} for NotSwift")
        return False
    func.store_metadata(NOT_SWIFT_FUNCTION_METADATA_KEY, inf.to_dict())
    log_debug(f"stored NotSwift ({inf.reason}) at {inf.address}")
    return True
