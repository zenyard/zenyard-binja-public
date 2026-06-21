import typing as ty
from itertools import groupby

from binaryninja import LinearViewObject, LinearViewCursor  # type: ignore[import]
from binaryninja import LinearDisassemblyLine, LinearDisassemblyLineType  # type: ignore[import]
from binaryninja import InstructionTextTokenType, SectionSemantics  # type: ignore[import]
from binaryninja import BinaryView, Function as BNFunction  # type: ignore[import]
from .helpers.log import log_debug, log_warn

from .zenyard_client.models import (
    AddressDetail,
    Function,
    GlobalVariable,
    LineRange,
    LVarDetail,
    Range,
    Section as ApiSection,
    Thunk,
    RangeDetail,
)


_SKIP_TOKENS = {
    InstructionTextTokenType.TagToken,
    InstructionTextTokenType.AddressDisplayToken,
    InstructionTextTokenType.AddressSeparatorToken,
    InstructionTextTokenType.CollapseStateIndicatorToken,
}

# Token types whose `value` field carries the address of a referenced
# binary entity (function, global, import, external symbol, or a literal
# Binary Ninja already classified as likely-an-address). Tokens of these
# types produce one AddressDetail range covering the token text, addressed
# by token.value. Other token types — including IntegerToken,
# CharacterConstantToken, RegisterToken, FloatingPointToken, etc. — also
# carry a non-zero `value` for unrelated reasons (integer literal value,
# character code, register index, …), so we must filter by type rather
# than by `value != 0` alone.
_ADDRESS_TOKEN_TYPES = {
    InstructionTextTokenType.CodeSymbolToken,
    InstructionTextTokenType.DataSymbolToken,
    InstructionTextTokenType.ImportToken,
    InstructionTextTokenType.IndirectImportToken,
    InstructionTextTokenType.ExternalSymbolToken,
    InstructionTextTokenType.PossibleAddressToken,
}


def _line_to_text(line: LinearDisassemblyLine) -> str:
    if line.type == LinearDisassemblyLineType.FunctionHeaderEndLineType:
        return "{"
    if line.type == LinearDisassemblyLineType.FunctionEndLineType:
        return "}"
    return "".join(
        t.text for t in line.contents.tokens if t.type not in _SKIP_TOKENS
    )


def _line_to_id(line: LinearDisassemblyLine) -> str:
    if line.type == LinearDisassemblyLineType.FunctionEndLineType:
        return "tail"
    il = line.contents.il_instruction
    if il is None:
        return "header"
    return f"{line.contents.address:x}-{il.expr_index}"


def _ids_to_line_ranges(ids: ty.Iterable[str]) -> ty.Iterator[LineRange]:
    for key, group in groupby(ids):
        yield LineRange(id=key, line_count=sum(1 for _ in group))


def _line_to_ranges(
    line: LinearDisassemblyLine,
    char_offset: int,
    param_names: set[str],
) -> list[Range]:
    if line.type in (
        LinearDisassemblyLineType.FunctionHeaderEndLineType,
        LinearDisassemblyLineType.FunctionEndLineType,
    ):
        return []

    tokens = [t for t in line.contents.tokens if t.type not in _SKIP_TOKENS]

    ranges: list[Range] = []
    pos = 0
    for token in tokens:
        text_len = len(token.text)
        if token.type == InstructionTextTokenType.LocalVariableToken:
            ranges.append(
                Range(
                    start=char_offset + pos,
                    length=text_len,
                    detail=RangeDetail(
                        LVarDetail(
                            name=token.text,
                            is_arg=token.text in param_names,
                        )
                    ),
                )
            )
        elif token.type in _ADDRESS_TOKEN_TYPES and token.value != 0:
            ranges.append(
                Range(
                    start=char_offset + pos,
                    length=text_len,
                    detail=RangeDetail(
                        AddressDetail(address=f"{token.value:016x}")
                    ),
                )
            )
        pos += text_len

    return ranges


def seq_number_for_cursor(cursor: int | None) -> int | None:
    return cursor - 1 if cursor is not None else None


