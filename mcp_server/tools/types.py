from __future__ import annotations

import typing as ty

from ._hints import READ_ONLY, WRITE_VOLATILE

import binaryninja as bn  # type: ignore[import]

from ..server import bn_write


def register_types_tools(mcp: ty.Any, bv: ty.Any) -> None:
    """Wire the types tools onto the given FastMCP instance.

    Faithful port of BinAssistMCP's type read tools (get_classes / get_types /
    get_type_info) and write tools (create_type / create_enum / create_typedef /
    create_class / add_class_member), as atomic ``@mcp.tool()`` functions over
    the already-bound ``bv``.
    """

    def _get_type_category(type_obj: ty.Any) -> str:
        """Get the category of a type object via ``.type_class`` identity."""
        tc = type_obj.type_class
        if tc is bn.TypeClass.StructureTypeClass:
            return "struct"
        elif tc is bn.TypeClass.EnumerationTypeClass:
            return "enum"
        elif tc is bn.TypeClass.ArrayTypeClass:
            return "array"
        elif tc is bn.TypeClass.PointerTypeClass:
            return "pointer"
        elif tc is bn.TypeClass.FunctionTypeClass:
            return "function"
        return "primitive"

    @mcp.tool(annotations=READ_ONLY)
    def get_classes() -> list[dict[str, ty.Any]]:
        """Get all classes/structs/types in the binary."""
        classes: list[dict[str, ty.Any]] = []

        # Get all user-defined types
        for type_name, type_obj in bv.types.items():
            if type_obj.type_class is not bn.TypeClass.StructureTypeClass:
                continue

            members: list[dict[str, ty.Any]] = []
            for member in type_obj.members:
                members.append(
                    {
                        "name": member.name,
                        "type": str(member.type),
                        "offset": member.offset,
                    }
                )

            classes.append(
                {
                    "name": type_name,
                    "type": "struct",  # BN uses StructureType for both
                    "size": type_obj.width,
                    "members": members,
                    "member_count": len(members),
                }
            )

        return classes

    @mcp.tool(annotations=READ_ONLY)
    def list_types(
        page_size: int = 100, page_number: int = 1
    ) -> dict[str, ty.Any]:
        """List all type definitions in the binary with pagination."""
        all_types: list[dict[str, ty.Any]] = []

        for type_name, type_obj in bv.types.items():
            type_info: dict[str, ty.Any] = {
                "name": type_name,
                "size": type_obj.width if hasattr(type_obj, "width") else None,
                "category": _get_type_category(type_obj),
                "definition": str(type_obj),
            }

            tc = type_obj.type_class
            if tc is bn.TypeClass.StructureTypeClass or (
                tc is bn.TypeClass.EnumerationTypeClass
            ):
                type_info["member_count"] = (
                    len(type_obj.members) if hasattr(type_obj, "members") else 0
                )
            elif tc is bn.TypeClass.ArrayTypeClass:
                type_info["element_type"] = str(type_obj.element_type)
                type_info["count"] = type_obj.count

            all_types.append(type_info)

        # Calculate pagination
        total_count = len(all_types)
        start_idx = (page_number - 1) * page_size
        end_idx = start_idx + page_size

        # Get the paginated slice
        paginated_types = all_types[start_idx:end_idx]

        return {
            "types": paginated_types,
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
    def get_type_info(type_name: str) -> dict[str, ty.Any]:
        """Get detailed information about a specific type by name."""
        if type_name not in bv.types:
            raise ValueError(f"Type '{type_name}' not found")

        type_obj = bv.types[type_name]

        info: dict[str, ty.Any] = {
            "name": type_name,
            "category": _get_type_category(type_obj),
            "size": type_obj.width if hasattr(type_obj, "width") else None,
            "definition": str(type_obj),
        }

        tc = type_obj.type_class
        if tc is bn.TypeClass.StructureTypeClass:
            info["members"] = []
            if hasattr(type_obj, "members"):
                for member in type_obj.members:
                    info["members"].append(
                        {
                            "name": member.name,
                            "type": str(member.type),
                            "offset": member.offset,
                            "size": (member.type.width if member.type else 0),
                        }
                    )

        elif tc is bn.TypeClass.EnumerationTypeClass:
            info["members"] = []
            if hasattr(type_obj, "members"):
                for member in type_obj.members:
                    info["members"].append(
                        {"name": member.name, "value": member.value}
                    )

        elif tc is bn.TypeClass.ArrayTypeClass:
            info["element_type"] = str(type_obj.element_type)
            info["count"] = type_obj.count
            info["element_size"] = (
                type_obj.element_type.width if type_obj.element_type else 0
            )

        elif tc is bn.TypeClass.PointerTypeClass:
            info["target_type"] = str(type_obj.target)
            info["pointer_size"] = type_obj.width

        elif tc is bn.TypeClass.FunctionTypeClass:
            info["return_type"] = str(type_obj.return_value)
            info["parameters"] = []
            if hasattr(type_obj, "parameters"):
                for param in type_obj.parameters:
                    info["parameters"].append(
                        {
                            "type": str(param.type),
                            "name": (
                                param.name if hasattr(param, "name") else None
                            ),
                        }
                    )

        return info

    @mcp.tool(annotations=WRITE_VOLATILE)
    def create_type(name: str, definition: str) -> str:
        """Create a new data type from a C-like definition.

        Args:
            name: Name of the type.
            definition: Type definition (e.g., 'struct { int x; int y; }', 'int*').

        Returns:
            Success message.
        """

        def do() -> str:
            if name in bv.types:
                raise ValueError(f"Type '{name}' already exists")

            try:
                parsed_type = bv.parse_type_string(definition)[0]
            except Exception as e:  # noqa: BLE001
                raise ValueError(
                    f"Invalid type definition '{definition}': {str(e)}"
                ) from e

            bv.define_user_type(name, parsed_type)
            return (
                f"Successfully created type '{name}' with definition "
                f"'{definition}'"
            )

        return bn_write(do)

    @mcp.tool(annotations=WRITE_VOLATILE)
    def create_enum(name: str, members: dict[str, int]) -> str:
        """Create an enumeration type.

        Args:
            name: Name of the enum.
            members: Dictionary of member names to integer values.

        Returns:
            Success message.
        """

        def do() -> str:
            if name in bv.types:
                raise ValueError(f"Type '{name}' already exists")

            enum_builder = bn.EnumerationBuilder.create()
            for member_name, value in members.items():
                enum_builder.append(member_name, value)

            enum_type = bn.Type.enumeration_type(bv.arch, enum_builder, 4)
            bv.define_user_type(name, enum_type)

            member_list = ", ".join(f"{k}={v}" for k, v in members.items())
            return (
                f"Successfully created enum '{name}' with members: "
                f"{member_list}"
            )

        return bn_write(do)

    @mcp.tool(annotations=WRITE_VOLATILE)
    def create_typedef(name: str, base_type: str) -> str:
        """Create a type alias (typedef).

        Args:
            name: Name of the typedef.
            base_type: Base type to alias.

        Returns:
            Success message.
        """

        def do() -> str:
            if name in bv.types:
                raise ValueError(f"Type '{name}' already exists")

            try:
                parsed_type = bv.parse_type_string(base_type)[0]
            except Exception as e:  # noqa: BLE001
                raise ValueError(
                    f"Invalid base type '{base_type}': {str(e)}"
                ) from e

            named_type = bn.Type.named_type_from_type(name, parsed_type)
            bv.define_user_type(name, named_type)

            return (
                f"Successfully created typedef '{name}' for type '{base_type}'"
            )

        return bn_write(do)

    @mcp.tool(annotations=WRITE_VOLATILE)
    def create_class(name: str, size: int) -> str:
        """Create a new class/struct type.

        Args:
            name: Name of the class/struct.
            size: Size in bytes.

        Returns:
            Success message.
        """

        def do() -> str:
            if name in bv.types:
                raise ValueError(f"Type '{name}' already exists")

            struct = bn.StructureBuilder.create()
            struct.width = size

            bv.define_user_type(name, struct)
            return (
                f"Successfully created class/struct '{name}' with size "
                f"{size} bytes"
            )

        return bn_write(do)

    @mcp.tool(annotations=WRITE_VOLATILE)
    def add_struct_member(
        class_name: str,
        member_name: str,
        member_type: str,
        offset: int,
    ) -> str:
        """Add a member to an existing class/struct.

        Args:
            class_name: Name of the class/struct.
            member_name: Name of the member.
            member_type: Type of the member (e.g., 'int32_t', 'char*').
            offset: Offset within the struct.

        Returns:
            Success message.
        """

        def do() -> str:
            if class_name not in bv.types:
                raise ValueError(f"Class/struct '{class_name}' not found")

            struct_type = bv.types[class_name]
            # Use the same .type_class guard the read tools in this module use
            # (proven on this BN version) rather than isinstance, for consistency.
            if struct_type.type_class is not bn.TypeClass.StructureTypeClass:
                raise ValueError(f"'{class_name}' is not a class or struct")

            try:
                parsed_type = bv.parse_type_string(member_type)[0]
            except Exception as e:  # noqa: BLE001
                raise ValueError(
                    f"Invalid type '{member_type}': {str(e)}"
                ) from e

            struct_builder = struct_type.mutable_copy()
            struct_builder.insert(offset, parsed_type, member_name)

            bv.define_user_type(class_name, struct_builder)
            return (
                f"Successfully added member '{member_name}' to "
                f"'{class_name}' at offset {offset}"
            )

        return bn_write(do)
