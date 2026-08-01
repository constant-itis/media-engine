#!/usr/bin/env python3
"""media-engine MCP face.

Auto-generates ONE MCP tool per operation directly from the engine's OPERATIONS
registry — the same registry the CLI drives. Port an op once (in operations.py)
and it shows up here as a typed tool with zero extra code.

Run:   python3 server.py           (MCP stdio server)
Needs: ffmpeg/ffprobe on PATH (or FFMPEG_BIN/FFPROBE_BIN env overrides).
"""
from __future__ import annotations

import inspect
import os
import sys
from typing import Literal, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fastmcp import FastMCP

from engine.operations import OPERATIONS, run_operation

mcp = FastMCP("media-engine")


def _py_type(p):
    if p.kind == "bool":
        return bool
    if p.kind == "int":
        return int
    if p.kind == "float":
        return float
    if p.kind == "choice" and p.choices:
        return Literal[tuple(p.choices)]  # constrained enum in the tool schema
    return str  # path / str


def _derive_output(op, input_path: str) -> str:
    stem = os.path.splitext(os.path.basename(input_path))[0]
    ext = op.output_ext({})  # ext with default params; explicit output overrides anyway
    return os.path.join(os.path.dirname(os.path.abspath(input_path)), f"{stem}_{op.id}.{ext}")


def _docstring(op) -> str:
    lines = [op.description, "", "Args:",
             "  input_path: source media file",
             "  output_path: destination (optional; derived next to input if omitted)",
             "  dry_run: if true, return the ffmpeg command(s) without executing"]
    for p in op.params:
        bits = [p.kind]
        if p.choices:
            bits.append(f"one of {p.choices}")
        if p.min is not None or p.max is not None:
            bits.append(f"{p.min}..{p.max}")
        bits.append("REQUIRED" if p.required else f"default {p.default!r}")
        tail = f" — {p.help}" if p.help else ""
        lines.append(f"  {p.name}: {', '.join(bits)}{tail}")
    return "\n".join(lines)


def _make_tool(op):
    def impl(**kwargs) -> dict:
        input_path = kwargs.pop("input_path")
        output_path = kwargs.pop("output_path", None) or _derive_output(op, input_path)
        dry_run = kwargs.pop("dry_run", False)
        params = {k: v for k, v in kwargs.items() if v is not None}
        try:
            res = run_operation(op.id, input_path, output_path, params, dry_run=dry_run)
        except Exception as e:  # noqa: BLE001 - surface as a tool error string
            return {"ok": False, "error": str(e)}
        out = {"ok": True, "operation": op.id, "output": output_path,
               "commands": [" ".join(c) for c in res.commands]}
        if not dry_run:
            out["passes_run"] = res.passes_run
        return out

    # build an explicit signature so FastMCP derives a typed schema
    sig_params = [
        inspect.Parameter("input_path", inspect.Parameter.KEYWORD_ONLY, annotation=str),
        inspect.Parameter("output_path", inspect.Parameter.KEYWORD_ONLY,
                          default=None, annotation=Optional[str]),
        inspect.Parameter("dry_run", inspect.Parameter.KEYWORD_ONLY,
                          default=False, annotation=bool),
    ]
    ann = {"input_path": str, "output_path": Optional[str], "dry_run": bool}
    for p in op.params:
        t = _py_type(p)
        if p.required:
            sig_params.append(inspect.Parameter(p.name, inspect.Parameter.KEYWORD_ONLY, annotation=t))
            ann[p.name] = t
        else:
            sig_params.append(inspect.Parameter(p.name, inspect.Parameter.KEYWORD_ONLY,
                                                default=p.default, annotation=Optional[t]))
            ann[p.name] = Optional[t]
    ann["return"] = dict
    impl.__signature__ = inspect.Signature(sig_params)
    impl.__annotations__ = ann
    impl.__name__ = op.id
    impl.__doc__ = _docstring(op)
    return impl


for _op in OPERATIONS.values():
    mcp.tool(name=_op.id)(_make_tool(_op))


@mcp.tool
def list_operations() -> list[dict]:
    """List all available media operations with their category and description."""
    return [{"id": o.id, "category": o.category, "description": o.description,
             "params": [p.name for p in o.params]} for o in OPERATIONS.values()]


if __name__ == "__main__":
    mcp.run()
