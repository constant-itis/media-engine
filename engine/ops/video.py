"""Video operations. Recipes transcribed from recon/INVENTORY-operations.md.

PATTERN (follow this for every op):
  1. a _build_<op>(ctx) -> list[Pass] function that returns one or more ffmpeg
     Passes. Read params from ctx.params[...]; probe info from ctx.info (a
     MediaInfo, or None if the op sets needs_probe=False). Use ctx.tempfile(sfx)
     for intermediate files (auto-cleaned).
  2. an Operation(...) entry appended to OPS with its Param specs, build fn,
     output_ext(params)->str, and needs_probe.
Append `*FPS_MODE` (not a literal "-fps_mode") wherever the recipe uses fps_mode,
and `*META_PRESERVE` for the -map_metadata/-map_chapters carry-forward.
"""
from __future__ import annotations

from typing import Optional

from ..ffmpeg import Pass
from ..opmodel import FPS_MODE, META_PRESERVE, Context, MediaInfo, Operation, Param

OPS: list[Operation] = []

# --- shared video recipe helpers ------------------------------------------

_GIF_HEIGHTS = {"240p": 240, "360p": 360, "480p": 480, "720p": 720, "1080p": 1080, "original": None}


def _animated_filter(fps: int, height: Optional[int]) -> str:
    f = f"fps={fps}"
    if height is not None:
        f += f",scale=-2:{height}:flags=lanczos"
    return f


def _audio_tempo_chain(factor: float) -> str:
    # atempo accepts 0.5..2.0 per stage; chain halving to escape the ceiling.
    remaining = max(0.5, min(100.0, factor))
    parts = []
    while remaining > 2.0:
        parts.append("atempo=2")
        remaining /= 2.0
    parts.append(f"atempo={max(0.5, min(2.0, remaining)):.6g}")
    return ",".join(parts)


_SPEED_CODEC = {"mp3": "libmp3lame", "opus": "libopus", "vorbis": "libvorbis"}

_WM_CORNER = {
    "top-left": ("20", "20"),
    "top-right": ("main_w-overlay_w-20", "20"),
    "bottom-left": ("20", "main_h-overlay_h-20"),
    "bottom-right": ("main_w-overlay_w-20", "main_h-overlay_h-20"),
}


# --- video_to_gif ----------------------------------------------------------


def _build_gif(ctx: Context) -> list[Pass]:
    fps = ctx.params["gifFpsPreset"]
    height = _GIF_HEIGHTS[ctx.params["gifResolutionPreset"]]
    filt = _animated_filter(fps, height)

    if ctx.params["gifExportWebp"]:
        return [Pass([
            "-i", ctx.input, "-vf", filt, "-loop", "0",
            "-c:v", "libwebp", "-quality", "80", "-compression_level", "6",
            ctx.output,
        ], label="webp")]

    palette = ctx.tempfile(".png")
    return [
        Pass(["-i", ctx.input, "-vf", f"{filt},palettegen=stats_mode=diff", palette],
             label="palettegen"),
        Pass(["-i", ctx.input, "-i", palette,
              "-lavfi", f"{filt}[x];[x][1:v]paletteuse=dither=bayer:bayer_scale=5:diff_mode=rectangle",
              "-loop", "0", ctx.output], label="paletteuse"),
    ]


OPS.append(Operation(
    id="video_to_gif", category="video",
    description="Video to animated GIF (two-pass palette) or WebP.",
    params=[
        Param("gifFpsPreset", "choice", 12, choices=[8, 10, 12, 15, 24, 25, 30, 50, 60], help="frames per second"),
        Param("gifResolutionPreset", "choice", "480p",
              choices=["240p", "360p", "480p", "720p", "1080p", "original"], help="output height preset"),
        Param("gifExportWebp", "bool", False, help="emit animated WebP instead of GIF"),
    ],
    build=_build_gif,
    output_ext=lambda p: "webp" if p.get("gifExportWebp") else "gif",
    needs_probe=False,
))


# --- video_speed -----------------------------------------------------------


