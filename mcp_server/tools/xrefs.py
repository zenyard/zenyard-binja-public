from __future__ import annotations

import typing as ty

from ._hints import READ_ONLY

from .._resolve import (
    get_function_by_name_or_address,
    get_function_containing_or_at,
    resolve_symbol_address,
)


def register_xrefs_tools(mcp: ty.Any, bv: ty.Any) -> None:
    """Wire the xrefs-category tools onto the given FastMCP instance.

    Split of BinAssistMCP's unified ``xrefs`` tool into three atomic per-action
    tools, each matching the corresponding sub-shape of ``xrefs``:
    ``get_refs_to`` (references_to), ``get_refs_from`` (references_from),
    ``get_call_graph`` (call_graph).
    """

    @mcp.tool(annotations=READ_ONLY)
    def get_refs_to(address_or_name: str) -> list[dict[str, ty.Any]]:
        """List references that point at this address or symbol.

        Mirrors the ``references_to`` section of BinAssistMCP's ``xrefs``.
        """
        addr = resolve_symbol_address(bv, address_or_name)
        if addr is None:
            raise ValueError(f"Could not resolve: {address_or_name}")

        result: list[dict[str, ty.Any]] = []
        for ref in bv.get_code_refs(addr):
            ref_func = get_function_containing_or_at(bv, ref.address)
            result.append(
                {
                    "address": hex(ref.address),
                    "function": ref_func.name if ref_func else None,
                }
            )
        return result

    @mcp.tool(annotations=READ_ONLY)
    def get_refs_from(address_or_name: str) -> list[dict[str, ty.Any]]:
        """List references emanating from a function.

        Mirrors the ``references_from`` section of BinAssistMCP's ``xrefs``:
        walks every address in the function's basic blocks.
        """
        func = get_function_by_name_or_address(bv, address_or_name)
        if func is None:
            raise ValueError(f"Function not found: {address_or_name}")

        result: list[dict[str, ty.Any]] = []
        for block in func.basic_blocks:
            for i in range(block.start, block.end):
                for ref in bv.get_code_refs_from(i):
                    target_func = bv.get_function_at(ref)
                    result.append(
                        {
                            "from_address": hex(i),
                            "to_address": hex(ref),
                            "to_function": (
                                target_func.name if target_func else None
                            ),
                        }
                    )
        return result

    @mcp.tool(annotations=READ_ONLY)
    def get_call_graph(address_or_name: str) -> dict[str, ty.Any]:
        """One-level call graph for a function: its callers and callees.

        Mirrors the ``call_graph`` section of BinAssistMCP's ``xrefs``.
        """
        func = get_function_by_name_or_address(bv, address_or_name)
        if func is None:
            raise ValueError(f"Function not found: {address_or_name}")

        callers: list[dict[str, ty.Any]] = []
        for ref in bv.get_code_refs(func.start):
            caller_func = get_function_containing_or_at(bv, ref.address)
            if caller_func and caller_func != func:
                callers.append(
                    {
                        "name": caller_func.name,
                        "address": hex(caller_func.start),
                    }
                )

        callees: list[dict[str, ty.Any]] = []
        if hasattr(func, "callees"):
            for callee in func.callees:
                callees.append(
                    {"name": callee.name, "address": hex(callee.start)}
                )

        return {
            "function": func.name,
            "address": hex(func.start),
            "callers": callers,
            "callees": callees,
        }
