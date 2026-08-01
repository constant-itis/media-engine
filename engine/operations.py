"""Operation registry aggregator + executor.

The model lives in opmodel.py; the operations themselves live in ops/{video,audio,
image}.py. This module assembles them into the OPERATIONS registry every face
(CLI, MCP) drives, and runs an op end-to-end (validate -> probe -> build -> run).
"""
from __future__ import annotations

import os

from .opmodel import Context, Operation, Param  # re-exported for faces/tests
from .ops import ALL_OPS
from .probe import probe

OPERATIONS: dict[str, Operation] = {op.id: op for op in ALL_OPS}

__all__ = ["OPERATIONS", "run_operation", "Context", "Operation", "Param"]


def run_operation(op_id: str, input_path: str, output_path: str, params: dict,
                  *, progress=None, threads=None, dry_run=False):
    from .ffmpeg import run_passes

    if op_id not in OPERATIONS:
        raise ValueError(f"unknown operation '{op_id}'. known: {sorted(OPERATIONS)}")
    op = OPERATIONS[op_id]
    valid = op.validate(params)
    info = probe(input_path) if (op.needs_probe and not dry_run) else None
    ctx = Context(input=input_path, output=output_path, params=valid, info=info)
    passes = op.build(ctx)
    try:
        return run_passes(passes, progress=progress, threads=threads, dry_run=dry_run)
    finally:
        for t in ctx._temps:
            try:
                os.remove(t)
            except OSError:
                pass
