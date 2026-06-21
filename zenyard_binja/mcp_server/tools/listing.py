from __future__ import annotations

import typing as ty

from ._hints import READ_ONLY


def _cyclomatic_complexity(func: ty.Any) -> int:
    """Cyclomatic complexity = E - N + 2 (single connected component)."""
    edges = sum(len(bb.outgoing_edges) for bb in func.basic_blocks)
    nodes = len(list(func.basic_blocks))
    return edges - nodes + 2


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


def register_listing_tools(mcp: ty.Any, bv: ty.Any) -> None:
    """Wire the listing-category tools onto the given FastMCP instance."""

    @mcp.tool(annotations=READ_ONLY)
    def list_functions() -> list[dict[str, ty.Any]]:
        """List every function in the bound binary, with name and start address."""
        return [{"name": f.name, "start": int(f.start)} for f in bv.functions]

    @mcp.tool(annotations=READ_ONLY)
    def get_functions() -> list[dict[str, ty.Any]]:
        """Get list of all functions in the binary."""
        return [
            {
                "name": func.name,
                "address": hex(func.start),
                "size": func.total_bytes,
                "symbol_type": (str(func.symbol.type) if func.symbol else None),
                "parameter_count": len(func.parameter_vars),
                "return_type": (
                    str(func.return_type) if func.return_type else None
                ),
                "basic_block_count": len(list(func.basic_blocks)),
            }
            for func in bv.functions
        ]

    @mcp.tool(annotations=READ_ONLY)
    def search_functions_by_name(search_term: str) -> list[dict[str, ty.Any]]:
        """Search functions by name substring."""
        if not search_term:
            return []
        matches: list[dict[str, ty.Any]] = []
        for func in bv.functions:
            if search_term.lower() in func.name.lower():
                matches.append(
                    {
                        "name": func.name,
                        "address": hex(func.start),
                        "symbol_type": (
                            str(func.symbol.type) if func.symbol else None
                        ),
                    }
                )
        matches.sort(key=lambda x: x["name"])
        return matches

    @mcp.tool(annotations=READ_ONLY)
    def get_functions_advanced(
        name_filter: str = "",
        min_size: int = 0,
        max_size: int = 0,
        has_parameters: bool = False,
        sort_by: str = "address",
        limit: int = 0,
    ) -> list[dict[str, ty.Any]]:
        """Get functions with advanced filtering and search capabilities."""
        name_filter_val = name_filter if name_filter else None
        min_size_val = min_size if min_size > 0 else None
        max_size_val = max_size if max_size > 0 else None
        has_parameters_val = has_parameters if has_parameters else None
        limit_val = limit if limit > 0 else None

        functions: list[dict[str, ty.Any]] = []
        for func in bv.functions:
            if (
                name_filter_val
                and name_filter_val.lower() not in func.name.lower()
            ):
                continue
            if min_size_val is not None and func.total_bytes < min_size_val:
                continue
            if max_size_val is not None and func.total_bytes > max_size_val:
                continue
            if has_parameters_val is not None:
                func_has_params = len(func.parameter_vars) > 0
                if has_parameters_val != func_has_params:
                    continue
            functions.append(
                {
                    "name": func.name,
                    "address": hex(func.start),
                    "size": func.total_bytes,
                    "parameter_count": len(func.parameter_vars),
                    "basic_block_count": len(list(func.basic_blocks)),
                    "complexity": _cyclomatic_complexity(func),
                    "call_count": len(list(func.call_sites)),
                    "caller_count": len(list(func.callers)),
                    "return_type": (
                        str(func.return_type) if func.return_type else "void"
                    ),
                }
            )

        if sort_by == "name":
            functions.sort(key=lambda x: x["name"].lower())
        elif sort_by == "size":
            functions.sort(key=lambda x: x["size"], reverse=True)
        elif sort_by == "complexity":
            functions.sort(key=lambda x: x["complexity"], reverse=True)
        else:
            functions.sort(key=lambda x: int(x["address"], 16))

        if limit_val is not None:
            functions = functions[:limit_val]
        return functions

    @mcp.tool(annotations=READ_ONLY)
    def search_functions_advanced(
        search_term: str,
        search_in: str = "name",
        case_sensitive: bool = False,
    ) -> list[dict[str, ty.Any]]:
        """Advanced function search with multiple search targets."""
        if not search_term:
            return []
        search_lower = search_term if case_sensitive else search_term.lower()

        matches: list[dict[str, ty.Any]] = []
        for func in bv.functions:
            match_found = False
            match_reason: list[str] = []

            if search_in in ("name", "all"):
                func_name = func.name if case_sensitive else func.name.lower()
                if search_lower in func_name:
                    match_found = True
                    match_reason.append("name")

            if search_in in ("comment", "all"):
                if func.comment:
                    comment = (
                        func.comment if case_sensitive else func.comment.lower()
                    )
                    if search_lower in comment:
                        match_found = True
                        match_reason.append("comment")

            if search_in in ("calls", "all"):
                for call_site in func.call_sites:
                    try:
                        if hasattr(call_site, "address"):
                            for called_func in _call_target_functions(
                                bv, call_site
                            ):
                                called_name = (
                                    called_func.name
                                    if case_sensitive
                                    else called_func.name.lower()
                                )
                                if search_lower in called_name:
                                    match_found = True
                                    match_reason.append("calls")
                                    break
                    except Exception:  # noqa: BLE001
                        continue

            if search_in in ("variables", "all"):
                for var in func.vars:
                    var_name = var.name if case_sensitive else var.name.lower()
                    if search_lower in var_name:
                        match_found = True
                        match_reason.append("variables")
                        break

            if match_found:
                matches.append(
                    {
                        "name": func.name,
                        "address": hex(func.start),
                        "size": func.total_bytes,
                        "match_reason": match_reason,
                        "comment": func.comment if func.comment else None,
                    }
                )

        matches.sort(
            key=lambda x: (
                0 if "name" in x["match_reason"] else 1,
                x["name"].lower(),
            )
        )
        return matches

    @mcp.tool(annotations=READ_ONLY)
    def get_function_statistics() -> dict[str, ty.Any]:
        """Get comprehensive statistics about all functions in the binary."""
        functions = list(bv.functions)
        if not functions:
            return {"error": "No functions found in binary"}

        sizes = [func.total_bytes for func in functions]
        complexities = [_cyclomatic_complexity(func) for func in functions]
        param_counts = [len(func.parameter_vars) for func in functions]
        bb_counts = [len(list(func.basic_blocks)) for func in functions]

        return {
            "total_functions": len(functions),
            "size_statistics": {
                "min": min(sizes),
                "max": max(sizes),
                "average": sum(sizes) / len(sizes),
                "total": sum(sizes),
            },
            "complexity_statistics": {
                "min": min(complexities),
                "max": max(complexities),
                "average": sum(complexities) / len(complexities),
            },
            "parameter_statistics": {
                "min": min(param_counts),
                "max": max(param_counts),
                "average": sum(param_counts) / len(param_counts),
                "functions_with_params": sum(
                    1 for count in param_counts if count > 0
                ),
            },
            "basic_block_statistics": {
                "min": min(bb_counts),
                "max": max(bb_counts),
                "average": sum(bb_counts) / len(bb_counts),
                "total": sum(bb_counts),
            },
            "top_largest_functions": [
                {
                    "name": func.name,
                    "address": hex(func.start),
                    "size": func.total_bytes,
                }
                for func in sorted(
                    functions, key=lambda f: f.total_bytes, reverse=True
                )[:10]
            ],
            "top_most_complex_functions": [
                {
                    "name": func.name,
                    "address": hex(func.start),
                    "complexity": _cyclomatic_complexity(func),
                }
                for func in sorted(
                    functions,
                    key=lambda f: _cyclomatic_complexity(f),
                    reverse=True,
                )[:10]
            ],
        }
