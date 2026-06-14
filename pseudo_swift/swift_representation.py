# zenyard_binja/swift_representation.py
from __future__ import annotations

import textwrap
import typing as ty
import collections
from contextlib import nullcontext


from binaryninja import (  # type: ignore[import]
    Architecture,
    DisassemblySettings,
    DisassemblyTextLine,
    Function,
    HighLevelILFunction,
    HighLevelILInstruction,
    HighLevelILOperation,
    HighLevelILTokenEmitter,
    InstructionTextToken,
    InstructionTextTokenType,
    LanguageRepresentationFunction,
    LanguageRepresentationFunctionType,
    OperatorPrecedence,
)

from .swift_pygments import _tokenize_swift_line
from ..zenyard_client.models import SwiftFunction
from ..zenyard_client.models.line_mapping import LineMapping
from ..zenyard_client.models.not_swift import NotSwift
from ..zenyard_client.models.swift_rejection_reason import SwiftRejectionReason
from ..zenyard_client.models.swift_speculation import SwiftSpeculation
from ..helpers.log import log_debug, log_warn


_BANNER = "// Swift analysis not yet available for this function"

_NOT_SWIFT_BANNERS: dict[SwiftRejectionReason, str] = {
    SwiftRejectionReason.TOO_SHORT: (
        "// Not analyzed as Swift: function is too short"
    ),
    SwiftRejectionReason.HAS_NON_SWIFT_NAME: (
        "// Not analyzed as Swift: non-Swift symbol name"
    ),
    SwiftRejectionReason.COMPILER_GENERATED: (
        "// Not analyzed as Swift: likely compiler-generated"
    ),
    SwiftRejectionReason.DOESNT_LOOK_LIKE_SWIFT: (
        "// Not analyzed as Swift: not conclusively Swift"
    ),
    SwiftRejectionReason.INITIAL_SWIFT_ANALYSIS_DISABLED: (
        "// Not analyzed as Swift: initial Swift analysis disabled"
    ),
}
_UNALIGNED_HEADER = "// Pseudo Swift (no per-instruction address mapping)"
_SPECULATION_WRAP_WIDTH = 80


def _build_body_source(source: str) -> str:
    """Replace the declaration prefix with empty lines so body line numbers stay stable.

    The line containing ``{`` keeps its content from ``{`` onward. Lines that were
    entirely declaration become empty strings.
    """
    brace_idx = source.find("{")
    if brace_idx < 0:
        return ""
    pre_newlines = source.count("\n", 0, brace_idx)
    return "\n" * pre_newlines + source[brace_idx:]


def _load_swift_function_for(func: Function) -> SwiftFunction | None:
    """Look up the persisted SwiftFunction inference for ``func``, if any."""
    try:
        raw = func.view.query_metadata("zenyard.swift_inferences")
    except KeyError:
        return None
    except Exception as e:
        log_warn(
            f"Pseudo Swift: query_metadata error for {hex(func.start)}: {e}"
        )
        return None
    if not raw or not isinstance(raw, dict):
        return None
    entry = raw.get(str(func.start))
    if entry is None:
        return None
    swift_func = SwiftFunction.from_dict(entry)
    if swift_func is None:
        log_warn(
            f"Pseudo Swift: SwiftFunction.from_dict returned None for {hex(func.start)}"
        )
        return None
    return swift_func


def _load_not_swift_for(func: Function) -> NotSwift | None:
    """Look up the persisted NotSwift inference for ``func``, if any."""
    try:
        raw = func.view.query_metadata("zenyard.not_swift_inferences")
    except KeyError:
        return None
    except Exception as e:
        log_warn(
            f"Pseudo Swift: query_metadata error for {hex(func.start)}: {e}"
        )
        return None
    if not raw or not isinstance(raw, dict):
        return None
    entry = raw.get(str(func.start))
    if entry is None:
        return None
    not_swift = NotSwift.from_dict(entry)
    if not_swift is None:
        log_warn(
            f"Pseudo Swift: NotSwift.from_dict returned None for {hex(func.start)}"
        )
        return None
    return not_swift


def _parse_line_id(line_id: str) -> int | None:
    if line_id in ("header", "tail"):
        return None
    addr_part = line_id.split("-", 1)[0]
    try:
        return int(addr_part, 16)
    except ValueError:
        return None


