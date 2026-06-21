from __future__ import annotations

import typing as ty

from ._hints import READ_ONLY, WRITE_IDEMPOTENT

from .._resolve import resolve_function, resolve_symbol_address
from ..server import bn_write


def _set_comment_at(bv: ty.Any, addr: int, comment: str) -> None:
    """Set a comment at an address, preferring function-level comments.

    Mirrors BinAssistMCP: if the address is within a function, use
    ``function.set_comment_at()``; otherwise fall back to ``bv.set_comment_at()``.
    """
    funcs = bv.get_functions_containing(addr)
    if funcs:
        funcs[0].set_comment_at(addr, comment)
    else:
        bv.set_comment_at(addr, comment)


def _get_comment_at(bv: ty.Any, addr: int) -> str | None:
    """Get a comment at an address, checking function- then BV-level comments."""
    funcs = bv.get_functions_containing(addr)
    if funcs:
        comment = funcs[0].get_comment_at(addr)
        if comment:
            return comment
    return bv.get_comment_at(addr) or None


def _remove_comment_at(bv: ty.Any, addr: int) -> bool:
    """Remove an address comment from both function and BV levels.

    Returns True if a comment was removed, False if none existed.
    """
    removed = False
    funcs = bv.get_functions_containing(addr)
    if funcs:
        if funcs[0].get_comment_at(addr):
            funcs[0].set_comment_at(addr, "")
            removed = True
    if bv.get_comment_at(addr):
        bv.set_comment_at(addr, "")
        removed = True
    return removed


def register_comments_tools(mcp: ty.Any, bv: ty.Any) -> None:
    """Wire the comments tools onto the given FastMCP instance.

    Faithful port of BinAssistMCP's comment-handling methods as 5 atomic tools:
    get_comment, set_comment, list_comments, remove_comment, set_function_comment.
    """

    @mcp.tool(annotations=READ_ONLY)
    def get_comment(address: str) -> str | None:
        """Get the comment at a specific address.

        Checks both function-level and BV-level comments.

        Args:
            address: Address (hex string) or symbol name.

        Returns:
            The comment string, or None if no comment exists.
        """
        addr = resolve_symbol_address(bv, address)
        if addr is None:
            raise ValueError(f"Invalid address: {address}")
        return _get_comment_at(bv, addr)

    @mcp.tool(annotations=WRITE_IDEMPOTENT)
    def set_comment(address: str, text: str) -> str:
        """Set a comment at the specified address.

        Uses a function-level comment when the address is within a function.

        Args:
            address: Address (hex string) or symbol name.
            text: Comment text.

        Returns:
            Success message.
        """

        def do() -> str:
            addr = resolve_symbol_address(bv, address)
            if addr is None:
                raise ValueError(f"Invalid address: {address}")
            _set_comment_at(bv, addr, text)
            return f"Successfully set comment at {hex(addr)}: '{text}'"

        return bn_write(do)

    @mcp.tool(annotations=READ_ONLY)
    def list_comments() -> list[dict[str, ty.Any]]:
        """List all comments in the binary.

        Collects function-level comments, function instruction comments, and
        BV-level address comments. Returns ``{address, type, comment,
        function_name}`` dicts sorted by address.
        """
        comments: list[dict[str, ty.Any]] = []
        seen_addresses: set[int] = set()

        for func in bv.functions:
            if func.comment:
                comments.append(
                    {
                        "address": hex(func.start),
                        "type": "function",
                        "comment": func.comment,
                        "function_name": func.name,
                    }
                )

            for addr, comment in func.comments.items():
                if comment:
                    comments.append(
                        {
                            "address": hex(addr),
                            "type": "instruction",
                            "comment": comment,
                            "function_name": func.name,
                        }
                    )
                    seen_addresses.add(addr)

        for addr, comment in bv.address_comments.items():
            if addr not in seen_addresses and comment:
                funcs = bv.get_functions_containing(addr)
                func_name = funcs[0].name if funcs else None
                comments.append(
                    {
                        "address": hex(addr),
                        "type": "address",
                        "comment": comment,
                        "function_name": func_name,
                    }
                )

        comments.sort(key=lambda x: int(x["address"], 16))
        return comments

    @mcp.tool(annotations=WRITE_IDEMPOTENT)
    def remove_comment(address: str) -> str:
        """Remove the comment at a specific address (function- and BV-level).

        Args:
            address: Address (hex string) or symbol name.

        Returns:
            Success message.
        """

        def do() -> str:
            addr = resolve_symbol_address(bv, address)
            if addr is None:
                raise ValueError(f"Invalid address: {address}")
            if not _remove_comment_at(bv, addr):
                return f"No comment found at {hex(addr)}"
            return f"Successfully removed comment at {hex(addr)}"

        return bn_write(do)

    @mcp.tool(annotations=WRITE_IDEMPOTENT)
    def set_function_comment(function_name_or_address: str, text: str) -> str:
        """Set a comment for an entire function.

        Args:
            function_name_or_address: Function name or address.
            text: Comment text.

        Returns:
            Success message.
        """

        def do() -> str:
            func = resolve_function(bv, function_name_or_address)
            func.comment = text
            return (
                f"Successfully set comment for function '{func.name}': '{text}'"
            )

        return bn_write(do)
