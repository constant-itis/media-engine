"""Audio operations. Recipes transcribed from recon/INVENTORY-operations.md.

Follow the pattern established in engine/ops/video.py: a _build_<op>(ctx)->list[Pass]
plus an Operation(...) appended to OPS. Use *META_PRESERVE for metadata carry-forward.
"""
from __future__ import annotations

from typing import Optional

from ..ffmpeg import Pass
from ..opmodel import META_PRESERVE, Context, MediaInfo, Operation, Param

OPS: list[Operation] = []

# --- shared audio recipe helpers -------------------------------------------

# format -> (ext, codec args, supports -b:a bitrate)
_AUDIO_FORMAT_CONFIG = {
    "mp3": ("mp3", ["-c:a", "libmp3lame"], True),
    "wav": ("wav", ["-c:a", "pcm_s16le"], False),
    "flac": ("flac", ["-c:a", "flac"], False),
    "ogg": ("ogg", ["-c:a", "libvorbis"], True),
    "opus": ("opus", ["-c:a", "libopus"], True),
    "aac": ("aac", ["-c:a", "aac"], True),
    "m4a": ("m4a", ["-c:a", "aac"], True),
    "aiff": ("aiff", ["-c:a", "pcm_s16be"], False),
}

# BUG FIX (inventory-flagged): trim_audio and audio_settings never picked an
# explicit audio codec upstream, relying on ffmpeg's default-encoder-by-extension
# guess for the (hardcoded) output container. That's fragile across ffmpeg builds/
# versions. Both ops below are hardcoded to mp3 per the recipe, so we set the codec
# explicitly via this map keyed by output extension.
_EXT_CODEC = {
    "mp3": "libmp3lame",
    "wav": "pcm_s16le",
    "flac": "flac",
    "ogg": "libvorbis",
    "opus": "libopus",
    "aac": "aac",
    "m4a": "aac",
    "aiff": "pcm_s16be",
}


def _audio_tempo_chain(factor: float) -> str:
    # atempo accepts 0.5..2.0 per stage; chain halving to escape the ceiling.
    remaining = max(0.5, min(100.0, factor))
    parts = []
    while remaining > 2.0:
        parts.append("atempo=2")
        remaining /= 2.0
    parts.append(f"atempo={max(0.5, min(2.0, remaining)):.6g}")
    return ",".join(parts)


# --- convert_audio -----------------------------------------------------------


def _build_convert_audio(ctx: Context) -> list[Pass]:
    fmt = ctx.params["audioFormat"]
    ext, codec_args, supports_bitrate = _AUDIO_FORMAT_CONFIG[fmt]

    filters = []
    if ctx.params["normalizeLoudness"]:
        filters.append("loudnorm=I=-16:TP=-1.5:LRA=11")
    boost = ctx.params["volumeBoostPercent"]
    if boost > 0:
        filters.append(f"volume={1 + boost / 100:.6g}")

    args = ["-i", ctx.input, "-map", "0:a:0?"]
    if filters:
        args += ["-af", ",".join(filters)]
    args += ["-ar", str(ctx.params["outputSampleRate"])]
    args += codec_args
    if supports_bitrate and ctx.params["bitrate"]:
        args += ["-b:a", ctx.params["bitrate"]]
    args += [ctx.output]
    return [Pass(args, label="convert_audio")]


OPS.append(Operation(
    id="convert_audio", category="audio",
    description="Transcode to a target audio format with optional loudness normalize / volume boost.",
    params=[
        Param("audioFormat", "choice", "mp3",
              choices=["mp3", "wav", "flac", "ogg", "opus", "aac", "m4a", "aiff"]),
        Param("normalizeLoudness", "bool", False, help="EBU R128 loudnorm I=-16:TP=-1.5:LRA=11"),
        Param("volumeBoostPercent", "int", 0, min=0, max=200),
        Param("outputSampleRate", "int", 44100, help="Hz; must be valid for chosen format/encoder"),
        Param("bitrate", "choice", "320k",
              choices=["96k", "128k", "160k", "192k", "256k", "320k"],
              help="only applied if format supports a bitrate"),
    ],
    build=_build_convert_audio,
    output_ext=lambda p: _AUDIO_FORMAT_CONFIG[p["audioFormat"]][0],
    needs_probe=False,
))


# --- trim_audio --------------------------------------------------------------


