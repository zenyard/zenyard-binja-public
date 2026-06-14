from __future__ import annotations

import typing as ty

from ._hints import ANALYSIS, READ_ONLY

import binaryninja as bn  # type: ignore[import]


def build_triage_summary(bv: ty.Any) -> dict[str, ty.Any]:
    """Binary triage summary: file metadata, binary info, and object counts."""
    return {
        "file_metadata": {
            "filename": bv.file.filename,
            "file_size": bv.length,
            "view_type": bv.view_type,
        },
        "binary_info": {
            "platform": str(bv.platform),
            "architecture": bv.arch.name if bv.arch else None,
            "entry_point": hex(bv.entry_point),
            "base_address": hex(bv.start),
            "end_address": hex(bv.end),
            "endianness": bv.endianness.name,
            "address_size": bv.address_size,
        },
        "statistics": {
            "function_count": len(list(bv.functions)),
            "string_count": len(list(bv.strings)),
            "segment_count": len(bv.segments),
            "section_count": len(bv.sections),
        },
    }


def register_binary_info_tools(mcp: ty.Any, bv: ty.Any) -> None:
    """Wire the binary-info tools onto the given FastMCP instance.

    Ports BinAssistMCP's triage-summary / imports / exports / strings /
    segments / sections / entry-points tools as atomic, read-only
    ``@mcp.tool()`` functions over the already-bound ``bv``.
    """

    @mcp.tool(annotations=READ_ONLY)
    def get_triage_summary() -> dict[str, ty.Any]:
        """Binary triage summary: file metadata, architecture/entry/
        endianness, and function/string/segment/section counts. Good first
        call for orientation on a freshly opened binary.
        """
        return build_triage_summary(bv)

    @mcp.tool(annotations=READ_ONLY)
    def get_imports() -> dict[str, list[dict[str, ty.Any]]]:
        """Get imported symbols grouped by module."""
        imports: dict[str, list[dict[str, ty.Any]]] = {}
        for sym_type in (
            bn.SymbolType.ImportedFunctionSymbol,
            bn.SymbolType.ImportedDataSymbol,
        ):
            for sym in bv.get_symbols_of_type(sym_type):
                module = sym.namespace or "unknown"
                imports.setdefault(module, []).append(
                    {
                        "name": sym.name,
                        "address": hex(sym.address),
                        "type": str(sym.type),
                        "ordinal": getattr(sym, "ordinal", None),
                    }
                )
        return imports

    @mcp.tool(annotations=READ_ONLY)
    def get_exports() -> list[dict[str, ty.Any]]:
        """Get exported (globally-bound) symbols."""
        exports: list[dict[str, ty.Any]] = []
        for sym_type in (
            bn.SymbolType.FunctionSymbol,
            bn.SymbolType.DataSymbol,
        ):
            for sym in bv.get_symbols_of_type(sym_type):
                if sym.binding == bn.SymbolBinding.GlobalBinding:
                    exports.append(
                        {
                            "name": sym.name,
                            "address": hex(sym.address),
                            "type": str(sym.type),
                            "ordinal": getattr(sym, "ordinal", None),
                        }
                    )
        return exports

    @mcp.tool(annotations=READ_ONLY)
    def get_strings(
        page_size: int = 100, page_number: int = 1
    ) -> dict[str, ty.Any]:
        """Get strings found in the binary with pagination."""
        all_strings = [
            {
                "value": s.value,
                "address": hex(s.start),
                "length": s.length,
                "type": str(s.type),
            }
            for s in bv.strings
        ]
        total_count = len(all_strings)
        start_idx = (page_number - 1) * page_size
        end_idx = start_idx + page_size
        return {
            "strings": all_strings[start_idx:end_idx],
            "page_size": page_size,
            "page_number": page_number,
            "total_count": total_count,
            "total_pages": (
                (total_count + page_size - 1) // page_size
                if page_size > 0
                else 0
            ),
        }

    @mcp.tool(annotations=READ_ONLY)
    def search_strings(
        pattern: str,
        case_sensitive: bool = False,
        page_size: int = 100,
        page_number: int = 1,
    ) -> dict[str, ty.Any]:
        """Search strings by substring match, with pagination."""
        search_pattern = pattern if case_sensitive else pattern.lower()
        results: list[dict[str, ty.Any]] = []
        for s in bv.strings:
            value = s.value
            compare_value = value if case_sensitive else value.lower()
            if search_pattern in compare_value:
                results.append(
                    {
                        "address": hex(s.start),
                        "value": value,
                        "length": s.length,
                        "type": str(s.type),
                    }
                )

        total_count = len(results)
        start_idx = (page_number - 1) * page_size
        end_idx = start_idx + page_size
        return {
            "strings": results[start_idx:end_idx],
            "page_size": page_size,
            "page_number": page_number,
            "total_count": total_count,
            "total_pages": (
                (total_count + page_size - 1) // page_size
                if page_size > 0
                else 0
            ),
        }

    @mcp.tool(annotations=READ_ONLY)
    def get_segments() -> list[dict[str, ty.Any]]:
        """List memory segments with permissions."""
        return [
            {
                "start": hex(s.start),
                "end": hex(s.end),
                "length": s.length,
                "readable": bool(s.readable),
                "writable": bool(s.writable),
                "executable": bool(s.executable),
                "data_offset": s.data_offset,
                "data_length": s.data_length,
            }
            for s in bv.segments
        ]

    @mcp.tool(annotations=READ_ONLY)
    def get_sections() -> list[dict[str, ty.Any]]:
        """List binary sections with name, range, and semantics."""
        return [
            {
                "name": s.name,
                "start": hex(s.start),
                "end": hex(s.end),
                "length": s.length,
                "type": s.type,
                "align": s.align,
                "entry_size": s.entry_size,
            }
            for s in bv.sections.values()
        ]

    @mcp.tool(annotations=READ_ONLY)
    def get_entry_points() -> list[dict[str, ty.Any]]:
        """List binary entry points (true entry plus global exports)."""
        result: list[dict[str, ty.Any]] = []
        entry = bv.entry_point
        if entry is not None:
            func = bv.get_function_at(entry)
            result.append(
                {
                    "address": hex(entry),
                    "name": func.name if func else "entry",
                    "type": "EntryPoint",
                }
            )
        for func in bv.functions:
            if func.start == (entry or 0):
                continue
            sym = bv.get_symbol_at(func.start)
            if sym and sym.binding == bn.SymbolBinding.GlobalBinding:
                result.append(
                    {
                        "address": hex(func.start),
                        "name": func.name,
                        "type": "Export",
                    }
                )
        return result

    @mcp.tool(annotations=READ_ONLY)
    def get_analysis_status() -> dict[str, ty.Any]:
        """Report Binary Ninja's analysis state for the bound binary.

        Worth calling before querying functions/code: the MCP server comes up
        as soon as the binary opens, so analysis may still be running and other
        tools can return partial or empty results until ``analysis_complete`` is
        true.
        """
        function_count = len(list(bv.functions))

        # Prefer analysis_progress (gives count/total); fall back to
        # analysis_info.state — both expose an AnalysisState.
        state = None
        count = 0
        total = 0
        progress = getattr(bv, "analysis_progress", None)
        if (
            progress is not None
            and getattr(progress, "state", None) is not None
        ):
            state = progress.state
            count = int(getattr(progress, "count", 0) or 0)
            total = int(getattr(progress, "total", 0) or 0)
        else:
            info = getattr(bv, "analysis_info", None)
            if info is not None:
                state = getattr(info, "state", None)

        complete = (
            state == bn.AnalysisState.IdleState if state is not None else None
        )
        result: dict[str, ty.Any] = {
            "state": str(state) if state is not None else "unknown",
            "analyzing": complete is False,
            "analysis_complete": bool(complete),
            "function_count": function_count,
        }
        if total > 0:
            result["progress_fraction"] = round(count / total, 4)

        if complete is None:
            result["message"] = (
                f"Could not determine analysis state; {function_count} "
                "functions discovered so far."
            )
        elif complete:
            result["message"] = (
                f"Analysis complete. {function_count} functions discovered."
            )
        else:
            result["message"] = (
                f"Analysis in progress ({result['state']}); {function_count} "
                "functions so far. Some tools may return incomplete results — "
                "re-check get_analysis_status before relying on them."
            )
        return result

    @mcp.tool(annotations=ANALYSIS)
    def update_analysis_and_wait() -> str:
        """Run analysis to completion, blocking until it finishes.

        Triggers Binary Ninja's analysis and returns only once all disassembly
        and analysis is complete. Use when you need fully-analyzed results and
        get_analysis_status reports analysis is still running.
        """
        bv.update_analysis_and_wait()
        return f"Analysis complete. {len(list(bv.functions))} functions discovered."
