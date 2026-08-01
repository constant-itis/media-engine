"""Image operations. Recipes transcribed from recon/INVENTORY-operations.md.

Follow the pattern established in engine/ops/video.py: a _build_<op>(ctx)->list[Pass]
plus an Operation(...) appended to OPS. Single-frame image ops usually don't need
probe info (needs_probe=False) unless the recipe requires source dimensions.
"""
from __future__ import annotations

from typing import Optional

from ..ffmpeg import Pass
from ..opmodel import Context, MediaInfo, Operation, Param

OPS: list[Operation] = []

# --- shared image recipe helpers -------------------------------------------

_IMAGE_FORMAT_CONFIG = {
    "jpg": ["-c:v", "mjpeg", "-q:v", "2"],
    "png": ["-c:v", "png"],
    "webp": ["-c:v", "libwebp", "-lossless", "0", "-quality", "92"],
    "bmp": ["-c:v", "bmp"],
    "tiff": ["-c:v", "tiff"],
}

_RATIOS = {
    "1:1": 1 / 1, "16:9": 16 / 9, "9:16": 9 / 16, "4:3": 4 / 3, "3:4": 3 / 4,
    "3:2": 3 / 2, "2:3": 2 / 3, "5:4": 5 / 4, "4:5": 4 / 5, "21:9": 21 / 9,
    "9:21": 9 / 21, "2:1": 2 / 1, "1:2": 1 / 2, "18:9": 18 / 9, "9:18": 9 / 18,
    "19.5:9": 19.5 / 9, "9:19.5": 9 / 19.5, "20:9": 20 / 9, "9:20": 9 / 20,
    "5:7": 5 / 7, "7:5": 7 / 5,
}


# --- resize_image ------------------------------------------------------------


def _build_resize_image(ctx: Context) -> list[Pass]:
    info = ctx.info or MediaInfo()
    preset = ctx.params["imageResizePreset"]
    fmt = ctx.params["imageOutputFormat"]

    if preset == "manual":
        target_w = ctx.params["imageCustomWidth"]
        target_h = ctx.params["imageCustomHeight"]
    else:
        w_str, h_str = preset.lower().split("x")
        target_w, target_h = int(w_str), int(h_str)

    src_w, src_h = info.width, info.height
    if src_w and src_h:
        source_ratio = src_w / src_h
        target_ratio = target_w / target_h
        if abs(source_ratio - target_ratio) > 0.01:
            raise ValueError("Selected size requires confirming an image crop")

    upscale = bool((src_w and target_w > src_w) or (src_h and target_h > src_h))
    scale = f"scale={target_w}:{target_h}"
    if upscale:
        scale += ":flags=lanczos"

    args = ["-i", ctx.input, "-vf", scale, *_IMAGE_FORMAT_CONFIG[fmt], "-frames:v", "1"]
    if fmt == "png":
        args += ["-update", "1"]
    args.append(ctx.output)
    return [Pass(args, label="resize")]


OPS.append(Operation(
    id="resize_image", category="image",
    description="Resize/reformat a still image to a target aspect ratio and size.",
    params=[
        Param("imageAspectRatio", "choice", "1:1", choices=list(_RATIOS.keys()),
              help="target aspect ratio"),
        Param("imageResizePreset", "str", "1080x1080",
              help="'manual' or a WxH preset string"),
        Param("imageCustomWidth", "int", 1080, min=1, max=8192, help="used when preset is manual"),
        Param("imageCustomHeight", "int", 1080, min=1, max=8192, help="used when preset is manual"),
        Param("imageOutputFormat", "choice", "png",
              choices=["jpg", "png", "webp", "bmp", "tiff"]),
    ],
    build=_build_resize_image,
    output_ext=lambda p: p.get("imageOutputFormat", "png"),
    needs_probe=True,
))


# --- rotate_image -------------------------------------------------------------


def _build_rotate_image(ctx: Context) -> list[Pass]:
    rotation = ctx.params["rotation"]
    flip_h = ctx.params["flipHorizontal"]
    flip_v = ctx.params["flipVertical"]

    filters = []
    if rotation == "cw":
        filters.append("transpose=1")
    elif rotation == "ccw":
        filters.append("transpose=2")
    elif rotation == "180":
        filters += ["hflip", "vflip"]
    if flip_h:
        filters.append("hflip")
    if flip_v:
        filters.append("vflip")

    args = ["-i", ctx.input]
    if filters:
        args += ["-vf", ",".join(filters)]
    args += ["-frames:v", "1", "-update", "1", ctx.output]
    return [Pass(args, label="rotate")]