def _build_trim_audio(ctx: Context) -> list[Pass]:
    codec = _EXT_CODEC["mp3"]
    args = [
        "-ss", ctx.params["startTime"], "-t", ctx.params["duration"],
        "-i", ctx.input, "-map", "0:a:0?",
        "-c:a", codec,
        ctx.output,
    ]
    info = ctx.info or MediaInfo()
    return [Pass(args, label="trim_audio", total_seconds=info.duration)]


OPS.append(Operation(
    id="trim_audio", category="audio",
    description="Cut a segment from an audio file (start + duration).",
    params=[
        Param("startTime", "str", "00:00:00", help="HH:MM:SS"),
        Param("duration", "str", "00:00:10", help="HH:MM:SS"),
    ],
    build=_build_trim_audio,
    output_ext=lambda p: "mp3",
    needs_probe=True,
))


# --- audio_settings ------------------------------------------------------------


def _build_audio_settings(ctx: Context) -> list[Pass]:
    info = ctx.info or MediaInfo()
    mode = ctx.params["audioChannelMode"]
    reverse = ctx.params["reverseAudioEnabled"]
    fade_in = ctx.params["fadeInDuration"]
    fade_out = ctx.params["fadeOutDuration"]

    if mode == "keep" and not reverse and fade_in <= 0 and fade_out <= 0:
        raise ValueError("audio_settings: no actual change requested "
                          "(channelMode=keep, reverse=false, fadeIn<=0, fadeOut<=0)")
    if fade_out > 0:
        if not info.duration:
            raise ValueError("audio_settings: fadeOutDuration requires known source duration")
        if fade_out >= info.duration:
            raise ValueError("audio_settings: fadeOutDuration must be < source duration")

    filters = []
    if mode == "mono":
        filters.append("aformat=channel_layouts=mono")
    elif mode == "stereo":
        filters.append("aformat=channel_layouts=stereo")
    if reverse:
        filters.append("areverse")
    if fade_in > 0:
        filters.append(f"afade=t=in:st=0:d={fade_in:.6g}")
    if fade_out > 0:
        start = max(0.0, info.duration - fade_out)
        filters.append(f"afade=t=out:st={start:.6g}:d={fade_out:.6g}")

    codec = _EXT_CODEC["mp3"]
    args = ["-i", ctx.input, "-map", "0:a:0?"]
    if filters:
        args += ["-af", ",".join(filters)]
    args += ["-c:a", codec, ctx.output]
    return [Pass(args, label="audio_settings", total_seconds=info.duration)]


OPS.append(Operation(
    id="audio_settings", category="audio",
    description="Channel-layout change, full reverse, and/or fade in/out in one pass.",
    params=[
        Param("audioChannelMode", "choice", "keep", choices=["keep", "mono", "stereo"]),
        Param("reverseAudioEnabled", "bool", False),
        Param("fadeInDuration", "float", 0, min=0, max=30),
        Param("fadeOutDuration", "float", 0, min=0, max=30),
    ],
    build=_build_audio_settings,
    output_ext=lambda p: "mp3",
    needs_probe=True,
))


# --- add_metadata --------------------------------------------------------------

# ext -> (canWriteText, canAddCover, canRemoveCover)
_METADATA_SUPPORT = {
    "mp3": (True, True, True),
    "flac": (True, False, True),
    "m4a": (True, False, True),
    "ogg": (True, False, False),
    "opus": (True, False, False),
    "wav": (False, False, False),
    "aiff": (False, False, False),
    "aif": (False, False, False),
    "aac": (False, False, False),
}


