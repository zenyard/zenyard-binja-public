from __future__ import annotations

import typing as ty

from ._hints import READ_ONLY

from .._resolve import resolve_function, resolve_symbol_address
from ..function_signature_generator import (
    BinaryNinjaFunctionSignatureGenerator,
)


def _cyclomatic_complexity(func: ty.Any) -> int:
    """Cyclomatic complexity = E - N + 2 (single connected component)."""
    edges = sum(len(bb.outgoing_edges) for bb in func.basic_blocks)
    nodes = len(list(func.basic_blocks))
    return edges - nodes + 2


def _count_loops(func: ty.Any) -> int:
    """Count loops via back-edge heuristic (mirrors BinAssistMCP)."""
    loop_count = 0
    visited: set[int] = set()
    for bb in func.basic_blocks:
        for edge in bb.outgoing_edges:
            target = getattr(edge, "target", None)
            if target is None:
                continue
            if target.start <= bb.start and target.start not in visited:
                loop_count += 1
        visited.add(bb.start)
    return loop_count


def _variable_type_safe(func: ty.Any, var: ty.Any) -> str:
    """Best-effort variable type string with fallbacks."""
    try:
        if hasattr(func, "get_variable_type"):
            var_type = func.get_variable_type(var)
            return str(var_type) if var_type else "unknown"
        if hasattr(var, "type") and var.type:
            return str(var.type)
    except Exception:  # noqa: BLE001
        return "unknown"
    return "unknown"


def _call_target_functions(bv: ty.Any, call_site: ty.Any) -> list[ty.Any]:
    """Resolve function call targets for a call site."""
    targets: list[ty.Any] = []
    seen: set[int] = set()

    for attr in ("destination", "dest", "function", "callee"):
        target = getattr(call_site, attr, None)
        if target is None:
            continue
        target_func = None
        if hasattr(target, "start") and hasattr(target, "name"):
            target_func = target
        elif isinstance(target, int):
            target_func = bv.get_function_at(target)
        if target_func is not None and target_func.start not in seen:
            seen.add(target_func.start)
            targets.append(target_func)

    site_addr = getattr(call_site, "address", None)
    if site_addr is None:
        return targets

    for target_addr in bv.get_code_refs_from(site_addr):
        target_func = bv.get_function_at(target_addr)
        if target_func is not None and target_func.start not in seen:
            seen.add(target_func.start)
            targets.append(target_func)
    return targets