def _build_speed(ctx: Context) -> list[Pass]:
    pct = ctx.params["speed"]
    if pct == 0:
        vfac = afac = 1.0
    elif pct > 0:
        speed = 1 + pct / 100
        vfac, afac = 1 / speed, speed
    else:
        speed = 1 + abs(pct) / 100
        vfac, afac = speed, 1 / speed

    info = ctx.info or MediaInfo()
    args = ["-i", ctx.input, "-map", "0:v:0", "-map", "0:a?", "-vf", f"setpts={vfac:.6g}*PTS"]
    if info.has_audio:
        codec = _SPEED_CODEC.get((info.audio_codec or "").lower(), "aac")
        args += ["-af", _audio_tempo_chain(afac), "-c:a", codec]
        if info.audio_bitrate_k:
            args += ["-b:a", f"{info.audio_bitrate_k}k"]
    else:
        args += ["-an"]
    if info.video_bitrate_k:
        b = info.video_bitrate_k
        args += ["-c:v", "libx264", "-b:v", f"{b}k", "-maxrate", f"{b}k", "-bufsize", f"{max(2 * b, b)}k"]
    else:
        args += ["-c:v", "libx264", "-crf", "18", "-preset", "medium"]
    args += [*FPS_MODE, "-movflags", "+faststart", *META_PRESERVE, ctx.output]
    return [Pass(args, label="speed", total_seconds=info.duration)]


OPS.append(Operation(
    id="video_speed", category="video",
    description="Speed up / slow down video + audio in sync (atempo-chained).",
    params=[Param("speed", "int", 0, min=-100, max=100, help="percent; +faster, -slower, 0 none")],
    build=_build_speed,
    output_ext=lambda p: "mp4",
    needs_probe=True,
))


# --- add_watermark ---------------------------------------------------------


def _build_watermark(ctx: Context) -> list[Pass]:
    info = ctx.info or MediaInfo()
    pos = ctx.params["watermarkPosition"]
    size_pct = ctx.params["watermarkSize"]
    opacity = ctx.params["watermarkOpacity"] / 100.0

    if pos == "manual":
        x = f"main_w*{ctx.params['watermarkX']}/100"
        y = f"main_h*{ctx.params['watermarkY']}/100"
    else:
        x, y = _WM_CORNER[pos]

    wm_w = str(int(info.width * size_pct / 100)) if info.width else "iw"
    filt = (
        f"[1:v]scale={wm_w}:-1,format=rgba,colorchannelmixer=aa={opacity:.4g}[wm];"
        f"[0:v][wm]overlay=x={x}:y={y}:format=auto[v]"
    )
    args = ["-i", ctx.input, "-i", ctx.params["coverImage"],
            "-filter_complex", filt, "-map", "[v]", "-map", "0:a?"]
    if info.video_bitrate_k:
        b = info.video_bitrate_k
        args += ["-c:v", "libx264", "-b:v", f"{b}k", "-maxrate", f"{b}k", "-bufsize", f"{2 * b}k"]
    else:
        args += ["-c:v", "libx264", "-crf", "18", "-preset", "medium"]
    args += [*FPS_MODE, "-pix_fmt", "yuv420p", "-movflags", "+faststart",
             *META_PRESERVE, "-c:a", "copy", "-shortest", ctx.output]
    return [Pass(args, label="watermark", total_seconds=info.duration)]


OPS.append(Operation(
    id="add_watermark", category="video",
    description="Overlay a scaled, alpha-blended image watermark.",
    params=[
        Param("coverImage", "path", required=True, help="watermark image file"),
        Param("watermarkPosition", "choice", "top-right",
              choices=["top-left", "top-right", "bottom-left", "bottom-right", "manual"]),
        Param("watermarkSize", "int", 18, min=1, max=100, help="percent of source width"),
        Param("watermarkOpacity", "int", 90, min=0, max=100),
        Param("watermarkX", "int", 0, min=0, max=100, help="manual X (percent)"),
        Param("watermarkY", "int", 0, min=0, max=100, help="manual Y (percent)"),
    ],
    build=_build_watermark,
    output_ext=lambda p: "mp4",
    needs_probe=True,
))

# --- convert_video ----------------------------------------------------------