OPS.append(Operation(
    id="rotate_image", category="image",
    description="Rotate an image 90/180 degrees and/or flip horizontally/vertically.",
    params=[
        Param("rotation", "choice", "none", choices=["none", "cw", "ccw", "180"]),
        Param("flipHorizontal", "bool", False),
        Param("flipVertical", "bool", False),
    ],
    build=_build_rotate_image,
    output_ext=lambda p: "png",
    needs_probe=False,
))


# --- grayscale_image -----------------------------------------------------------

_BW_PRESETS = {
    "classic": (1.0, 0.0),
    "bright": (0.92, 0.035),
    "contrast": (1.18, 0.0),
    "dark": (1.08, -0.045),
    "soft": (0.85, 0.015),
}


def _build_grayscale_image(ctx: Context) -> list[Pass]:
    mode = ctx.params["blackWhiteMode"]
    intensity = ctx.params["blackWhiteIntensity"] / 100.0
    contrast_pct = ctx.params["blackWhiteContrast"]
    brightness_pct = ctx.params["blackWhiteBrightness"]

    keep = 1 - intensity
    luma_r = 0.299 * intensity
    luma_g = 0.587 * intensity
    luma_b = 0.114 * intensity
    mixer = (
        "colorchannelmixer="
        f"{keep + luma_r:.6g}:{luma_g:.6g}:{luma_b:.6g}:0:"
        f"{luma_r:.6g}:{keep + luma_g:.6g}:{luma_b:.6g}:0:"
        f"{luma_r:.6g}:{luma_g:.6g}:{keep + luma_b:.6g}:0"
    )

    preset_contrast, preset_brightness = _BW_PRESETS[mode]
    user_contrast = (100 + (contrast_pct / 100) * 50) / 100
    user_brightness = (brightness_pct / 100) * 30 / 250
    final_contrast = max(0.5, min(1.5, preset_contrast * user_contrast))
    final_brightness = max(-0.3, min(0.3, preset_brightness + user_brightness))

    filt = mixer
    if final_contrast != 1 or final_brightness != 0:
        filt += f",eq=contrast={final_contrast:.6g}:brightness={final_brightness:.6g}"

    args = ["-i", ctx.input, "-vf", filt, "-frames:v", "1", "-update", "1", ctx.output]
    return [Pass(args, label="grayscale")]


OPS.append(Operation(
    id="grayscale_image", category="image",
    description="Black-and-white conversion via intensity-blended color mixer plus preset contrast/brightness.",
    params=[
        Param("blackWhiteMode", "choice", "classic",
              choices=["classic", "bright", "contrast", "dark", "soft"]),
        Param("blackWhiteIntensity", "int", 100, min=0, max=100),
        Param("blackWhiteContrast", "int", 0, min=-100, max=100),
        Param("blackWhiteBrightness", "int", 0, min=-100, max=100),
    ],
    build=_build_grayscale_image,
    output_ext=lambda p: "png",
    needs_probe=False,
))


# --- sharpness_image -----------------------------------------------------------


def _build_sharpness_image(ctx: Context) -> list[Pass]:
    sharpen_pct = ctx.params["sharpenAmount"]
    blur_pct = ctx.params["blurStrength"]

    filters = []
    if blur_pct:
        filters.append(f"boxblur={blur_pct * 0.4:.6g}")
    if sharpen_pct:
        # unsharp luma_amount is valid only in [-2, 5]; the inventory's *0.2
        # (0..20) overshoots and errors on every ffmpeg. Map 0-100% -> 0..5.
        filters.append(f"unsharp=5:5:{sharpen_pct * 0.05:.6g}:5:5:0.0")

    args = ["-i", ctx.input]
    if filters:
        args += ["-vf", ",".join(filters)]
    args += ["-frames:v", "1", "-update", "1", ctx.output]
    return [Pass(args, label="sharpness")]


OPS.append(Operation(
    id="sharpness_image", category="image",
    description="Apply box blur and/or unsharp-mask sharpening to a still image.",
    params=[
        Param("sharpenAmount", "int", 0, min=0, max=100, help="0-100%, mapped to unsharp luma amount 0-5"),
        Param("blurStrength", "int", 0, min=0, max=100, help="0-100%, mapped to boxblur 0-40"),
    ],
    build=_build_sharpness_image,
    output_ext=lambda p: "png",
    needs_probe=False,
))