def register_code_tools(mcp: ty.Any, bv: ty.Any) -> None:
    """Wire the code-category tools onto the given FastMCP instance."""

    @mcp.tool(annotations=READ_ONLY)
    def get_code(
        function_name_or_address: str, format: str = "decompile"
    ) -> dict[str, ty.Any]:
        """Get function code in specified format (unified tool).

        Formats: 'decompile', 'hlil', 'mlil', 'llil', 'disasm', 'pseudo_c'.
        """
        func = resolve_function(bv, function_name_or_address)
        result: dict[str, ty.Any] = {
            "function": func.name,
            "address": hex(func.start),
            "format": format,
            "code": None,
        }

        if format == "decompile":
            if getattr(func, "hlil", None):
                result["code"] = str(func.hlil)
            elif getattr(func, "mlil", None):
                result["code"] = str(func.mlil)
            else:
                lines: list[str] = []
                for block in func.basic_blocks:
                    for i in range(block.start, block.end):
                        disasm = bv.get_disassembly(i)
                        if disasm:
                            lines.append(f"{hex(i)}: {disasm}")
                result["code"] = "\n".join(lines)

        elif format == "hlil":
            if getattr(func, "hlil", None):
                lines = []
                for block in func.hlil:
                    for instr in block:
                        lines.append(str(instr))
                result["code"] = "\n".join(lines)
            else:
                result["code"] = "HLIL not available for this function"

        elif format == "mlil":
            if getattr(func, "mlil", None):
                lines = []
                for block in func.mlil:
                    for instr in block:
                        lines.append(str(instr))
                result["code"] = "\n".join(lines)
            else:
                result["code"] = "MLIL not available for this function"

        elif format == "llil":
            if getattr(func, "llil", None):
                lines = []
                for block in func.llil:
                    for instr in block:
                        lines.append(f"{hex(instr.address)}: {instr}")
                result["code"] = "\n".join(lines)
            else:
                result["code"] = "LLIL not available for this function"

        elif format == "disasm":
            lines = []
            for block in func.basic_blocks:
                for i in range(block.start, block.end):
                    disasm = bv.get_disassembly(i)
                    if disasm:
                        lines.append(f"{hex(i)}: {disasm}")
            result["code"] = "\n".join(lines)

        elif format == "pseudo_c":
            if getattr(func, "hlil", None):
                code_lines: list[str] = []
                if func.parameter_vars:
                    params = ", ".join(
                        f"{_variable_type_safe(func, p)} {p.name}"
                        for p in func.parameter_vars
                    )
                else:
                    params = "void"
                return_type = (
                    str(func.return_type) if func.return_type else "void"
                )
                code_lines.append(f"{return_type} {func.name}({params}) {{")
                for block in func.hlil:
                    for instr in block:
                        code_lines.append(f"    {instr}")
                code_lines.append("}")
                result["code"] = "\n".join(code_lines)
            else:
                result["code"] = "Pseudo-C not available (HLIL unavailable)"

        else:
            raise ValueError(
                f"Unknown format: {format}. "
                "Valid: decompile, hlil, mlil, llil, disasm, pseudo_c"
            )

        return result

    @mcp.tool(annotations=READ_ONLY)
    def get_function_low_level_il(function_name_or_address: str) -> str:
        """Get Low Level IL for a function."""
        func = resolve_function(bv, function_name_or_address)
        if getattr(func, "llil", None):
            lines: list[str] = []
            for block in func.llil:
                for instr in block:
                    lines.append(f"{hex(instr.address)}: {instr}")
            return "\n".join(lines)
        return "LLIL not available for this function"

    @mcp.tool(annotations=READ_ONLY)
    def analyze_function(
        function_name_or_address: str,
    ) -> dict[str, ty.Any]:
        """Perform comprehensive analysis of a function."""
        func = resolve_function(bv, function_name_or_address)

        analysis: dict[str, ty.Any] = {
            "name": func.name,
            "address": hex(func.start),
            "size": func.total_bytes,
            "basic_block_count": len(list(func.basic_blocks)),
            "instruction_count": sum(len(bb) for bb in func.basic_blocks),
            "parameter_count": len(func.parameter_vars),
            "local_variable_count": len(func.vars) - len(func.parameter_vars),
            "complexity": {
                "cyclomatic": _cyclomatic_complexity(func),
                "call_depth": len(list(func.call_sites)),
            },
        }

        analysis["control_flow"] = {
            "entry_point": hex(func.start),
            "exit_points": [
                hex(bb.end)
                for bb in func.basic_blocks
                if len(bb.outgoing_edges) == 0
            ],
            "branch_count": sum(
                1 for bb in func.basic_blocks if len(bb.outgoing_edges) > 1
            ),
            "loop_count": _count_loops(func),
        }

        calls_to: list[str] = []
        for call_site in func.call_sites:
            try:
                if hasattr(call_site, "address"):
                    for called_func in _call_target_functions(bv, call_site):
                        calls_to.append(called_func.name)
            except Exception:  # noqa: BLE001
                continue

        analysis["calls"] = {
            "outgoing": calls_to,
            "incoming": [caller.name for caller in func.callers],
            "external_calls": [
                call
                for call in calls_to
                if call.startswith("sub_") or "@" in call
            ],
        }

        analysis["types"] = {
            "return_type": (
                str(func.return_type) if func.return_type else "void"
            ),
            "parameters": [
                {"name": p.name, "type": _variable_type_safe(func, p)}
                for p in func.parameter_vars
            ],
        }
        return analysis

    @mcp.tool(annotations=READ_ONLY)
    def get_function_signature(
        function_name_or_address: str,
    ) -> dict[str, ty.Any]:
        """Get the native byte signature for a function.

        Returns the masked-prefix byte signature (matching BinAssistMCP), as
        ``{name, address, signature}``.
        """
        func = resolve_function(bv, function_name_or_address)
        generator = BinaryNinjaFunctionSignatureGenerator(bv)
        return {
            "name": func.name,
            "address": hex(func.start),
            "signature": generator.generate(func),
        }

    @mcp.tool(annotations=READ_ONLY)
    def get_basic_blocks(
        function_name_or_address: str,
    ) -> list[dict[str, ty.Any]]:
        """Get basic blocks for a function (control flow graph)."""
        func = resolve_function(bv, function_name_or_address)
        blocks: list[dict[str, ty.Any]] = []
        for block in func.basic_blocks:
            block_info: dict[str, ty.Any] = {
                "start": hex(block.start),
                "end": hex(block.end),
                "length": getattr(block, "length", block.end - block.start),
                "instruction_count": getattr(block, "instruction_count", 0),
                "successors": [],
                "predecessors": [],
            }
            for edge in block.outgoing_edges:
                target = getattr(edge, "target", None)
                if target is not None:
                    block_info["successors"].append(
                        {
                            "address": hex(target.start),
                            "type": str(getattr(edge, "type", "")),
                        }
                    )
            for edge in getattr(block, "incoming_edges", []):
                source = getattr(edge, "source", None)
                if source is not None:
                    block_info["predecessors"].append(
                        {"address": hex(source.start)}
                    )
            blocks.append(block_info)
        return blocks

    @mcp.tool(annotations=READ_ONLY)
    def get_function_stack_layout(
        function_name_or_address: str,
    ) -> dict[str, ty.Any]:
        """Get stack frame layout for a function."""
        func = resolve_function(bv, function_name_or_address)
        result: dict[str, ty.Any] = {
            "function": func.name,
            "address": hex(func.start),
            "stack_variables": [],
            "total_local_size": 0,
        }
        for var in getattr(func, "stack_layout", []):
            result["stack_variables"].append(
                {
                    "name": var.name,
                    "offset": var.storage,
                    "type": _variable_type_safe(func, var),
                }
            )
        adj = getattr(func, "stack_adjustment", None)
        if adj is not None:
            try:
                result["total_local_size"] = int(adj)
            except (TypeError, ValueError):
                result["total_local_size"] = 0
        return result

    @mcp.tool(annotations=READ_ONLY)
    def search_bytes(
        pattern: str,
        start_address: str = "",
        max_results: int = 100,
    ) -> list[dict[str, ty.Any]]:
        """Search the binary for a byte pattern; returns matches up to max_results."""
        clean_pattern = pattern.replace(" ", "").replace("0x", "")
        try:
            search_target = bytes.fromhex(clean_pattern)
        except ValueError:
            raise ValueError(f"Invalid hex pattern: {pattern}")

        start = bv.start
        if start_address:
            resolved = resolve_symbol_address(bv, start_address)
            if resolved:
                start = resolved

        results: list[dict[str, ty.Any]] = []
        current_addr = start
        while len(results) < max_results:
            found = bv.find_next_data(current_addr, search_target)
            if found is None:
                break
            context_data = bv.read(found, min(16, len(search_target) + 8))
            context_hex = context_data.hex() if context_data else ""
            funcs = list(bv.get_functions_containing(found))
            func_name = funcs[0].name if funcs else None
            results.append(
                {
                    "address": hex(found),
                    "context_hex": context_hex,
                    "function": func_name,
                }
            )
            current_addr = found + 1
        return results