_CONVERT_CODECS = {
    "mp4": ["h264", "h265"],
    "mov": ["h264", "h265"],
    "mkv": ["h264", "h265", "vp9"],
    "webm": ["vp9", "vp8"],
    "avi": ["mpeg4"],
}

_CONVERT_CODEC_ARGS = {
    "h264": ["-c:v", "libx264", "-pix_fmt", "yuv420p"],
    "h265": ["-c:v", "libx265", "-pix_fmt", "yuv420p"],
    "vp9": ["-c:v", "libvpx-vp9", "-pix_fmt", "yuv420p"],
    "vp8": ["-c:v", "libvpx", "-pix_fmt", "yuv420p"],
    "mpeg4": ["-c:v", "mpeg4"],
}

_CONVERT_FORMAT_AUDIO = {
    "mp4": ["-c:a", "aac"],
    "mkv": ["-c:a", "aac"],
    "webm": ["-c:a", "libopus"],
    "mov": ["-c:a", "aac"],
    "avi": ["-c:a", "libmp3lame"],
}

_CONVERT_FORMAT_EXTRA = {
    "mp4": ["-movflags", "+faststart"],
    "mkv": [],
    "webm": [],
    "mov": ["-movflags", "+faststart"],
    "avi": [],
}


def _build_convert(ctx: Context) -> list[Pass]:
    info = ctx.info or MediaInfo()
    fmt = ctx.params["videoFormat"]
    codec = ctx.params["videoCodec"]
    if codec not in _CONVERT_CODECS.get(fmt, [codec]):
        codec = _CONVERT_CODECS[fmt][0]

    bitrate = ctx.params["videoBitrate"]
    if bitrate == "source":
        bitrate = f"{info.video_bitrate_k}k" if info.video_bitrate_k else "8M"

    args = ["-i", ctx.input, *_CONVERT_CODEC_ARGS[codec], "-b:v", bitrate,
            *_CONVERT_FORMAT_AUDIO[fmt], *_CONVERT_FORMAT_EXTRA[fmt], ctx.output]
    return [Pass(args, label="convert", total_seconds=info.duration)]


OPS.append(Operation(
    id="convert_video", category="video",
    description="Change container format, codec, and bitrate.",
    params=[
        Param("videoFormat", "choice", "mp4", choices=["mp4", "mkv", "webm", "mov", "avi"]),
        Param("videoCodec", "choice", "h264", choices=["h264", "h265", "vp9", "vp8", "mpeg4"]),
        Param("videoBitrate", "str", "8M", help="e.g. '8M', or 'source' to reuse input bitrate"),
    ],
    build=_build_convert,
    output_ext=lambda p: p.get("videoFormat", "mp4"),
    needs_probe=True,
))


# --- resize_video ------------------------------------------------------------

_RESIZE_PRESETS = {
    "4k_landscape": (3840, 2160, "4k"),
    "fullhd_landscape": (1920, 1080, "fullhd"),
    "hd_landscape": (1280, 720, "hd"),
    "4k_portrait": (2160, 3840, "4k"),
    "fullhd_portrait": (1080, 1920, "fullhd"),
    "hd_portrait": (720, 1280, "hd"),
}

_RESIZE_FORMAT_CONFIG = {
    "mp4": {"vcodec": "libx264", "acodec": "aac", "abitrate": "192k", "h264": True},
    "mov": {"vcodec": "libx264", "acodec": "aac", "abitrate": "192k", "h264": True},
    "mkv": {"vcodec": "libx264", "acodec": "aac", "abitrate": "192k", "h264": True},
    "avi": {"vcodec": "mpeg4", "acodec": "libmp3lame", "abitrate": "192k", "h264": False},
    "webm": {"vcodec": "libvpx-vp9", "acodec": "libopus", "abitrate": "160k", "h264": False},
}

_RESIZE_BITRATE_TABLE = {
    "4k": {"avi": "45M", "webm": "20M", "mp4": "35M", "mov": "35M", "mkv": "35M"},
    "fullhd": {"avi": "12M", "webm": "6M", "mp4": "8M", "mov": "8M", "mkv": "8M"},
    "hd": {"avi": "7M", "webm": "3M", "mp4": "5M", "mov": "5M", "mkv": "5M"},
}


