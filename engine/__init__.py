"""media-engine: a standalone, headless ffmpeg operation engine.

Recipes ripped from media-by-outlaw2082 (GPL-3.0) and reimplemented clean in
stdlib Python. Drives from any face — CLI today, MCP next.
"""
from .operations import OPERATIONS, run_operation
from .ffmpeg import FFmpegError

__all__ = ["OPERATIONS", "run_operation", "FFmpegError"]
