from __future__ import annotations

import typing as ty

from ._hints import READ_ONLY, WRITE_IDEMPOTENT

from .._resolve import resolve_symbol_address
from ..server import bn_write


def register_navigation_tools(mcp: ty.Any, bv: ty.Any) -> None:
    """Wire navigation/bookmark tools onto the given FastMCP instance.

    Faithful port of BinAssistMCP's get_current_address / get_current_function
    and the bookmarks tool. ``bv.offset`` is a UI concept; in a headless
    per-BinaryView server these typically report the entry-point fallback.
    """

    @mcp.tool(annotations=READ_ONLY)
    def get_current_address() -> dict[str, ty.Any]:
        """Get the current address/offset in the binary view, with context."""
        if not hasattr(bv, "offset"):
            if bv.entry_points:
                current_addr = bv.entry_points[0]
            elif bv.functions:
                current_addr = next(iter(bv.functions)).start
            else:
                current_addr = bv.start

            return {
                "address": hex(current_addr),
                "decimal": current_addr,
                "note": (
                    "No current offset available, showing entry point or "
                    "start address"
                ),
                "has_current_offset": False,
            }

        current_addr = bv.offset

        result: dict[str, ty.Any] = {
            "address": hex(current_addr),
            "decimal": current_addr,
            "has_current_offset": True,
        }

        functions = bv.get_functions_containing(current_addr)
        if functions:
            func = functions[0]
            result["in_function"] = {
                "name": func.name,
                "start": hex(func.start),
                "end": hex(func.start + func.total_bytes),
                "offset_in_function": current_addr - func.start,
            }
        else:
            result["in_function"] = None

        symbol = bv.get_symbol_at(current_addr)
        if symbol:
            result["symbol"] = {"name": symbol.name, "type": str(symbol.type)}
        else:
            result["symbol"] = None

        for segment in bv.segments:
            if segment.start <= current_addr < segment.end:
                result["segment"] = {
                    "start": hex(segment.start),
                    "end": hex(segment.end),
                    "readable": segment.readable,
                    "writable": segment.writable,
                    "executable": segment.executable,
                }
                break
        else:
            result["segment"] = None

        try:
            disasm = bv.get_disassembly(current_addr)
            result["disassembly"] = disasm if disasm else None
        except Exception:  # noqa: BLE001
            result["disassembly"] = None

        return result

    @mcp.tool(annotations=READ_ONLY)
    def get_current_function() -> dict[str, ty.Any]:
        """Get the function containing the current address."""
        if not hasattr(bv, "offset"):
            return {
                "error": "No current offset available",
                "has_current_offset": False,
            }

        current_addr = bv.offset
        functions = bv.get_functions_containing(current_addr)

        if not functions:
            return {
                "current_address": hex(current_addr),
                "function": None,
                "message": "Current address is not within any function",
            }

        func = functions[0]
        result: dict[str, ty.Any] = {
            "current_address": hex(current_addr),
            "function": {"name": func.name, "address": hex(func.start)},
        }

        if len(functions) > 1:
            result["note"] = (
                f"Multiple functions at this address ({len(functions)} total)"
            )

        return result

    @mcp.tool(annotations=WRITE_IDEMPOTENT)
    def bookmarks(
        action: str,
        address: str | None = None,
        comment: str | None = None,
    ) -> str:
        """Manage bookmarks (tag-based): list, set, or remove.

        Args:
            action: Operation: 'list', 'set', or 'remove'.
            address: Address (hex string) or symbol name for set/remove.
            comment: Comment text for set.

        Returns:
            A human-readable status string.
        """
        if action == "list":
            results: list[str] = []
            for tt_name in bv.tag_types:
                tt = bv.tag_types[tt_name]
                tagged = bv.get_all_tags_of_type(tt)
                for addr, tag in tagged:
                    func = bv.get_function_at(addr)
                    func_label = f" [{func.name}]" if func else ""
                    results.append(
                        f"{hex(addr)}{func_label} ({tt_name}): {tag.data}"
                    )
            return "\n".join(results) if results else "No bookmarks found"

        if action == "set":

            def do_set() -> str:
                if not address:
                    return "Error: address required for set"
                addr = resolve_symbol_address(bv, address)
                if addr is None:
                    return f"Error: cannot resolve '{address}'"
                tt = bv.tag_types.get("Bookmarks")
                if tt is None:
                    tt = bv.create_tag_type("Bookmarks", "⭐")
                text = comment or "Bookmark"
                tag = bv.create_tag(tt, text, True)
                func = bv.get_function_at(addr)
                if func:
                    func.add_user_address_tag(addr, tag)
                else:
                    bv.add_tag(addr, tag, True)
                return f"Bookmark set at {hex(addr)}: {text}"

            return bn_write(do_set)

        if action == "remove":

            def do_remove() -> str:
                if not address:
                    return "Error: address required for remove"
                addr = resolve_symbol_address(bv, address)
                if addr is None:
                    return f"Error: cannot resolve '{address}'"
                removed = 0
                tt = bv.tag_types.get("Bookmarks")
                if tt:
                    for tag in bv.get_tags_at(addr):
                        if tag.type == tt:
                            bv.remove_user_data_tag(addr, tag)
                            removed += 1
                    func = bv.get_function_at(addr)
                    if func:
                        for tag in func.get_address_tags_at(addr):
                            if tag.type == tt:
                                func.remove_user_address_tag(addr, tag)
                                removed += 1
                return (
                    f"Removed {removed} bookmark(s) at {hex(addr)}"
                    if removed
                    else f"No bookmarks at {hex(addr)}"
                )

            return bn_write(do_remove)

        return f"Invalid action '{action}'. Use 'list', 'set', or 'remove'"