def _build_resize(ctx: Context) -> list[Pass]:
    info = ctx.info or MediaInfo()
    preset = ctx.params["resolutionPreset"]
    w, h, tier = _RESIZE_PRESETS[preset]
    fmt = ctx.params["resizeVideoFormat"]
    cfg = _RESIZE_FORMAT_CONFIG[fmt]
    bitrate = _RESIZE_BITRATE_TABLE[tier][fmt]

    filt = f"scale={w}:{h}:force_original_aspect_ratio=decrease,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:black"
    args = ["-i", ctx.input, "-vf", filt, "-c:v", cfg["vcodec"], "-b:v", bitrate]
    if cfg["h264"]:
        args += ["-pix_fmt", "yuv420p"]
    args += ["-c:a", cfg["acodec"], "-b:a", cfg["abitrate"], ctx.output]
    return [Pass(args, label="resize", total_seconds=info.duration)]


OPS.append(Operation(
    id="resize_video", category="video",
    description="Fit video into a preset resolution/orientation frame with letterbox padding.",
    params=[
        Param("resolutionPreset", "choice", "fullhd_landscape",
              choices=list(_RESIZE_PRESETS.keys())),
        Param("resizeVideoFormat", "choice", "mp4",
              choices=["mp4", "mov", "mkv", "avi", "webm"]),
    ],
    build=_build_resize,
    output_ext=lambda p: p.get("resizeVideoFormat", "mp4"),
    needs_probe=True,
))


# --- trim_video ---------------------------------------------------------------


def _build_trim(ctx: Context) -> list[Pass]:
    info = ctx.info or MediaInfo()
    start = ctx.params["startTime"]
    duration = ctx.params["duration"]
    args = ["-ss", start, "-t", duration, "-i", ctx.input,
            "-c:v", "libx264", "-c:a", "aac", ctx.output]
    return [Pass(args, label="trim", total_seconds=info.duration)]


OPS.append(Operation(
    id="trim_video", category="video",
    description="Cut a segment starting at startTime for duration (input-seeking, re-encodes).",
    params=[
        Param("startTime", "str", "00:00:00", help="HH:MM:SS[.fff]"),
        Param("duration", "str", "00:00:00", help="HH:MM:SS[.fff]"),
    ],
    build=_build_trim,
    output_ext=lambda p: "mp4",
    needs_probe=True,
))


# --- extract_audio -------------------------------------------------------------

_EXTRACT_AUDIO_FORMAT_CONFIG = {
    "mp3": ("mp3", ["-c:a", "libmp3lame"]),
    "aac": ("m4a", ["-c:a", "aac"]),
    "opus": ("opus", ["-c:a", "libopus"]),
    "ogg": ("ogg", ["-c:a", "libvorbis"]),
}

_ORIGINAL_AUDIO_EXT = {
    "aac": "m4a", "mp3": "mp3", "opus": "opus", "vorbis": "ogg",
    "flac": "flac", "alac": "m4a",
}


def _build_extract_audio(ctx: Context) -> list[Pass]:
    info = ctx.info or MediaInfo()
    action = ctx.params["extractAudioAction"]

    if action == "remove":
        args = ["-i", ctx.input, "-c:v", "copy", "-an", ctx.output]
        return [Pass(args, label="remove-audio", total_seconds=info.duration)]

    fmt = ctx.params["extractAudioFormat"]
    if fmt == "original":
        # single active audio stream (stream 0) — stream-copy, no transcode.
        args = ["-i", ctx.input, "-map", "0:a:0", "-c:a", "copy", ctx.output]
        return [Pass(args, label="extract-audio-copy", total_seconds=info.duration)]

    ext, codec_args = _EXTRACT_AUDIO_FORMAT_CONFIG[fmt]
    bitrate = ctx.params["extractAudioBitrate"]
    if bitrate == "source":
        bitrate = f"{info.audio_bitrate_k}k" if info.audio_bitrate_k else "128k"
    args = ["-i", ctx.input, "-map", "0:a:0", *codec_args, "-b:a", bitrate, ctx.output]
    return [Pass(args, label="extract-audio", total_seconds=info.duration)]


