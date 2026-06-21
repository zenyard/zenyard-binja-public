from __future__ import annotations

import re
import typing as ty

from ._hints import DESTRUCTIVE, FILE_WRITE
from pathlib import Path

from .._resolve import resolve_symbol_address
from ..server import bn_write


def _parse_patch_bytes(value: ty.Any) -> bytes:
    """Parse bytes from a hex string or an integer array (mirrors BinAssistMCP)."""
    if isinstance(value, str):
        normalized = value.replace("0x", "").replace("0X", "")
        normalized = re.sub(r"[,\s]", "", normalized)
        if not normalized:
            return b""
        if len(normalized) % 2 != 0:
            raise ValueError(
                "hex string must contain an even number of characters"
            )
        try:
            return bytes.fromhex(normalized)
        except ValueError as e:
            raise ValueError(f"invalid hex string: {value}") from e

    if isinstance(value, (list, tuple)):
        parsed = bytearray()
        for index, item in enumerate(value):
            if not isinstance(item, int):
                raise ValueError(
                    f"array element at index {index} is not an integer"
                )
            if item < 0 or item > 255:
                raise ValueError(
                    f"array element at index {index} out of range "
                    f"(0-255): {item}"
                )
            parsed.append(item)
        return bytes(parsed)

    raise ValueError("bytes must be a hex string or integer array")


def _format_hex(data: bytes) -> str:
    """Format bytes as space-separated uppercase hex."""
    return " ".join(f"{b:02X}" for b in data)


def _patch_bytes_impl(
    bv: ty.Any, addr: int, patch_data: bytes, clear_code_units: bool = False
) -> dict[str, ty.Any]:
    """Write raw bytes and return before/after details (mirrors BinAssistMCP)."""
    if not patch_data:
        raise ValueError("No bytes provided to patch")

    end_addr = addr + len(patch_data) - 1
    if addr < bv.start or end_addr > bv.end:
        raise ValueError(
            f"Patch range is outside binary bounds: {hex(addr)} - "
            f"{hex(end_addr)} (bounds {hex(bv.start)} - {hex(bv.end)})"
        )

    before = bv.read(addr, len(patch_data))
    if before is None or len(before) != len(patch_data):
        raise ValueError(f"Failed reading original bytes at {hex(addr)}")

    written = bv.write(addr, patch_data)
    if written != len(patch_data):
        raise ValueError(f"Patch wrote {written} of {len(patch_data)} byte(s)")

    return {
        "status": "patched",
        "address": hex(addr),
        "end_address": hex(end_addr),
        "size": len(patch_data),
        "before": _format_hex(before),
        "after": _format_hex(patch_data),
        "clear_code_units": clear_code_units,
    }


def _get_arch_for_address(bv: ty.Any, addr: int) -> ty.Any:
    """Resolve the best Binary Ninja architecture for an address."""
    funcs = bv.get_functions_containing(addr)
    if funcs:
        arch = getattr(funcs[0], "arch", None)
        if arch:
            return arch
    arch = getattr(bv, "arch", None)
    if arch:
        return arch
    raise ValueError(f"Could not determine architecture at {hex(addr)}")


