from __future__ import annotations

import typing as ty


def parse_address(s: str) -> int:
    """Parse '0x1000' or '4096' (decimal) into an int. Raises ValueError on garbage."""
    s = s.strip()
    if not s:
        raise ValueError("empty address")
    return int(s, 16) if s.lower().startswith("0x") else int(s, 0)


def resolve_symbol_address(bv: ty.Any, address_or_name: str) -> int | None:
    """Resolve a symbol name or address to a numeric address.

    Faithful port of BinAssistMCP's ``BinAssistMCPTools._resolve_symbol``: tries
    hex parse, then decimal, validates against binary bounds, then falls back to
    function name, data-variable symbol name, and raw symbol name. Returns the
    address, or ``None`` if nothing resolves. Raises ``ValueError`` when a parsed
    numeric address is outside the binary bounds.
    """
    address: int | None = None

    # Try to parse as hex address (bare hex is treated as hex, matching BinAssist)
    try:
        if isinstance(address_or_name, str) and address_or_name.startswith(
            "0x"
        ):
            address = int(address_or_name, 16)
        else:
            address = int(address_or_name, 16)
    except ValueError:
        pass

    # Try to parse as decimal address
    if address is None:
        try:
            addr = int(address_or_name)
            if addr >= 0:
                address = addr
        except ValueError:
            pass

    # Validate address bounds if we parsed a numeric address
    if address is not None:
        if address < bv.start or address > bv.end:
            raise ValueError(
                f"Address {hex(address)} is outside binary bounds "
                f"({hex(bv.start)} - {hex(bv.end)})"
            )
        return address

    # Search by function name
    for func in bv.functions:
        if func.name == address_or_name:
            return func.start

    # Search by data variable name
    for addr, var in bv.data_vars.items():
        if (
            hasattr(var, "symbol")
            and var.symbol
            and var.symbol.name == address_or_name
        ):
            return addr

    # Search by symbol name
    symbol = bv.get_symbol_by_raw_name(str(address_or_name))
    if symbol:
        return symbol.address

    return None


def get_function_by_name_or_address(bv: ty.Any, identifier: str) -> ty.Any:
    """Get a function by name or address, or ``None``.

    Faithful port of BinAssistMCP's
    ``BinAssistMCPTools._get_function_by_name_or_address``.
    """
    # Handle address-based lookup
    try:
        if isinstance(identifier, str) and identifier.startswith("0x"):
            addr = int(identifier, 16)
        else:
            addr = (
                int(identifier) if isinstance(identifier, str) else identifier
            )
        func = bv.get_function_at(addr)
        if func:
            return func
    except ValueError:
        pass

    # Handle name-based lookup
    for func in bv.functions:
        if func.name == identifier:
            return func

    # Try case-insensitive match
    for func in bv.functions:
        if func.name.lower() == str(identifier).lower():
            return func

    # Try symbol lookup
    symbol = bv.get_symbol_by_raw_name(str(identifier))
    if symbol and symbol.address:
        func = bv.get_function_at(symbol.address)
        if func:
            return func

    return None


def get_function_containing_or_at(bv: ty.Any, addr: int) -> ty.Any:
    """Resolve an address to the function that owns it, or ``None``.

    Faithful port of BinAssistMCP's
    ``BinAssistMCPTools._get_function_containing_or_at``.
    """
    func = bv.get_function_at(addr)
    if func:
        return func

    funcs = bv.get_functions_containing(addr)
    if funcs:
        return funcs[0]

    return None


def resolve_function(bv: ty.Any, key: str) -> ty.Any:
    """Resolve a function by name OR address, raising ValueError on no match.

    Thin wrapper over :func:`get_function_by_name_or_address` that mirrors
    BinAssistMCP's "function not found" raise so callers stay one line.
    """
    func = get_function_by_name_or_address(bv, key)
    if func is None:
        raise ValueError(f"Function not found: {key}")
    return func