def _extract_audio_ext(p: dict) -> str:
    if p.get("extractAudioAction") == "remove":
        return "mp4"
    fmt = p.get("extractAudioFormat", "original")
    if fmt == "original":
        # Real ext depends on source codec (resolved via ffprobe at run time);
        # default to m4a (aac) as the common case when unresolved statically.
        return "m4a"
    return _EXTRACT_AUDIO_FORMAT_CONFIG[fmt][0]


OPS.append(Operation(
    id="extract_audio", category="video",
    description="Extract the audio track (optionally transcoding), or strip audio entirely.",
    params=[
        Param("extractAudioAction", "choice", "extract", choices=["extract", "remove"]),
        Param("extractAudioFormat", "choice", "original",
              choices=["original", "mp3", "aac", "opus", "ogg"]),
        Param("extractAudioBitrate", "str", "source",
              help="'source', or e.g. '128k'; ignored for format=original"),
    ],
    build=_build_extract_audio,
    output_ext=_extract_audio_ext,
    needs_probe=True,
))


# --- rotate_video ---------------------------------------------------------------

# BUGFIX vs. upstream: "mirror" was UI-exposed but had no entry in the filter
# map (silent no-op — selecting it did nothing). We map it to `hflip`, the
# conventional meaning of "mirror" for video (flip left-right, like a mirror
# held up to the frame). `180` already covers hflip+vflip combined, so mirror
# staying horizontal-only keeps the two options visually distinct.
_ROTATE_FILTERS = {
    "none": None,
    "cw": "transpose=1",
    "ccw": "transpose=2",
    "180": "hflip,vflip",
    "mirror": "hflip",
}


def _build_rotate(ctx: Context) -> list[Pass]:
    info = ctx.info or MediaInfo()
    rotation = ctx.params["rotation"]
    filt = _ROTATE_FILTERS[rotation]
    args = ["-i", ctx.input]
    if filt:
        args += ["-vf", filt]
    args += ["-c:v", "libx264", "-c:a", "aac", ctx.output]
    return [Pass(args, label="rotate", total_seconds=info.duration)]


OPS.append(Operation(
    id="rotate_video", category="video",
    description="Rotate 90 deg CW/CCW, 180, or mirror horizontally; always re-encodes.",
    params=[
        Param("rotation", "choice", "none",
              choices=["none", "cw", "ccw", "180", "mirror"]),
    ],
    build=_build_rotate,
    output_ext=lambda p: "mp4",
    needs_probe=True,
))


# --- grayscale_video (color/style grade) ----------------------------------------


def _color_style_preset_filters(mode: str, intensity: int) -> list[str]:
    f = intensity / 100.0
    if mode == "none" or f == 0:
        return []
    if mode == "grayscale":
        return [f"hue=s={1 - f:.6g}"]
    if mode == "grayscale_contrast":
        return [f"hue=s={1 - f:.6g}", f"eq=contrast={1 + 0.25 * f:.6g}"]
    if mode == "sepia":
        # interpolate identity matrix -> sepia matrix by f
        sepia = [0.393, 0.769, 0.189, 0, 0.349, 0.686, 0.168, 0, 0.272, 0.534, 0.131, 0]
        identity = [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0]
        mixed = [identity[i] + (sepia[i] - identity[i]) * f for i in range(12)]
        rr, rg, rb, ra, gr, gg, gb, ga, br, bg, bb, ba = (f"{v:.6g}" for v in mixed)
        return [f"colorchannelmixer={rr}:{rg}:{rb}:{ra}:{gr}:{gg}:{gb}:{ga}:{br}:{bg}:{bb}:{ba}"]
    if mode == "vintage":
        return [
            f"eq=contrast={1 + 0.15 * f:.6g}:saturation={1 - 0.25 * f:.6g}:brightness={0.02 * f:.6g}",
            f"colorbalance=rs={0.08 * f:.6g}:gs={0.03 * f:.6g}:bs={-0.05 * f:.6g}",
        ]
    if mode == "stronger_colors":
        return [f"eq=saturation={1 + 0.35 * f:.6g}"]
    if mode == "weaker_colors":
        return [f"eq=saturation={max(0.0, 1 - 0.35 * f):.6g}"]
    if mode == "brighter":
        return [f"eq=brightness={0.06 * f:.6g}"]
    if mode == "darker":
        return [f"eq=brightness={-0.06 * f:.6g}"]
    if mode == "higher_contrast":
        return [f"eq=contrast={1 + 0.25 * f:.6g}"]
    if mode == "warmer":
        return [f"colorbalance=rs={0.08 * f:.6g}:gs={0.03 * f:.6g}:bs={-0.04 * f:.6g}"]
    if mode == "cooler":
        return [f"colorbalance=rs={-0.05 * f:.6g}:gs=0:bs={0.08 * f:.6g}"]
    if mode == "negative":
        return ["negate"]
    return []