def _build_line_anchor(
    source: str,
    line_mappings: list[LineMapping],
) -> tuple[dict[int, int], list[tuple[int, str]]]:
    """Build the per-Swift-line anchor map and the trailing-tail line list.

    Maps each Swift output line (1-indexed) to the machine address its
    ``first_input_line_id`` points at, for click-to-jump tagging. Each
    ``LineMapping``'s declared range is also filled in across the
    intermediate Swift lines so gaps between explicit mappings adopt the
    previous mapping's anchor.

    Lines anchored to ``tail`` are returned separately so the caller can
    emit them after the HLIL_BLOCK body walk.
    """
    if not line_mappings:
        return {}, []

    source_lines = source.splitlines()
    sorted_mappings = sorted(line_mappings, key=lambda m: m.first_inferred_line)
    line_anchor: dict[int, int] = {}
    tail_lines: list[tuple[int, str]] = []

    for i, mapping in enumerate(sorted_mappings):
        start_line = mapping.first_inferred_line  # 1-indexed
        end_line = (
            sorted_mappings[i + 1].first_inferred_line
            if i + 1 < len(sorted_mappings)
            else len(source_lines) + 1
        )

        if mapping.first_input_line_id == "tail":
            # Tail represents the function's single closing-brace line. Don't
            # extend it to end-of-source — when the LLM emits extra top-level
            # declarations after the function body, those lines would
            # otherwise be swept into the tail.
            idx = start_line - 1
            if 0 <= idx < len(source_lines) and source_lines[idx]:
                tail_lines.append((start_line, source_lines[idx]))
            continue

        address = _parse_line_id(mapping.first_input_line_id)
        if address is None:
            continue
        for line_num in range(start_line, end_line):
            line_anchor[line_num] = address

    return line_anchor, tail_lines


def _build_speculations(
    speculations: list[SwiftSpeculation] | None,
) -> tuple[dict[int, list[str]], list[str]]:
    if not speculations:
        return {}, []
    line_map: dict[int, list[str]] = collections.defaultdict(list)
    footnotes: list[str] = []
    for idx, spec in enumerate(speculations, 1):
        label = f"[{idx}]"
        footnotes.append(f"{label} {spec.description}")
        for line_num in spec.source_line_numbers:
            line_map[line_num].append(label)
    return line_map, footnotes


def _emit_unaligned_source_tokens(
    source_lines: list[str],
    function_start: int,
    tokens: HighLevelILTokenEmitter,
) -> None:
    """Mode B emit: dump whole Swift source as syntax-colored tokens pinned to function entry."""
    tokens.append(
        InstructionTextToken(
            InstructionTextTokenType.CommentToken,
            _UNALIGNED_HEADER,
            address=function_start,
        )
    )
    tokens.new_line()
    for line_num, line_text in enumerate(source_lines, start=1):
        if not line_text:
            continue
        for tok_text, tok_type in _tokenize_swift_line(line_text):
            tokens.append(
                InstructionTextToken(tok_type, tok_text, address=function_start)
            )
        tokens.new_line()


