#!/usr/bin/env python3
"""media-engine CLI face. One of several planned faces over the same engine
(MCP next). Usage:

    python cli.py list
    python cli.py info <op>
    python cli.py run <op> -i IN -o OUT [--param value ...]
    python cli.py run video_to_gif -i clip.mp4 -o out.gif --gifFpsPreset 15
"""
from __future__ import annotations

import argparse
import sys

from engine.operations import OPERATIONS, run_operation


def _progress(label, done, total):
    if total:
        pct = min(100, 100 * done / total)
        sys.stderr.write(f"\r  [{label}] {pct:5.1f}%  {done:6.1f}/{total:.1f}s")
    else:
        sys.stderr.write(f"\r  [{label}] {done:6.1f}s")
    sys.stderr.flush()


def cmd_list(_):
    for op in OPERATIONS.values():
        print(f"  {op.id:16s} [{op.category:5s}] {op.description}")


def cmd_info(a):
    op = OPERATIONS.get(a.op)
    if not op:
        sys.exit(f"unknown op '{a.op}'")
    print(f"{op.id}  [{op.category}]\n  {op.description}\n  params:")
    for p in op.params:
        bits = [p.kind]
        if p.choices:
            bits.append(f"choices={p.choices}")
        if p.min is not None or p.max is not None:
            bits.append(f"range={p.min}..{p.max}")
        if p.required:
            bits.append("REQUIRED")
        else:
            bits.append(f"default={p.default!r}")
        print(f"    --{p.name:20s} {', '.join(bits)}  {p.help}")


def cmd_run(a):
    op = OPERATIONS.get(a.op)
    if not op:
        sys.exit(f"unknown op '{a.op}'")
    # collect --param values from the remainder
    params = {}
    it = iter(a.params)
    for tok in it:
        if not tok.startswith("--"):
            sys.exit(f"expected --param, got {tok!r}")
        key = tok[2:]
        val = next(it, None)
        params[key] = val
    try:
        res = run_operation(a.op, a.input, a.output, params,
                            progress=None if a.quiet else _progress,
                            threads=a.threads, dry_run=a.dry_run)
    except Exception as e:  # noqa: BLE001 - CLI surface
        sys.stderr.write("\n")
        sys.exit(f"error: {e}")
    sys.stderr.write("\n")
    if a.dry_run:
        for c in res.commands:
            print(" ".join(_quote(x) for x in c))
    else:
        print(f"done: {a.output} ({res.passes_run} pass{'es' if res.passes_run != 1 else ''})")


def _quote(s: str) -> str:
    return f'"{s}"' if " " in s else s


def main():
    ap = argparse.ArgumentParser(prog="media-engine")
    sub = ap.add_subparsers(required=True)

    sub.add_parser("list").set_defaults(func=cmd_list)

    pi = sub.add_parser("info")
    pi.add_argument("op")
    pi.set_defaults(func=cmd_info)

    pr = sub.add_parser("run")
    pr.add_argument("op")
    pr.add_argument("-i", "--input", required=True)
    pr.add_argument("-o", "--output", required=True)
    pr.add_argument("--threads", type=int)
    pr.add_argument("--dry-run", action="store_true", help="print ffmpeg commands, don't run")
    pr.add_argument("--quiet", action="store_true")
    pr.set_defaults(func=cmd_run)

    # unknown args (the --param value pairs for an op) fall through to the engine
    a, extra = ap.parse_known_args()
    a.params = extra
    a.func(a)


if __name__ == "__main__":
    main()