def extract_one_function(
    bv: BinaryView,
    addr: int,
    inference_seq_number: int | None = None,
    calls: list[str] | None = None,
) -> Function | None:
    """Extract a single upload-ready ``Function`` at ``addr``.

    Returns ``None`` when there is nothing to upload there (no function at the
    address, a thunk, or no HLIL). ``calls`` may be supplied precomputed (e.g.
    the call graph already walked while planning) to avoid a second data-ref
    pass; when ``None`` it is computed via ``_get_calls``.
    """
    func = bv.get_function_at(addr)
    if func is None:
        return None
    if func.is_thunk:
        return None
    if func.hlil is None:
        log_warn(f"Zenyard: skipping {func.name} — no HLIL")
        return None

    func_address = f"{func.start:016x}"
    param_names = {v.name for v in func.parameter_vars.vars}

    lvo = LinearViewObject.single_function_hlil(func)
    cursor = LinearViewCursor(lvo)

    code_text_lines: list[str] = []
    all_ranges: list[Range] = []
    line_ids: list[str] = []
    char_offset = 0

    while cursor.valid:
        done = False
        for line in cursor.lines:
            text = _line_to_text(line)
            all_ranges.extend(_line_to_ranges(line, char_offset, param_names))
            code_text_lines.append(text)
            line_ids.append(_line_to_id(line))
            char_offset += len(text) + 1  # +1 for \n between lines
            if line.type == LinearDisassemblyLineType.FunctionEndLineType:
                done = True
                break
        if done:
            break
        cursor.next()

    if calls is None:
        calls = _get_calls(bv, func)
    has_known_name = not func.name.startswith("sub_")
    data_refs_to = list(
        dict.fromkeys(f"{ref:016x}" for ref in bv.get_data_refs(func.start))
    )

    sym = func.symbol
    mangled_name = sym.raw_name if has_known_name and sym is not None else None
    func_to_analyze = Function(
        address=func_address,
        name=func.name,
        code="\n".join(code_text_lines),
        calls=calls,
        has_known_name=has_known_name,
        data_refs_to=data_refs_to,
        ranges=all_ranges,
        line_ranges=list(_ids_to_line_ranges(line_ids)),
        analyze_as_swift=None,  # LLM decides
        decompiler_notes=[],
        mangled_name=mangled_name,
        inference_seq_number=inference_seq_number,
    )

    if func_to_analyze.line_ranges is not None:
        covered_lines = sum(
            range.line_count for range in func_to_analyze.line_ranges
        )
        code_lines = func_to_analyze.code.rstrip("\n").count("\n") + 1
        if covered_lines != code_lines:
            log_debug(
                f"Code has {code_lines} lines but ranges cover {covered_lines} lines"
            )

    log_debug(f"function to upload: {func_to_analyze.to_dict()}")
    log_debug(f"line ids for func: {line_ids}")
    return func_to_analyze


def extract_thunks(
    bv: BinaryView, inference_seq_number: int | None = None
) -> list[Thunk]:
    results = []
    for func in bv.functions:
        if not func.is_thunk:
            continue
        callees = func.callees
        if not callees:
            log_debug(f"Zenyard: skipping thunk {func.name} — no callee")
            continue
        results.append(
            Thunk(
                address=f"{func.start:016x}",
                name=func.name,
                target=f"{callees[0].start:016x}",
                has_known_name=not func.name.startswith("sub_"),
                inference_seq_number=inference_seq_number,
            )
        )
    log_debug(f"Zenyard: extracted {len(results)} thunks")
    return results


def extract_one_global(
    bv: BinaryView,
    addr: int,
    function_addresses: set[int],
    inference_seq_number: int | None = None,
) -> GlobalVariable | None:
    """Extract a single upload-ready ``GlobalVariable`` at ``addr``.

    Returns ``None`` when there is nothing to upload there (no data var, an
    address that is actually a function, or an unnamed var).
    ``function_addresses`` is supplied by the caller so it is computed once
    rather than per global. IgnoredSections filtering is the caller's job —
    ``BringUpTask._plan_objects`` drops ignored addresses before extraction.
    """
    var = bv.get_data_var_at(addr)
    if var is None:
        return None
    if var.address in function_addresses:
        return None

    name = var.name
    if not name:
        return None
    uses = list(
        dict.fromkeys(
            f"{f.start:016x}"
            for ref in bv.get_code_refs(var.address)
            for f in bv.get_functions_containing(ref.address)
        )
    )
    has_known_name = not name.startswith("data_")
    sym = var.symbol
    mangled_name = sym.raw_name if has_known_name and sym is not None else None
    g = GlobalVariable(
        address=f"{var.address:016x}",
        name=name,
        uses=uses,
        has_known_name=has_known_name,
        mangled_name=mangled_name,
        inference_seq_number=inference_seq_number,
    )
    log_debug(f"global variable to upload {g.to_dict()}")
    return g


