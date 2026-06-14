from __future__ import annotations

import ast
import io
import traceback
import typing as ty
from contextlib import redirect_stderr, redirect_stdout

import binaryninja as bn  # type: ignore[import]

from ._hints import CODE_EXEC

from ..server import bn_write


def register_scripting_tools(mcp: ty.Any, bv: ty.Any) -> None:
    """Wire the Python-evaluation tool onto the given FastMCP instance.

    Runs arbitrary Python against the open binary, the equivalent of typing
    into Binary Ninja's scripting console. The snippet's namespace exposes
    ``bv`` (the bound BinaryView), ``bn``/``binaryninja``, and best-effort
    ``here``/``current_function`` mirrored from ``navigation``.
    """

    @mcp.tool(
        annotations=CODE_EXEC,
        meta={"zenyard.ai/action-category": "scripts"},
    )
    def py_eval(code: str) -> dict[str, str]:
        """Run Python against the open binary and return its output.

        The namespace exposes ``bv`` (the open BinaryView), ``bn``/
        ``binaryninja``, and best-effort ``here``/``current_function``.

        Returns a dict of three strings: ``result`` (the trailing expression,
        else a ``result``/last-assigned variable, else ``""``, rendered as a
        string), ``stdout``, and ``stderr``. Any error is returned, not
        raised: ``result`` and ``stdout`` come back empty and the full
        traceback is in ``stderr``.

        The namespace is fresh on every call (nothing persists between
        calls). Execution happens on Binary Ninja's main thread, so a long or
        blocking snippet stalls the UI until it returns.

        Args:
            code: Python code to run.
        """

        def run() -> dict[str, str]:
            namespace = _console_namespace(bv)
            out, err = io.StringIO(), io.StringIO()
            try:
                with redirect_stdout(out), redirect_stderr(err):
                    result = _eval_capture(code, namespace)
            except BaseException:  # noqa: BLE001 - any error is returned
                return {
                    "result": "",
                    "stdout": "",
                    "stderr": traceback.format_exc(),
                }
            return {
                "result": result,
                "stdout": out.getvalue(),
                "stderr": err.getvalue(),
            }

        return bn_write(run)


def _console_namespace(bv: ty.Any) -> dict[str, ty.Any]:
    """Build the fresh namespace a snippet runs in, with console magic vars."""
    here = _current_address(bv)
    return {
        "bv": bv,
        "bn": bn,
        "binaryninja": bn,
        "here": here,
        "current_address": here,
        "current_function": _current_function(bv, here),
    }


def _current_address(bv: ty.Any) -> int | None:
    """Best-effort current address, mirroring ``navigation`` fallbacks.

    Uses ``bv.offset`` when present (a UI concept), else the first entry
    point, else the first function start, else the view start.
    """
    try:
        if hasattr(bv, "offset"):
            return bv.offset
        if bv.entry_points:
            return bv.entry_points[0]
        functions = list(bv.functions)
        if functions:
            return functions[0].start
        return bv.start
    except Exception:  # noqa: BLE001 - magic vars are best-effort
        return None


def _current_function(bv: ty.Any, here: int | None) -> ty.Any:
    """Function containing ``here``, or None."""
    if here is None:
        return None
    try:
        functions = bv.get_functions_containing(here)
        return functions[0] if functions else None
    except Exception:  # noqa: BLE001 - magic vars are best-effort
        return None


def _eval_capture(code: str, namespace: dict[str, ty.Any]) -> str:
    """Execute ``code`` in ``namespace`` and return its value as a string.

    Empty code yields ``""``. When the final top-level node is an expression,
    its value is returned. Otherwise a variable named ``result`` wins, then
    the last assigned variable, then ``""``.
    """
    tree = ast.parse(code)
    body = tree.body
    if not body:
        return ""

    last = body[-1]
    if isinstance(last, ast.Expr):
        if len(body) > 1:
            prefix = ast.Module(body=body[:-1], type_ignores=[])
            exec(compile(prefix, "<py_eval>", "exec"), namespace)
        value = eval(
            compile(ast.Expression(last.value), "<py_eval>", "eval"),
            namespace,
        )
        return _to_str(value)

    exec(compile(tree, "<py_eval>", "exec"), namespace)
    if "result" in namespace:
        return _to_str(namespace["result"])
    name = _last_assigned_name(body)
    if name is not None and name in namespace:
        return _to_str(namespace[name])
    return ""


def _last_assigned_name(body: list[ast.stmt]) -> str | None:
    """Name of the last simple top-level assignment target, or None."""
    name: str | None = None
    for node in body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    name = target.id
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
            if isinstance(node.target, ast.Name):
                name = node.target.id
    return name


def _to_str(value: ty.Any) -> str:
    """Render a value as a string, guarding a misbehaving ``__str__``."""
    try:
        return str(value)
    except Exception:  # noqa: BLE001 - never break the return contract
        try:
            return repr(value)
        except Exception:  # noqa: BLE001
            return ""
