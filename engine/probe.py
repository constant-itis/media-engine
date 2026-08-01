"""ffprobe wrapper -> MediaInfo. Ops that need source width/bitrate/audio-presence
(watermark, speed) consume this; ops that don't (gif) skip it.
"""
from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from typing import Optional

from .ffmpeg import resolve_ffprobe

# Mirrors the ripped app's runFfprobeJson entry list.
_SHOW_ENTRIES = (
    "stream=index,codec_type,codec_name,pix_fmt,bit_rate,duration,"
    "avg_frame_rate,r_frame_rate,width,height,sample_rate,channels:"
    "stream_disposition=attached_pic:format=bit_rate,duration"
)


@dataclass
class MediaInfo:
    width: Optional[int] = None
    height: Optional[int] = None
    duration: Optional[float] = None
    video_codec: Optional[str] = None
    audio_codec: Optional[str] = None
    video_bitrate_k: Optional[int] = None  # kbps
    audio_bitrate_k: Optional[int] = None  # kbps
    has_audio: bool = False


def _first_finite(*vals) -> Optional[float]:
    for v in vals:
        try:
            f = float(v)
            if f == f and f not in (float("inf"), float("-inf")):
                return f
        except (TypeError, ValueError):
            continue
    return None


def probe(path: str) -> MediaInfo:
    cmd = [
        resolve_ffprobe(), "-v", "error",
        "-show_entries", _SHOW_ENTRIES,
        "-of", "json", path,
    ]
    out = subprocess.run(cmd, capture_output=True, text=True)
    if out.returncode != 0:
        # Graceful: an op that needs probe data falls back to bitrate-unknown branch.
        return MediaInfo()
    data = json.loads(out.stdout or "{}")
    streams = data.get("streams", [])
    fmt = data.get("format", {})

    info = MediaInfo()
    info.duration = _first_finite(fmt.get("duration"))

    for s in streams:
        if s.get("codec_type") == "video" and info.width is None:
            # skip cover-art "video" streams
            if s.get("disposition", {}).get("attached_pic"):
                continue
            info.width = int(s["width"]) if s.get("width") else None
            info.height = int(s["height"]) if s.get("height") else None
            info.video_codec = s.get("codec_name")
            br = _first_finite(s.get("bit_rate"))
            info.video_bitrate_k = int(br / 1000) if br else None
            info.duration = info.duration or _first_finite(s.get("duration"))
        elif s.get("codec_type") == "audio" and not info.has_audio:
            info.has_audio = True
            info.audio_codec = s.get("codec_name")
            br = _first_finite(s.get("bit_rate"))
            info.audio_bitrate_k = int(br / 1000) if br else None

    return info