class PseudoSwiftLanguageRepresentationFunction(LanguageRepresentationFunction):
    comment_start_string = "// "

    def __init__(
        self,
        type_obj: "PseudoSwiftLanguageRepresentationType",
        arch: Architecture,
        owner: Function,
        hlil: HighLevelILFunction,
    ) -> None:
        super().__init__(type_obj, arch, owner, hlil)
        self._source_lines: list[str] = []
        self._line_anchor: dict[int, int] = {}
        self._speculation_map: dict[int, list[str]] = {}
        self._footnotes: list[str] = []
        self._tail_lines: list[tuple[int, str]] = []
        self._addr_to_hlil: dict[int, HighLevelILInstruction] = {}
        self._load_attempted: bool = False
        self._start_address: int = 0
        self._not_swift_reason: SwiftRejectionReason | None = None
        self._load_swift_source(owner)

    def _load_swift_source(self, owner: Function) -> None:
        self._load_attempted = True
        swift_func = _load_swift_function_for(owner)
        if swift_func is None:
            not_swift = _load_not_swift_for(owner)
            if not_swift is not None:
                self._not_swift_reason = not_swift.reason
                self._start_address = owner.start
            return
        self._start_address = owner.start

        # Strip the declaration prefix from the source rendered into the body —
        # the declaration is now drawn in the prototype slot by
        # PseudoSwiftLanguageRepresentationType.function_type_tokens.
        body_source = _build_body_source(swift_func.source)
        self._speculations = [
            f"[{idx}] {spec.description}"
            for idx, spec in enumerate(swift_func.speculations or [], 1)
        ]

        self._source_lines = body_source.splitlines()
        self._line_anchor, self._tail_lines = _build_line_anchor(
            body_source, swift_func.line_mappings
        )
        self._speculation_map, self._footnotes = _build_speculations(
            swift_func.speculations
        )
        self._addr_to_hlil = {
            instr.address: instr for instr in self.hlil.instructions
        }
        sample_ids = [
            m.first_input_line_id for m in swift_func.line_mappings[:5]
        ]
        log_debug(
            f"Pseudo Swift: loaded {len(self._line_anchor)} line anchors"
            f" for {hex(owner.start)}"
            f" (mappings={len(swift_func.line_mappings)},"
            f" body_lines={len(self._source_lines)},"
            f" sample_ids={sample_ids})"
        )

    def perform_get_expr_text(
        self,
        instr: HighLevelILInstruction,
        tokens: HighLevelILTokenEmitter,
        settings: ty.Optional[DisassemblySettings],
        precedence: OperatorPrecedence = OperatorPrecedence.TopLevelOperatorPrecedence,
        statement: bool = False,
    ) -> None:
        with tokens.expr(instr):
            if instr.operation != HighLevelILOperation.HLIL_BLOCK:
                return

            # Top anchor: emit a zero-width token at func.start before any body
            # lines so BN attaches the FunctionOverview address comment
            # (set via func.set_comment_at(func.start, ...) in apply_inferences)
            # to the top of the body instead of trailing the closing brace.
            tokens.append(
                InstructionTextToken(
                    InstructionTextTokenType.TextToken,
                    "",
                    address=self._start_address,
                )
            )
            tokens.new_line()

            if not self._line_anchor:
                if self._source_lines:
                    _emit_unaligned_source_tokens(
                        self._source_lines,
                        self.function.start,
                        tokens,
                    )
                    return

                banner = (
                    _NOT_SWIFT_BANNERS.get(self._not_swift_reason, _BANNER)
                    if self._not_swift_reason is not None
                    else _BANNER
                )
                tokens.append(
                    InstructionTextToken(
                        InstructionTextTokenType.CommentToken,
                        banner,
                        address=instr.address,
                    )
                )
                return

            for line_num, line_text in enumerate(self._source_lines, start=1):
                anchor = self._line_anchor.get(line_num, None)
                if anchor is None:
                    continue

                if not line_text:
                    tokens.new_line()
                    continue

                line_instr = self._addr_to_hlil.get(anchor)
                expr_ctx = (
                    tokens.expr(line_instr)
                    if line_instr is not None
                    else nullcontext()
                )
                with expr_ctx:
                    for tok_text, tok_type in _tokenize_swift_line(line_text):
                        tokens.append(
                            InstructionTextToken(
                                tok_type, tok_text, address=anchor
                            )
                        )
                    speculation = self._speculation_map.get(line_num)
                    if speculation:
                        spec = ",".join(speculation)
                        tokens.append(
                            InstructionTextToken(
                                InstructionTextTokenType.CommentToken,
                                f"  {spec}",
                                address=anchor,
                            )
                        )
                    tokens.new_line()
            self._emit_tail_lines(tokens, instr.address)

    def _emit_speculation_footnotes(
        self, tokens: HighLevelILTokenEmitter, anchor_addr: int
    ):
        for i, spec in enumerate(self._footnotes):
            if i > 0:
                tokens.append(
                    InstructionTextToken(
                        InstructionTextTokenType.CommentToken,
                        "",
                        address=anchor_addr,
                    )
                )
                tokens.new_line()
            wrapped_lines = textwrap.wrap(
                spec,
                width=_SPECULATION_WRAP_WIDTH,
                subsequent_indent="  ",
            ) or [""]
            for line in wrapped_lines:
                tokens.append(
                    InstructionTextToken(
                        InstructionTextTokenType.CommentToken,
                        line,
                        address=anchor_addr,
                    )
                )
                tokens.new_line()

    def _emit_tail_lines(
        self, tokens: HighLevelILTokenEmitter, anchor_addr: int
    ) -> None:
        # Closing brace lines from the Swift source.
        for _, line_text in self._tail_lines:
            if not line_text:
                continue
            for tok_text, tok_type in _tokenize_swift_line(line_text):
                tokens.append(
                    InstructionTextToken(
                        tok_type, tok_text, address=anchor_addr
                    )
                )
            tokens.new_line()

        self._emit_speculation_footnotes(tokens, anchor_addr)


class PseudoSwiftLanguageRepresentationType(LanguageRepresentationFunctionType):
    language_name = "Zenyard Swift"

    def create(
        self,
        arch: Architecture,
        owner: Function,
        hlil: HighLevelILFunction,
    ) -> PseudoSwiftLanguageRepresentationFunction:
        return PseudoSwiftLanguageRepresentationFunction(
            self, arch, owner, hlil
        )

    def function_type_tokens(
        self,
        func: Function,
        settings: ty.Optional[DisassemblySettings],
    ) -> list[DisassemblyTextLine]:
        tokens = []
        tokens.append(
            InstructionTextToken(InstructionTextTokenType.KeywordToken, "func ")
        )
        for tok_text, tok_type in _tokenize_swift_line(func.name):
            tokens.append(
                InstructionTextToken(tok_type, tok_text, address=func.start)
            )

        return [DisassemblyTextLine(tokens=tokens, address=func.start)]
