from __future__ import annotations

import typing as ty

from ._hints import READ_ONLY, WRITE_IDEMPOTENT

import binaryninja as bn  # type: ignore[import]

from .._resolve import resolve_symbol_address
from ..server import bn_write


def register_symbols_tools(mcp: ty.Any, bv: ty.Any) -> None:
    """Wire the symbols tools onto the given FastMCP instance.

    Faithful port of BinAssistMCP's symbol-namespace and symbol-write tools.
    """

    def _rename_symbol(address_or_name: str, new_name: str) -> str:
        """Rename a function or data variable. Mirrors BinAssistMCP.rename_symbol."""
        addr = resolve_symbol_address(bv, address_or_name)
        if addr is None:
            raise ValueError(
                "No function or data variable found with name/address "
                f"'{address_or_name}'"
            )

        # Try to rename function
        func = bv.get_function_at(addr)
        if func:
            old_name = func.name
            func.name = new_name
            return (
                f"Successfully renamed function at {hex(addr)} "
                f"from '{old_name}' to '{new_name}'"
            )

        # Try to rename data variable
        if addr in bv.data_vars:
            var = bv.data_vars[addr]
            old_name = var.symbol.name if var.symbol else "unnamed"
            bv.define_user_symbol(
                bn.Symbol(bn.SymbolType.DataSymbol, addr, new_name)
            )
            return (
                f"Successfully renamed data variable at {hex(addr)} "
                f"from '{old_name}' to '{new_name}'"
            )

        raise ValueError(
            f"No function or data variable found at address {hex(addr)}"
        )

    @mcp.tool(annotations=READ_ONLY)
    def get_namespaces() -> list[dict[str, ty.Any]]:
        """Get all namespaces in the binary.

        Returns a list of ``{namespace, symbol_count, symbols}`` dicts, where
        each symbol is ``{name, address, type}``.
        """
        namespaces: dict[str, list[dict[str, ty.Any]]] = {}

        for sym_list in bv.symbols.values():
            for symbol in sym_list:
                ns_obj = symbol.namespace
                ns = str(ns_obj) if ns_obj else "global"
                namespaces.setdefault(ns, []).append(
                    {
                        "name": symbol.name,
                        "address": hex(symbol.address),
                        "type": str(symbol.type),
                    }
                )

        return [
            {
                "namespace": ns_name,
                "symbol_count": len(symbols),
                "symbols": symbols,
            }
            for ns_name, symbols in namespaces.items()
        ]

    @mcp.tool(annotations=WRITE_IDEMPOTENT)
    def rename_symbol(address_or_name: str, new_name: str) -> str:
        """Rename a function or data variable; performed on the BN main thread.

        Args:
            address_or_name: Address (hex string) or name of the symbol.
            new_name: New name for the symbol.

        Returns:
            Success message string.
        """
        return bn_write(lambda: _rename_symbol(address_or_name, new_name))

    @mcp.tool(annotations=WRITE_IDEMPOTENT)
    def rename_global_variable(address_or_name: str, new_name: str) -> str:
        """Rename a non-function global/data symbol; on the BN main thread.

        Args:
            address_or_name: Address (hex string) or current global/data symbol
                name.
            new_name: New name for the symbol.

        Returns:
            Success message string.
        """

        def do() -> str:
            addr = resolve_symbol_address(bv, address_or_name)
            if addr is None:
                raise ValueError(
                    "No global/data symbol found with name/address "
                    f"'{address_or_name}'"
                )

            if bv.get_function_at(addr):
                raise ValueError(
                    f"Target '{address_or_name}' resolves to a function at "
                    f"{hex(addr)}; use rename_variable for locals or "
                    "rename_symbol for functions"
                )

            symbol = bv.get_symbol_at(addr)
            old_name = symbol.name if symbol else "unnamed"
            symbol_type = symbol.type if symbol else bn.SymbolType.DataSymbol

            bv.define_user_symbol(bn.Symbol(symbol_type, addr, new_name))
            return (
                f"Successfully renamed global symbol at {hex(addr)} "
                f"from '{old_name}' to '{new_name}'"
            )

        return bn_write(do)

    @mcp.tool(annotations=WRITE_IDEMPOTENT)
    def batch_rename(renames: list[dict]) -> list[dict[str, ty.Any]]:
        """Batch rename multiple symbols; performed on the BN main thread.

        Each entry is a dict with ``address_or_name`` and ``new_name``. Returns
        one result dict per entry; per-entry errors are captured rather than
        aborting the batch.

        Args:
            renames: List of ``{address_or_name, new_name}`` dicts.

        Returns:
            List of result dicts ``{address_or_name, new_name, success,
            message|error}``.
        """

        def do() -> list[dict[str, ty.Any]]:
            results: list[dict[str, ty.Any]] = []
            for rename in renames:
                address_or_name = rename.get("address_or_name", "")
                new_name = rename.get("new_name", "")

                if not address_or_name or not new_name:
                    results.append(
                        {
                            "address_or_name": address_or_name,
                            "success": False,
                            "error": "Missing address_or_name or new_name",
                        }
                    )
                    continue

                try:
                    message = _rename_symbol(address_or_name, new_name)
                    results.append(
                        {
                            "address_or_name": address_or_name,
                            "new_name": new_name,
                            "success": True,
                            "message": message,
                        }
                    )
                except Exception as e:  # noqa: BLE001
                    results.append(
                        {
                            "address_or_name": address_or_name,
                            "success": False,
                            "error": str(e),
                        }
                    )
            return results

        return bn_write(do)
