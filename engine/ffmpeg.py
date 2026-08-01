"""ffmpeg/ffprobe process layer: binary resolution, robust progress, error tails.

Deliberately stdlib-only. Improves on the ripped source in two places the recon
flagged as fragile:
  - progress is read from `-progress pipe:1` (machine-readable) instead of
    scraping ffmpeg's locale/version-dependent stderr `time=` lines.
  - the null sink is `/dev/null` / `NUL` chosen per-OS instead of hardcoded NUL.
"""
from __future__ import annotations

import functools
import os
import re
import shutil
import subprocess
import threading
from dataclasses import dataclass, field
from typing import Callable, Optional

MAX_STDERR_CHARS = 64 * 1024  # bound memory on chatty jobs; only the tail is surfaced


class FFmpegError(RuntimeError):
    def __init__(self, args: list[str], returncode: int, stderr_tail: str):
        self.returncode = returncode
        self.stderr_tail = stderr_tail
        super().__init__(f"ffmpeg exited {returncode}\n{stderr_tail}")


def resolve_ffmpeg() -> str:
    return _resolve("FFMPEG_BIN", "ffmpeg")


def resolve_ffprobe() -> str:
    return _resolve("FFPROBE_BIN", "ffprobe")


def _resolve(env: str, name: str) -> str:
    # explicit override wins, then PATH. Cross-platform: no .exe assumption.
    override = os.environ.get(env)
    if override and os.path.isfile(override):
        return override
    found = shutil.which(name)
    if found:
        return found
    raise FileNotFoundError(
        f"{name} not found on PATH (set {env} to override)."
    )


@functools.lru_cache(maxsize=1)
def ffmpeg_major_version() -> Optional[int]:
    """Best-effort ffmpeg major version. None if unparseable (assume modern)."""
    try:
        out = subprocess.run([resolve_ffmpeg(), "-version"],
                             capture_output=True, text=True).stdout
    except Exception:  # noqa: BLE001
        return None
    m = re.search(r"ffmpeg version n?(\d+)\.", out)
    return int(m.group(1)) if m else None


def fps_mode_args() -> list[str]:
    """`-fps_mode passthrough` on ffmpeg >=5.0, else the legacy `-vsync passthrough`.
    Upstream bundled ffmpeg 6.1.1 and always used -fps_mode; a portable engine
    must fall back for older system ffmpeg (e.g. 4.4.2)."""
    major = ffmpeg_major_version()
    if major is not None and major < 5:
        return ["-vsync", "passthrough"]
    return ["-fps_mode", "passthrough"]


def null_device() -> str:
    """Per-OS null sink. The ripped app hardcoded 'NUL' -> broke off Windows."""
    return "NUL" if os.name == "nt" else "/dev/null"


@dataclass
class Pass:
    """One ffmpeg invocation. `args` excludes the binary and global flags."""
    args: list[str]
    label: str = ""
    # media seconds this pass will process, for progress %; None = unknown
    total_seconds: Optional[float] = None


@dataclass
class RunResult:
    passes_run: int
    commands: list[list[str]] = field(default_factory=list)


ProgressCB = Callable[[str, float, Optional[float]], None]
# (pass_label, seconds_done, total_seconds) -> None


def run_passes(
    passes: list[Pass],
    *,
    progress: Optional[ProgressCB] = None,
    threads: Optional[int] = None,
    dry_run: bool = False,
) -> RunResult:
    ffmpeg = resolve_ffmpeg()
    global_args = ["-hide_banner", "-y", "-nostdin"]
    if threads:
        global_args += ["-threads", str(threads)]

    result = RunResult(passes_run=0)
    for p in passes:
        cmd = [ffmpeg, *global_args, "-progress", "pipe:1", "-nostats", *p.args]
        result.commands.append(cmd)
        if dry_run:
            continue
        _run_one(cmd, p, progress)
        result.passes_run += 1
    return result


def _run_one(cmd: list[str], p: Pass, progress: Optional[ProgressCB]) -> None:
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )

    # Drain stderr on a thread into a bounded ring buffer to avoid pipe deadlock.
    stderr_tail: list[str] = []

    def _drain_stderr():
        assert proc.stderr is not None
        buf = ""
        for line in proc.stderr:
            buf += line
            if len(buf) > MAX_STDERR_CHARS:
                buf = buf[-MAX_STDERR_CHARS:]
        stderr_tail.append(buf)

    t = threading.Thread(target=_drain_stderr, daemon=True)
    t.start()

    # Parse machine-readable progress from stdout.
    assert proc.stdout is not None
    for line in proc.stdout:
        line = line.strip()
        if line.startswith("out_time_us=") and progress:
            raw = line.split("=", 1)[1]
            if raw.isdigit():
                progress(p.label, int(raw) / 1_000_000, p.total_seconds)
        elif line == "progress=end" and progress:
            progress(p.label, p.total_seconds or 0.0, p.total_seconds)

    proc.wait()
    t.join(timeout=1)
    if proc.returncode != 0:
        tail = (stderr_tail[0] if stderr_tail else "").strip()
        tail = "\n".join(tail.splitlines()[-8:])  # last 8 lines, like upstream
        raise FFmpegError(cmd, proc.returncode, tail)