def _build_grayscale(ctx: Context) -> list[Pass]:
    info = ctx.info or MediaInfo()
    preset_filters = _color_style_preset_filters(ctx.params["styleMode"], ctx.params["styleIntensity"])

    b = ctx.params["brightnessAdjust"]
    c = ctx.params["contrastAdjust"]
    s = ctx.params["saturationAdjust"]
    advanced = []
    if b or c or s:
        advanced.append(f"eq=brightness={b / 250:.6g}:contrast={1 + c / 100:.6g}:saturation={max(0.0, 1 + s / 100):.6g}")

    filters = preset_filters + advanced

    args = ["-i", ctx.input, "-map", "0:v:0", "-map", "0:a?"]
    if filters:
        args += ["-vf", ",".join(filters)]
    if info.video_bitrate_k:
        vb = info.video_bitrate_k
        args += ["-b:v", f"{vb}k", "-maxrate", f"{vb}k", "-bufsize", f"{max(2 * vb, vb)}k"]
    else:
        args += ["-crf", "18", "-preset", "medium"]
    args += ["-c:v", "libx264", *FPS_MODE, "-movflags", "+faststart",
             *META_PRESERVE, "-c:a", "copy", ctx.output]
    return [Pass(args, label="color-style", total_seconds=info.duration)]


OPS.append(Operation(
    id="grayscale_video", category="video",
    description="Named color-grade preset plus manual brightness/contrast/saturation trim.",
    params=[
        Param("styleMode", "choice", "none",
              choices=["none", "grayscale", "grayscale_contrast", "sepia", "vintage",
                       "stronger_colors", "weaker_colors", "brighter", "darker",
                       "higher_contrast", "warmer", "cooler", "negative"]),
        Param("styleIntensity", "int", 100, min=0, max=100),
        Param("brightnessAdjust", "int", 0, min=-50, max=50),
        Param("contrastAdjust", "int", 0, min=-50, max=100),
        Param("saturationAdjust", "int", 0, min=-100, max=100),
    ],
    build=_build_grayscale,
    output_ext=lambda p: "mp4",
    needs_probe=True,
))


# --- burn_subtitles ---------------------------------------------------------------


def _escape_subtitle_path(path: str) -> str:
    return (path.replace("\\", "/")
                .replace(":", "\\:")
                .replace("'", "\\'")
                .replace("[", "\\[")
                .replace("]", "\\]"))


def _build_burn_subtitles(ctx: Context) -> list[Pass]:
    info = ctx.info or MediaInfo()
    escaped = _escape_subtitle_path(ctx.params["subtitles"])
    args = ["-i", ctx.input, "-vf", f"subtitles='{escaped}'",
            "-c:v", "libx264", "-c:a", "aac", ctx.output]
    return [Pass(args, label="burn-subtitles", total_seconds=info.duration)]


OPS.append(Operation(
    id="burn_subtitles", category="video",
    description="Hardcode SRT/VTT/ASS subtitle text into the video frame (irreversible).",
    params=[
        Param("subtitles", "path", required=True, help="path to .srt/.vtt/.ass file"),
    ],
    build=_build_burn_subtitles,
    output_ext=lambda p: "mp4",
    needs_probe=True,
))
