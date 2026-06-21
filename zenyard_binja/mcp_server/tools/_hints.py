"""Reusable MCP ``ToolAnnotations`` for the tool decorators.

These are advisory hints (per the MCP spec, clients must never trust them from
untrusted servers); an MCP client uses them for display and auto-approval
decisions — e.g. auto-run read-only queries, confirm writes, flag destructive
operations. We classify each tool by behavior rather than copying BinAssistMCP's
coarse buckets (which, for instance, mislabel relative ``patch_bytes`` as
idempotent).

Hint meanings:
- ``readOnlyHint``: tool does not modify state.
- ``idempotentHint``: repeated calls with the same args cause no *additional*
  effect (only meaningful when not read-only).
- ``destructiveHint``: may perform a destructive/irreversible update.
- ``openWorldHint``: may touch an external/open world (filesystem, network);
  false for pure local analysis.
"""

from __future__ import annotations

from mcp.types import ToolAnnotations  # type: ignore[import-not-found]

# Pure queries over the bound BinaryView.
READ_ONLY = ToolAnnotations(
    readOnlyHint=True, idempotentHint=True, openWorldHint=False
)

# Mutations whose repeat leaves the same final state (renames, set comment/type).
WRITE_IDEMPOTENT = ToolAnnotations(
    readOnlyHint=False, idempotentHint=True, openWorldHint=False
)

# Mutations that are not naturally idempotent (create_* error on duplicate).
WRITE_VOLATILE = ToolAnnotations(
    readOnlyHint=False, idempotentHint=False, openWorldHint=False
)

# Overwrites existing bytes/code units — irreversible in place.
DESTRUCTIVE = ToolAnnotations(
    readOnlyHint=False,
    idempotentHint=False,
    destructiveHint=True,
    openWorldHint=False,
)

# Writes a file to disk (reaches outside the binary view).
FILE_WRITE = ToolAnnotations(
    readOnlyHint=False,
    idempotentHint=False,
    destructiveHint=True,
    openWorldHint=True,
)

# Triggers Binary Ninja analysis (mutates analysis state, non-idempotent).
ANALYSIS = ToolAnnotations(
    readOnlyHint=False, idempotentHint=False, openWorldHint=False
)

# Executes arbitrary user Python — may mutate the binary, hit disk/network.
CODE_EXEC = ToolAnnotations(
    title="Evaluate Python",
    readOnlyHint=False,
    destructiveHint=True,
    idempotentHint=False,
    openWorldHint=True,
)
