from __future__ import annotations

import typing as ty

from ._hints import READ_ONLY, WRITE_IDEMPOTENT, WRITE_VOLATILE

import binaryninja as bn  # type: ignore[import]

from .._resolve import resolve_function, resolve_symbol_address
from ..server import bn_write
from ...helpers.log import log_debug, log_error, log_info


def _variable_type_safe(func: ty.Any, var: ty.Any) -> str:
    """Best-effort variable type string with fallbacks (mirrors BinAssistMCP)."""
    try:
        if hasattr(func, "get_variable_type"):
            var_type = func.get_variable_type(var)
            return str(var_type) if var_type else "unknown"
        if hasattr(var, "type") and var.type:
            return str(var.type)
    except Exception:  # noqa: BLE001
        return "unknown"
    return "unknown"


def _var_ident(var: ty.Any) -> str:
    """Compact Variable identity for diagnostic logging."""
    return (
        f"name={var.name!r} source_type={var.source_type} "
        f"index={var.index} storage={var.storage} type={var.type}"
    )


def register_variables_tools(mcp: ty.Any, bv: ty.Any) -> None:
    """Wire the variables tools onto the given FastMCP instance.

    Faithful port of BinAssistMCP's variable and data-variable tools.
    """

    @mcp.tool(annotations=READ_ONLY)
    def get_data_vars() -> list[dict[str, ty.Any]]:
        """Get all data variables in the binary.

        Returns a list of data variables with address, type, size, and name,
        sorted by address.
        """
        data_vars: list[dict[str, ty.Any]] = []

        for addr, var in bv.data_vars.items():
            var_info: dict[str, ty.Any] = {
                "address": hex(addr),
                "type": str(var.type),
                "size": var.type.width if var.type else 0,
                "name": None,
            }
            symbol = bv.get_symbol_at(addr)
            if symbol:
                var_info["name"] = symbol.name
            data_vars.append(var_info)

        data_vars.sort(key=lambda x: int(x["address"], 16))
        return data_vars

    @mcp.tool(annotations=READ_ONLY)
    def get_data_at(address: str, size: int | None = None) -> dict[str, ty.Any]:
        """Get data at a specific address.

        Args:
            address: Address (hex string) or symbol name.
            size: Optional size to read (if not specified, uses data var size or
                default 16).

        Returns:
            Dictionary with address, size, raw hex, raw bytes, and interpreted
            integer/string values (and defined_type/symbol_name when present).
        """
        addr = resolve_symbol_address(bv, address)
        if addr is None:
            raise ValueError(f"Invalid address: {address}")

        # Determine size to read
        read_size = size
        if not read_size:
            if addr in bv.data_vars:
                var = bv.data_vars[addr]
                read_size = var.type.width if var.type else 16
            else:
                read_size = 16

        try:
            raw_data = bv.read(addr, read_size)
        except Exception as e:  # noqa: BLE001
            raise ValueError(
                f"Failed to read data at {hex(addr)}: {str(e)}"
            ) from e

        hex_data = " ".join(f"{b:02x}" for b in raw_data)

        result: dict[str, ty.Any] = {
            "address": hex(addr),
            "size": read_size,
            "raw_hex": hex_data,
            "raw_bytes": list(raw_data),
        }

        if len(raw_data) >= 4:
            try:
                result["as_uint32"] = int.from_bytes(
                    raw_data[:4], byteorder="little"
                )
                result["as_int32"] = int.from_bytes(
                    raw_data[:4], byteorder="little", signed=True
                )
            except Exception:  # noqa: BLE001
                pass

        if len(raw_data) >= 8:
            try:
                result["as_uint64"] = int.from_bytes(
                    raw_data[:8], byteorder="little"
                )
                result["as_int64"] = int.from_bytes(
                    raw_data[:8], byteorder="little", signed=True
                )
            except Exception:  # noqa: BLE001
                pass

        # Try to interpret as string
        try:
            null_pos = raw_data.find(0)
            str_data = raw_data[:null_pos] if null_pos != -1 else raw_data
            result["as_string"] = str_data.decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass

        # Check if there's a defined data variable
        if addr in bv.data_vars:
            var = bv.data_vars[addr]
            result["defined_type"] = str(var.type)
            symbol = bv.get_symbol_at(addr)
            if symbol:
                result["symbol_name"] = symbol.name

        return result

    @mcp.tool(annotations=READ_ONLY)
    def list_variables(
        function_name_or_address: str,
    ) -> list[dict[str, ty.Any]]:
        """List all variables (parameters and locals) of a function.

        Args:
            function_name_or_address: Function name or address.

        Returns:
            List of variables with name, type, category, storage, identifier.
        """
        func = resolve_function(bv, function_name_or_address)

        variables: list[dict[str, ty.Any]] = []

        for param in func.parameter_vars:
            variables.append(
                {
                    "name": param.name,
                    "type": _variable_type_safe(func, param),
                    "category": "parameter",
                    "storage": str(param.storage),
                    "identifier": str(param.identifier),
                }
            )

        for var in func.vars:
            if var not in func.parameter_vars:
                variables.append(
                    {
                        "name": var.name,
                        "type": _variable_type_safe(func, var),
                        "category": "local",
                        "storage": str(var.storage),
                        "identifier": str(var.identifier),
                    }
                )

        return variables

    @mcp.tool(annotations=WRITE_VOLATILE)
    def create_variable(
        function_name_or_address: str,
        var_name: str,
        var_type: str,
        storage: str = "auto",
    ) -> str:
        """Create a local variable in a function.

        Args:
            function_name_or_address: Function name or address.
            var_name: Variable name.
            var_type: Variable type (e.g., 'int32_t', 'char*').
            storage: Storage type ('auto', 'register', etc.).

        Returns:
            Success message.
        """

        def do() -> str:
            func = resolve_function(bv, function_name_or_address)

            try:
                parsed_type = bv.parse_type_string(var_type)[0]
            except Exception as e:  # noqa: BLE001
                raise ValueError(f"Invalid type '{var_type}': {str(e)}") from e

            var = bn.Variable.from_identifier(func, 0)

            try:
                func.create_user_var(var, parsed_type, var_name)
            except Exception as e:  # noqa: BLE001
                raise ValueError(f"Failed to create variable: {str(e)}") from e
            return (
                f"Successfully created variable '{var_name}' with type "
                f"'{var_type}' in function '{func.name}'"
            )

        return bn_write(do)

    @mcp.tool(annotations=WRITE_IDEMPOTENT)
    def rename_variable(
        function_name_or_address: str,
        old_name: str,
        new_name: str,
    ) -> str:
        """Rename a variable in a function.

        Args:
            function_name_or_address: Function name or address.
            old_name: Current variable name.
            new_name: New variable name.

        Returns:
            Success message.
        """

        def apply() -> None:
            func = resolve_function(bv, function_name_or_address)
            log_debug(
                f"rename_variable: func={func.name!r} old={old_name!r} "
                f"new={new_name!r}"
            )

            target_var = None
            for var in func.vars:
                if var.name == old_name:
                    target_var = var
                    break

            if target_var is None:
                names = [v.name for v in func.vars]
                log_error(
                    f"rename_variable: '{old_name}' not found in "
                    f"'{func.name}' ({len(names)} vars present)"
                )
                raise ValueError(
                    f"Variable '{old_name}' not found in function '{func.name}'"
                )

            log_debug(f"rename_variable: before -> {_var_ident(target_var)}")
            target_var.name = new_name

        def verify() -> str:
            after = resolve_function(bv, function_name_or_address)
            names_after = [v.name for v in after.vars]
            applied = new_name in names_after
            log_info(
                f"rename_variable: verify '{after.name}' applied={applied} "
                f"old_still_present={old_name in names_after}"
            )
            if not applied:
                log_error(
                    f"rename_variable: did NOT persist '{old_name}' -> "
                    f"'{new_name}' in '{after.name}' even after analysis "
                    f"update; the variable override did not take."
                )
                raise ValueError(
                    f"Rename of '{old_name}' to '{new_name}' did not persist "
                    f"in '{after.name}' (verified after analysis)."
                )
            return (
                f"Successfully renamed variable from '{old_name}' to "
                f"'{new_name}' in function '{after.name}'"
            )

        bn_write(apply)
        # The rename only materialises once analysis reprocesses the function.
        # Drive that to completion here on the calling (background) thread, not
        # inside the main-thread bn_write callback, so it actually finishes
        # instead of staying pending until the UI is nudged.
        bv.update_analysis_and_wait()
        return bn_write(verify)

    @mcp.tool(annotations=WRITE_IDEMPOTENT)
    def set_variable_type(
        function_name_or_address: str,
        var_name: str,
        var_type: str,
    ) -> str:
        """Set the type of a variable in a function.

        Args:
            function_name_or_address: Function name or address.
            var_name: Variable name.
            var_type: New variable type (e.g., 'int32_t', 'char*').

        Returns:
            Success message.
        """

        state: dict[str, str] = {}

        def apply() -> None:
            func = resolve_function(bv, function_name_or_address)
            log_debug(
                f"set_variable_type: func={func.name!r} var={var_name!r} "
                f"type={var_type!r}"
            )

            target_var = None
            for var in func.vars:
                if var.name == var_name:
                    target_var = var
                    break

            if target_var is None:
                log_error(
                    f"set_variable_type: '{var_name}' not found in "
                    f"'{func.name}'"
                )
                raise ValueError(
                    f"Variable '{var_name}' not found in function '{func.name}'"
                )

            try:
                parsed_type = bv.parse_type_string(var_type)[0]
            except Exception as e:  # noqa: BLE001
                log_error(f"set_variable_type: invalid type '{var_type}': {e}")
                raise ValueError(f"Invalid type '{var_type}': {str(e)}") from e

            state["before"] = str(target_var.type)
            state["want"] = str(parsed_type)
            log_debug(f"set_variable_type: before -> {_var_ident(target_var)}")
            func.create_user_var(target_var, parsed_type, var_name)

        def verify() -> str:
            after = resolve_function(bv, function_name_or_address)
            type_after = None
            for v in after.vars:
                if v.name == var_name:
                    type_after = str(v.type)
                    break
            want = state["want"]
            applied = type_after is not None and type_after.replace(
                " ", ""
            ) == want.replace(" ", "")
            log_info(
                f"set_variable_type: verify '{after.name}' var={var_name!r} "
                f"before={state.get('before')!r} after={type_after!r} "
                f"want={want!r} applied={applied}"
            )
            if not applied:
                log_error(
                    f"set_variable_type: did NOT persist for '{var_name}' "
                    f"in '{after.name}': type is {type_after!r}, wanted "
                    f"{want!r}, even after analysis update."
                )
                raise ValueError(
                    f"Type change for '{var_name}' to '{var_type}' did not "
                    f"persist in '{after.name}' (verified after analysis)."
                )
            return (
                f"Successfully set type of variable '{var_name}' to "
                f"'{var_type}' in function '{after.name}'"
            )

        bn_write(apply)
        # Type changes only show after the function is re-analysed; flush it
        # here on the background thread (not in the main-thread callback) so it
        # completes instead of stalling until the UI is nudged.
        bv.update_analysis_and_wait()
        return bn_write(verify)

    @mcp.tool(annotations=WRITE_VOLATILE)
    def create_data_var(
        address: str,
        var_type: str,
        name: str | None = None,
    ) -> str:
        """Create a data variable at the specified address.

        Args:
            address: Address (hex string) or symbol name.
            var_type: Type of the variable (e.g., 'int32_t', 'char*').
            name: Optional name for the variable.

        Returns:
            Success message.
        """

        def do() -> str:
            addr = resolve_symbol_address(bv, address)
            if addr is None:
                raise ValueError(f"Invalid address: {address}")

            try:
                parsed_type = bv.parse_type_string(var_type)[0]
            except Exception as e:  # noqa: BLE001
                raise ValueError(f"Invalid type '{var_type}': {str(e)}") from e

            bv.define_user_data_var(addr, parsed_type)

            if name:
                symbol = bn.Symbol(bn.SymbolType.DataSymbol, addr, name)
                bv.define_user_symbol(symbol)

            return (
                f"Successfully created data variable at {hex(addr)} with type "
                f"'{var_type}'" + (f" named '{name}'" if name else "")
            )

        return bn_write(do)