def global_user_addrs(bv: BinaryView, addr: int) -> set[int]:
    """Start addresses of functions that reference the global at ``addr``.

    The order-insensitive ``set[int]`` counterpart of ``extract_one_global``'s
    ``uses`` field — used only to build function→global ordering edges. Kept
    separate from the payload so the payload's code-ref order is never
    disturbed (which would churn the global's content hash).
    """
    return {
        f.start
        for ref in bv.get_code_refs(addr)
        for f in bv.get_functions_containing(ref.address)
    }


def extract_globals(
    bv: BinaryView,
    only_addrs: set[int] | None = None,
    inference_seq_number: int | None = None,
) -> list[GlobalVariable]:
    function_addresses = {func.start for func in bv.functions}
    addrs = (
        only_addrs
        if only_addrs is not None
        else [var.address for var in bv.data_vars.values()]
    )
    results = [
        gl
        for addr in addrs
        if (
            gl := extract_one_global(
                bv, addr, function_addresses, inference_seq_number
            )
        )
        is not None
    ]
    log_debug(f"Zenyard: extracted {len(results)} globals")
    return results


def extract_sections(bv: BinaryView) -> list[ApiSection]:
    results = []
    seen_addresses: set[str] = set()
    for sec in bv.sections.values():
        semantics = sec.semantics
        if semantics == SectionSemantics.ReadOnlyCodeSectionSemantics:
            class_ = "code"
            read, write, execute = True, False, True
        elif semantics in (
            SectionSemantics.ReadOnlyDataSectionSemantics,
            SectionSemantics.ReadWriteDataSectionSemantics,
        ):
            class_ = "data"
            read = True
            write = semantics == SectionSemantics.ReadWriteDataSectionSemantics
            execute = False
        else:
            class_ = "other"
            read, write, execute = True, False, False
        address = f"{sec.start:016x}"
        if address in seen_addresses:
            log_debug(
                f"Zenyard: skipping duplicate section {sec.name} at {address}"
            )
            continue
        seen_addresses.add(address)
        results.append(
            ApiSection(
                address=address,
                name=sec.name,
                size=sec.length,
                class_=class_,
                read=read,
                write=write,
                execute=execute,
                has_known_name=True,
            )
        )
    log_debug(f"Zenyard: extracted {len(results)} sections")
    return results


def partition_addrs(
    bv: BinaryView, addrs: ty.Iterable[int]
) -> tuple[list[int], list[int], list[int]]:
    """Split addresses into (function, global, thunk) by what lives there.

    A thunk is a function with ``is_thunk`` set; thunk addresses are returned
    separately so callers can accept-and-skip them. Addresses that resolve to
    neither a function nor a data var are dropped.
    """
    fns: list[int] = []
    gls: list[int] = []
    thunks: list[int] = []
    for a in addrs:
        func = bv.get_function_at(a)
        if func is not None:
            (thunks if func.is_thunk else fns).append(a)
        elif bv.get_data_var_at(a) is not None:
            gls.append(a)
    return fns, gls, thunks


def call_target_addrs(bv: BinaryView, func: BNFunction) -> set[int]:
    """Addresses ``func`` reaches: direct callees + function-pointer takings.

    Direct calls (``func.callees``) plus any function whose address is taken as
    a data ref anywhere in the body. Self-edges are excluded. Returns raw int
    addresses so callers can topo-sort or stash them cheaply; ``_get_calls``
    wraps this for the hex-string ``calls`` payload.
    """
    results: set[int] = set()
    # Direct calls (what func.callees already gives you)
    for callee in func.callees:
        if callee.start != func.start:
            results.add(callee.start)

    # Function-pointer takings: a taken address is an instruction operand, i.e.
    # a *code* ref (adrp/add on AArch64, lea on x86) — get_data_refs_from over a
    # code body returns nothing. get_data_refs_from still catches function
    # pointers embedded as data within the body (e.g. literal pools).
    for ref in (
        *bv.get_code_refs_from(func.start, length=func.total_bytes),
        *bv.get_data_refs_from(func.start, length=func.total_bytes),
    ):
        target = bv.get_function_at(ref)
        if target is not None and target.start != func.start:
            results.add(target.start)
    return results


def _get_calls(bv: BinaryView, func: BNFunction) -> list[str]:
    return sorted(f"{a:016x}" for a in call_target_addrs(bv, func))