def _build_add_metadata(ctx: Context) -> list[Pass]:
    import os as _os

    remove_all = ctx.params["removeAllMetadata"]
    cover_image = ctx.params["coverImage"]
    remove_cover = ctx.params["removeCover"]
    artist = ctx.params["metadataArtist"]
    title = ctx.params["metadataTitle"]
    release_date = ctx.params["metadataReleaseDate"]
    genre = ctx.params["metadataGenre"]

    if remove_all and (cover_image or remove_cover or artist or title or release_date or genre):
        raise ValueError("add_metadata: removeAllMetadata cannot be combined with new metadata or cover")

    ext = _os.path.splitext(ctx.input)[1].lstrip(".").lower()
    if ext == "m4b":
        ext = "m4a"
    can_text, can_add_cover, can_remove_cover = _METADATA_SUPPORT.get(ext, (False, False, False))

    # Case A: remove all metadata/chapters, stream-copy.
    if remove_all:
        args = ["-i", ctx.input, "-map", "0:a?", "-map_metadata", "-1",
                 "-map_chapters", "-1", "-c:a", "copy", ctx.output]
        return [Pass(args, label="add_metadata")]

    has_change = bool(cover_image or remove_cover or artist or title or release_date or genre)

    # Case B: no actual change requested -> plain copy (no ffmpeg).
    # Case C: requested change unsupported by container -> also plain copy.
    wants_cover = bool(cover_image) or remove_cover
    cover_supported = (not wants_cover) or (cover_image and can_add_cover) or (remove_cover and can_remove_cover)
    wants_text = bool(artist or title or release_date or genre)
    text_supported = (not wants_text) or can_text

    if not has_change or not (cover_supported and text_supported):
        # Recipe uses a plain filesystem copy here (no ffmpeg invoked). The engine's
        # Pass abstraction only runs ffmpeg, so emulate it with a lossless full-stream
        # copy (no re-encode, metadata/chapters preserved as-is) to the same effect.
        args = ["-i", ctx.input, "-map", "0", "-c", "copy", ctx.output]
        return [Pass(args, label="plain_copy")]

    # Case D: normal write.
    should_add_cover = bool(cover_image) and can_add_cover
    should_remove_cover = remove_cover and can_remove_cover

    if should_add_cover:
        args = ["-i", ctx.input, "-i", cover_image, "-map", "0:a:0?", "-map", "1:v:0?",
                "-c:a", "copy", "-c:v", "mjpeg", "-id3v2_version", "3",
                "-disposition:v:0", "attached_pic"]
    elif should_remove_cover:
        args = ["-i", ctx.input, "-map", "0:a?", "-c:a", "copy"]
    else:
        args = ["-i", ctx.input, "-map", "0", "-c", "copy"]

    if can_text:
        if artist:
            args += ["-metadata", f"artist={artist}"]
        if title:
            args += ["-metadata", f"title={title}"]
        if release_date:
            digits = "".join(c for c in release_date if c.isdigit())
            args += ["-metadata", f"date={digits}"]
        if genre:
            args += ["-metadata", f"genre={genre}"]

    args += [ctx.output]
    return [Pass(args, label="add_metadata")]


OPS.append(Operation(
    id="add_metadata", category="audio",
    description="Write/remove ID3-style text metadata and embed/strip cover art, per container support.",
    params=[
        Param("coverImage", "path", None, help="cover image to embed (if container supports it)"),
        Param("metadataArtist", "str", ""),
        Param("metadataTitle", "str", ""),
        Param("metadataReleaseDate", "str", "", help="digits only; non-digits stripped"),
        Param("metadataGenre", "str", ""),
        Param("removeCover", "bool", False),
        Param("removeAllMetadata", "bool", False,
              help="mutually exclusive with any other metadata/cover change"),
    ],
    build=_build_add_metadata,
    # Recipe: output ext = input ext (m4b->m4a aliased), never changes container.
    # output_ext(params) only ever sees the params dict (see server.py:41 — called
    # with {} for advisory naming; the real output path is always explicit), so it
    # can't inspect the input path. Default to the most common case; build() derives
    # the real behavior (same-as-input) from ctx.input at run time regardless.
    output_ext=lambda p: "mp3",
    needs_probe=False,
))


# --- audio_speed ---------------------------------------------------------------


def _build_audio_speed(ctx: Context) -> list[Pass]:
    # Consistency fix over upstream: upstream's UI said "percent" but the builder
    # clamped a raw 0.5-2.0 factor (inventory-flagged confusion). We use the SAME
    # percent convention as video_speed (-100..100) and reuse the atempo chain so
    # values past 2x/below 0.5x work instead of silently clamping.
    info = ctx.info or MediaInfo()
    pct = ctx.params["speed"]
    factor = (1 + pct / 100) if pct >= 0 else 1 / (1 + abs(pct) / 100)
    args = ["-i", ctx.input, "-map", "0:a:0?", "-filter:a", _audio_tempo_chain(factor), ctx.output]
    return [Pass(args, label="audio_speed", total_seconds=info.duration)]


OPS.append(Operation(
    id="audio_speed", category="audio",
    description="Change audio tempo (atempo-chained; consistent percent scale with video_speed).",
    params=[Param("speed", "int", 0, min=-100, max=100, help="percent; +faster, -slower, 0 none")],
    build=_build_audio_speed,
    output_ext=lambda p: "mp3",
    needs_probe=True,
))