def register_patching_tools(mcp: ty.Any, bv: ty.Any) -> None:
    """Wire the patching/export tools onto the given FastMCP instance.

    Faithful port of BinAssistMCP's byte/assembly patching and program export.
    """

    @mcp.tool(annotations=DESTRUCTIVE)
    def patch_bytes(
        address: str,
        bytes: str | list[int],  # noqa: A002 - matches BinAssistMCP input name
        clear_code_units: bool = False,
    ) -> dict[str, ty.Any]:
        """Patch raw bytes in the binary at a given address.

        Args:
            address: Address (hex string) or symbol name.
            bytes: Bytes to write, as a hex string (e.g. '90 90') or integer
                array (e.g. [144, 144]).
            clear_code_units: Accepted for cross-client parity; Binary Ninja
                writes bytes directly.

        Returns:
            Dict with patch range and before/after bytes.
        """

        def do() -> dict[str, ty.Any]:
            addr = resolve_symbol_address(bv, address)
            if addr is None:
                raise ValueError(f"Invalid address: {address}")
            patch_data = _parse_patch_bytes(bytes)
            return _patch_bytes_impl(bv, addr, patch_data, clear_code_units)

        return bn_write(do)

    @mcp.tool(annotations=DESTRUCTIVE)
    def assemble_code(
        address: str,
        code: str,
        patch: bool = True,
        clear_code_units: bool = False,
    ) -> dict[str, ty.Any]:
        """Assemble instruction text at an address and optionally patch it.

        Args:
            address: Address (hex string) or symbol name.
            code: Single instruction or newline-separated assembly block.
            patch: If true, write assembled bytes into the binary.
            clear_code_units: Accepted for cross-client parity; Binary Ninja
                writes bytes directly.

        Returns:
            Dict with assembled bytes and optional before/after patch details.
        """

        def do() -> dict[str, ty.Any]:
            addr = resolve_symbol_address(bv, address)
            if addr is None:
                raise ValueError(f"Invalid address: {address}")

            lines = [line.strip() for line in code.splitlines() if line.strip()]
            if not lines:
                raise ValueError("No assembly instructions provided")

            arch = _get_arch_for_address(bv, addr)
            try:
                assembled = arch.assemble("\n".join(lines), addr)
            except Exception as e:  # noqa: BLE001
                raise ValueError(f"Assembly failed at {hex(addr)}: {e}") from e

            if not assembled:
                raise ValueError("Assembler produced no bytes")

            end_addr = addr + len(assembled) - 1
            result: dict[str, ty.Any] = {
                "status": "assembled",
                "address": hex(addr),
                "end_address": hex(end_addr),
                "size": len(assembled),
                "bytes": _format_hex(assembled),
                "instruction_lines": len(lines),
                "architecture": getattr(arch, "name", str(arch)),
                "patched": False,
            }

            if patch:
                patch_result = _patch_bytes_impl(
                    bv, addr, assembled, clear_code_units
                )
                result.update(
                    {
                        "status": "assembled_and_patched",
                        "patched": True,
                        "before": patch_result["before"],
                        "after": patch_result["after"],
                        "clear_code_units": clear_code_units,
                    }
                )

            return result

        return bn_write(do)

    @mcp.tool(annotations=FILE_WRITE)
    def export_program(
        output_path: str,
        format: str = "binary",  # noqa: A002 - matches BinAssistMCP input name
        overwrite: bool = False,
    ) -> dict[str, ty.Any]:
        """Export the current binary or Binary Ninja database to disk.

        Args:
            output_path: Destination path on the host filesystem.
            format: 'binary' for a patched executable or 'bndb' for a database.
            overwrite: Whether to replace an existing output file.

        Returns:
            Dict with export status and output metadata.
        """

        def do() -> dict[str, ty.Any]:
            if not output_path or not str(output_path).strip():
                raise ValueError("output_path is required")

            destination = Path(output_path).expanduser()
            if destination.exists() and not overwrite:
                raise ValueError(
                    f"Output file already exists: {destination} "
                    "(set overwrite=True to replace it)"
                )

            destination.parent.mkdir(parents=True, exist_ok=True)
            export_format = (format or "binary").lower()

            if export_format == "binary":
                success = bv.save(str(destination))
            elif export_format == "bndb":
                success = bv.create_database(str(destination))
            else:
                raise ValueError("Unsupported format. Use 'binary' or 'bndb'.")

            if not success:
                raise ValueError(f"Export failed for {destination}")

            return {
                "status": "exported",
                "format": export_format,
                "output_path": str(destination.resolve()),
                "bytes_written": (
                    destination.stat().st_size if destination.exists() else None
                ),
            }

        return bn_write(do)
