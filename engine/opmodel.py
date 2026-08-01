"""Shared operation model + common recipe helpers.

Category modules (engine/ops/{video,audio,image}.py) import from here and define
their Operation lists; engine/operations.py aggregates them. Kept separate to
avoid a circular import (opmodel <- ops/* <- operations).
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Callable, Optional

from .ffmpeg import Pass, fps_mode_args
from .probe import MediaInfo

# version-appropriate `-fps_mode passthrough` (>=5.0) / `-vsync passthrough` (older)
FPS_MODE = fps_mode_args()

# metadata carry-forward appended before the output path by most transforms
META_PRESERVE = ["-map_metadata", "0", "-map_chapters", "0"]


@dataclass
class Param:
    name: str
    kind: str  # 'int' | 'float' | 'bool' | 'choice' | 'path' | 'str'
    default: object = None
    choices: Optional[list] = None
    min: Optional[float] = None
    max: Optional[float] = None
    required: bool = False
    help: str = ""

    def coerce(self, raw):
        if raw is None:
            if self.required:
                raise ValueError(f"'{self.name}' is required")
            return self.default
        if self.kind == "bool":
            return raw if isinstance(raw, bool) else str(raw).lower() in ("1", "true", "yes", "on")
        if self.kind == "int":
            v = int(raw)
        elif self.kind == "float":
            v = float(raw)
        elif self.kind == "choice":
            v = type(self.default)(raw) if self.default is not None else raw
            if self.choices and v not in self.choices:
                raise ValueError(f"'{self.name}' must be one of {self.choices}, got {v!r}")
            return v
        elif self.kind == "path":
            if not os.path.isfile(raw):
                raise ValueError(f"'{self.name}' file not found: {raw}")
            return raw
        else:
            return raw
        if self.min is not None and v < self.min:
            raise ValueError(f"'{self.name}' must be >= {self.min}")
        if self.max is not None and v > self.max:
            raise ValueError(f"'{self.name}' must be <= {self.max}")
        return v


@dataclass
class Context:
    input: str
    output: str
    params: dict
    info: Optional[MediaInfo]
    _temps: list = field(default_factory=list)

    def tempfile(self, suffix: str) -> str:
        base = os.path.splitext(os.path.basename(self.output))[0]
        path = os.path.join(
            os.path.dirname(os.path.abspath(self.output)),
            f".{base}-tmp{len(self._temps)}{suffix}",
        )
        self._temps.append(path)
        return path


@dataclass
class Operation:
    id: str
    category: str  # 'video' | 'audio' | 'image'
    description: str
    params: list[Param]
    build: Callable[[Context], list[Pass]]
    output_ext: Callable[[dict], str]
    needs_probe: bool = False

    def validate(self, raw_params: dict) -> dict:
        out = {}
        known = {p.name for p in self.params}
        for extra in set(raw_params) - known:
            raise ValueError(f"unknown param '{extra}' for op '{self.id}'")
        for p in self.params:
            out[p.name] = p.coerce(raw_params.get(p.name))
        return out
