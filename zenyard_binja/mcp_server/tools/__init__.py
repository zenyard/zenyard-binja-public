from __future__ import annotations

import typing as ty

from .binary_info import register_binary_info_tools
from .code import register_code_tools
from .comments import register_comments_tools
from .listing import register_listing_tools
from .navigation import register_navigation_tools
from .patching import register_patching_tools
from .scripting import register_scripting_tools
from .symbols import register_symbols_tools
from .types import register_types_tools
from .variables import register_variables_tools
from .xrefs import register_xrefs_tools


def register_tools(mcp: ty.Any, bv: ty.Any) -> None:
    """Wire every tool category onto the FastMCP instance."""
    register_listing_tools(mcp, bv)
    register_code_tools(mcp, bv)
    register_xrefs_tools(mcp, bv)
    register_binary_info_tools(mcp, bv)
    register_symbols_tools(mcp, bv)
    register_types_tools(mcp, bv)
    register_variables_tools(mcp, bv)
    register_comments_tools(mcp, bv)
    register_patching_tools(mcp, bv)
    register_navigation_tools(mcp, bv)
    register_scripting_tools(mcp, bv)
