# Media by Outlaw2082 — FFmpeg Operation Inventory

Source: `media-by-outlaw2082` Electron app. Read-only inventory for reimplementation as a
standalone Python media-ops engine. All commands reconstructed from
`src/shared/ffmpeg-config.js` (OPERATIONS registry + arg builders), `electron/main.cjs`
(job orchestration/IPC), `electron/ipc-validation.cjs` (IPC-boundary validation), and
`src/shared/i18n/operation-help/en.js` (user-facing descriptions).

**Total: 19 operations** — 10 video, 5 audio, 4 image.

- **Video (10):** convert_video, video_to_gif, resize_video, trim_video, extract_audio, rotate_video, grayscale_video, add_watermark, burn_subtitles, video_speed
- **Audio (5):** convert_audio, trim_audio, audio_settings, add_metadata, audio_speed
- **Image (4):** resize_image, rotate_image, grayscale_image, sharpness_image

All commands are invoked as `[...FFMPEG_GLOBAL_ARGS, ...operationArgs]` where
`FFMPEG_GLOBAL_ARGS = ["-y", "-hide_banner", "-nostdin"]`, then passed through two
post-processing passes (`applyMetadataPreservationPolicy`, and conditionally
`applyEmbeddedArtworkPreservation`) before being handed to `spawn(ffmpegPath, args)`.
See "Global Patterns" at the end for what those passes inject.

---

## VIDEO OPERATIONS

### 1. `convert_video` — Format and quality
**Category:** video
**Description:** Changes video container format, codec, and bitrate. Primary "export" operation.

**Params** (`fields: ["videoFormat", "videoCodec", "videoBitrate"]`):
- `videoFormat`: select, one of `mp4|mkv|webm|mov|avi`, default `mp4`
- `videoCodec`: select, one of `h264|h265|vp9|vp8|mpeg4`, default `h264`. Constrained per-format via `getVideoFormatConfig(format).codecs`:
  - mp4/mov: `[h264, h265]`
  - mkv: `[h264, h265, vp9]`
  - webm: `[vp9, vp8]`
  - avi: `[mpeg4]`
  - If requested codec unsupported for format, falls back to format's first supported codec (`getNormalizedVideoCodec`).
- `videoBitrate`: select from `VIDEO_BITRATE_OPTIONS` (`1M`...`120M`) OR literal `"source"` (use input's own bitrate) OR any `\d+(\.\d+)?M` string. Default `8M`.

**Codec → ffmpeg args** (`getVideoCodecConfig`):
| codec | codecArgs |
|---|---|
| h264 | `-c:v libx264 -pix_fmt yuv420p` |
| h265 | `-c:v libx265 -pix_fmt yuv420p` |
| vp9  | `-c:v libvpx-vp9 -pix_fmt yuv420p` |
| vp8  | `-c:v libvpx -pix_fmt yuv420p` |
| mpeg4| `-c:v mpeg4` |

**Format → container config** (`getVideoFormatConfig`):
| format | audioArgs | extraOutputArgs |
|---|---|---|
| mp4 | `-c:a aac` | `-movflags +faststart` |
| mkv | `-c:a aac` | (none) |
| webm| `-c:a libopus` | (none) |
| mov | `-c:a aac` | `-movflags +faststart` |
| avi | `-c:a libmp3lame` | (none) |

**Bitrate resolution:** if `videoBitrate === "source"`, use `inputInfo.videoBitrateKbps` (from ffprobe) as `${kbps}k`; if unavailable, fall back to `getRecommendedVideoBitrate(inputInfo, format, codec)` — a lookup table by codec × resolution tier (SD/HD/FullHD/2K/4K/8K, tiers derived from ffprobe width/height).

**Exact command:**
```
-i <input>
<codecArgs...>              # e.g. -c:v libx264 -pix_fmt yuv420p
-b:v <bitrate>               # e.g. -b:v 8M
<audioArgs...>                # e.g. -c:a aac
<extraOutputArgs...>          # e.g. -movflags +faststart (mp4/mov only)
<output>
```
No `-vf`, no scaling — pure transcode, resolution preserved.

**Output ext:** matches `videoFormat`.

---

### 2. `video_to_gif` — Video to GIF/WebP
**Category:** video
**Description:** Creates an animated GIF (two-pass palette) or animated WebP from a video clip.

**Params** (`fields: ["gifFpsPreset", "gifResolutionPreset", "gifExportWebp"]`):
- `gifFpsPreset`: select from `8,10,12,15,24,25,30,50,60`, default `12`
- `gifResolutionPreset`: select from `240p,360p,480p,720p,1080p,original`, default `480p`
- `gifExportWebp`: boolean toggle, default `false`

**Shared filter builder** (`buildAnimatedImageVideoFilter`):
```
fps=<fpsValue>[,scale=-2:<heightFromPreset>:flags=lanczos]   # scale omitted if resolution=="original"
```

**If `gifExportWebp === false` → GIF, two-pass palette (`runGifTwoPassNativeJob` in main.cjs):**

Pass 1 — generate palette (`buildGifPaletteArgs`):
```
-i <input>
-vf "fps=<fps>,scale=-2:<h>:flags=lanczos,palettegen=stats_mode=diff"
<tempDir>/palette-<pid>-<ts>-<uuid>.png
```

Pass 2 — apply palette (`buildGifOutputArgs`), then metadata-preservation pass applied:
```
-i <input>
-i <palette.png>
-lavfi "fps=<fps>,scale=-2:<h>:flags=lanczos[x];[x][1:v]paletteuse=dither=bayer:bayer_scale=5:diff_mode=rectangle"
-loop 0
<output.gif>
```
Palette temp file deleted in a `finally` block after pass 2 (even on failure).

**If `gifExportWebp === true` → WebP (single pass, `buildWebpOutputArgs`):**
```
-i <input>
-vf "fps=<fps>[,scale=-2:<h>:flags=lanczos]"
-loop 0
-c:v libwebp
-quality 80
-compression_level 6
<output.webp>
```

**Output ext:** `webp` if toggle on, else `gif`.

---

### 3. `resize_video` — Change resolution
**Category:** video
**Description:** Fits video into a preset resolution/orientation frame with letterbox padding, re-encodes to selected container.

**Params** (`fields: ["resolutionPreset", "resizeVideoFormat"]`):
- `resolutionPreset`: one of `4k_landscape (3840x2160), fullhd_landscape (1920x1080), hd_landscape (1280x720), 4k_portrait (2160x3840), fullhd_portrait (1080x1920), hd_portrait (720x1280)`. Default `fullhd_landscape`. Legacy `"WxH"` string values are normalized back to a preset key via reverse lookup.
- `resizeVideoFormat`: one of `mp4,mov,mkv,avi,webm`. Default `mp4`.

**Letterbox/pad filter** (`buildFitIntoCanvasFilter` — the "fit-into-canvas" recipe):
```
scale=<W>:<H>:force_original_aspect_ratio=decrease,pad=<W>:<H>:(ow-iw)/2:(oh-ih)/2:black
```
Scales to fit inside target box preserving aspect ratio, then centers on a black canvas of exact target size — classic letterboxing, done in one filter chain.

**Format → codec config** (`getResizeVideoFormatConfig`):
| format | videoCodec | audioCodec | audioBitrate | h264(pix_fmt) |
|---|---|---|---|---|
| mp4/mov/mkv | libx264 | aac | 192k | yes |
| avi | mpeg4 | libmp3lame | 192k | no |
| webm | libvpx-vp9 | libopus | 160k | no |

**Bitrate table** (`getResizeVideoBitrate`, by preset tier × format):
| | avi | webm | mp4/mov/mkv |
|---|---|---|---|
| 4k | 45M | 20M | 35M |
| fullhd | 12M | 6M | 8M |
| hd | 7M | 3M | 5M |

**Exact command:**
```
-i <input>
-vf "scale=<W>:<H>:force_original_aspect_ratio=decrease,pad=<W>:<H>:(ow-iw)/2:(oh-ih)/2:black"
-c:v <videoCodec>
-b:v <bitrateFromTable>
[-pix_fmt yuv420p]           # only if h264 flag set (mp4/mov/mkv)
-c:a <audioCodec>
-b:a <audioBitrate>
<output>
```

**Output ext:** matches `resizeVideoFormat`.

---

### 4. `trim_video` — Trim video
**Category:** video
**Description:** Cuts a segment starting at `startTime` for `duration`.

**Params** (`fields: ["startTime", "duration"]`):
- `startTime`: text, `HH:MM:SS[.fff]`, default `00:00:00`
- `duration`: text, `HH:MM:SS[.fff]`, default `00:00:00`

**Validation** (`validateTrimVideoParams`, enforced in main.cjs before job runs, using ffprobe-derived `durationSeconds` per file): start must be `>=0`, duration must be `>0`, `start < fileLength`, `duration <= fileLength`, `start+duration <= fileLength`. Runs per-file for batch jobs and aggregates per-file error messages.

**Exact command (input-seeking, not output-seeking):**
```
-ss <startTime> -t <duration> -i <input> -c:v libx264 -c:a aac <output>
```
Note: `-ss` placed **before** `-i` (fast/keyframe-ish seek), but since it re-encodes anyway (`libx264`/`aac`, not `copy`), frame-accurate seeking is used implicitly — no accuracy tradeoff from placement, but it does forgo the potential speed benefit of accurate `-ss` after `-i` combined with `copy`.

**Output ext:** hardcoded `mp4`.

---

### 5. `extract_audio` — Extract or remove audio
**Category:** video
**Description:** Pulls one or more audio streams out of a video (optionally transcoding), or produces a silent copy of the video.

**Params** (`fields: ["extractAudioAction", "extractAudioFormat", "extractAudioBitrate"]`):
- `extractAudioAction`: `extract` (default) | `remove`
- `extractAudioFormat`: `original` (default, stream-copy) | `mp3` | `aac` | `opus` | `ogg`
- `extractAudioBitrate`: select per-format from `EXTRACT_AUDIO_BITRATE_OPTIONS[format]`, OR literal `"source"`, OR `"\d+k"` string.

**Format → output config** (`getExtractAudioFormatConfig`):
| format | ext | codecArgs |
|---|---|---|
| mp3 | mp3 | `-c:a libmp3lame` |
| aac | m4a | `-c:a aac` |
| opus| opus| `-c:a libopus` |
| ogg | ogg | `-c:a libvorbis` |

**Case `action === "remove"`:**
```
-i <input> -c:v copy -an <output.mp4>
```

**Case `action === "extract", format === "original"`** (stream-copy, one job per audio stream index):
```
-i <input> -map 0:a:<streamIndex> -c:a copy <output>
```
Output extension derived from source codec name via `getOriginalAudioExtension` (`aac→m4a, mp3→mp3, opus→opus, vorbis→ogg, flac→flac, alac→m4a`). Unsupported source codec → job marked `error`.

**Case `action === "extract", format !== "original"`** (transcode):
```
-i <input> -map 0:a:<streamIndex> <codecArgs...> -b:a <bitrate> <output>
```
Bitrate resolution: if `extractAudioBitrate === "source"`, uses the **estimated source bitrate from ffprobe packet analysis** (see Global Patterns) rounded to kbps, else falls back to `128k`; otherwise uses the literal selected bitrate.

**Multi-stream + silence handling (orchestration in `runExtractAudioNativeJob`, main.cjs):**
1. `probeAudioStreamsForFile` (ffprobe) enumerates all audio streams.
2. If zero audio streams → single result: `status: "skipped"`.
3. Per stream: run `ffmpeg -i <input> -map 0:a:<i> -vn -af volumedetect -f null NUL` (see "NUL" bug flagged below) and parse `max_volume: X dB` from stderr. If `max_volume <= -60dB`, treat stream as silent and skip it (`status: "skipped"`).
4. For each remaining "active" stream, run the extract command above. Output filename: `<base>-audio.<ext>` if single active stream, else `<base>-audio-<n>.<ext>` for n=1..N.
5. Embedded artwork (attached_pic video stream, e.g. album art on an audio file) is preserved via `applyEmbeddedArtworkPreservation` when the output extension is mp3/flac/m4a (see Global Patterns).

**Output ext:** `mp4` if remove; `audio` (generic, resolved later) if original; else format's ext.

---

### 6. `rotate_video` — Rotate/mirror video
**Category:** video
**Description:** Rotates 90°/180° or mirrors, always re-encodes.

**Params** (`fields: ["rotation"]`, operation-specific override adds a 5th option):
- `rotation`: `none | cw | ccw | 180 | mirror`. Default `none`.

**Filter map** (`buildRotateArgs`/`getRotationFilter`):
| rotation | filter |
|---|---|
| none | (no `-vf`) |
| cw | `transpose=1` |
| ccw | `transpose=2` |
| 180 | `hflip,vflip` |
| mirror | not in the filter map (falls through to `null` → **no visual filter applied**, likely a bug — see Bugs section) |

**Exact command (re-encode always, `reencodeVideo=true` hardcoded from OPERATIONS registry call):**
```
-i <input> [-vf <filter>] -c:v libx264 -c:a aac <output>
```

**Output ext:** hardcoded `mp4`.

---

### 7. `grayscale_video` — Video color and style
**Category:** video
**Description:** Applies a named color-grade preset and/or manual brightness/contrast/saturation trim. Default (`styleMode: none`, all adjust=0) produces no visual change but still re-encodes.

**Params** (`fields: ["styleMode", "styleIntensity", "brightnessAdjust", "contrastAdjust", "saturationAdjust"]`):
- `styleMode`: one of `none, grayscale, grayscale_contrast, sepia, vintage, stronger_colors, weaker_colors, brighter, darker, higher_contrast, warmer, cooler, negative`. Default `none`.
- `styleIntensity`: range 0–100, default 100 (scales preset strength; 0 disables preset filters).
- `brightnessAdjust`: range -50..50, default 0
- `contrastAdjust`: range -50..100, default 0
- `saturationAdjust`: range -100..100, default 0

**Preset filters** (`buildColorStylePresetFilters`, `intensityFactor = styleIntensity/100`):
| mode | filter(s) |
|---|---|
| grayscale | `hue=s=<1-intensityFactor>` |
| grayscale_contrast | `hue=s=<1-intensityFactor>`, `eq=contrast=<1+0.25*intensityFactor>` |
| sepia | `colorchannelmixer=<12 coeffs>` — interpolates identity matrix → sepia matrix `[0.393,0.769,0.189,0, 0.349,0.686,0.168,0, 0.272,0.534,0.131,0]` by `intensityFactor` |
| vintage | `eq=contrast=<1+0.15f>:saturation=<1-0.25f>:brightness=<0.02f>`, `colorbalance=rs=<0.08f>:gs=<0.03f>:bs=<-0.05f>` |
| stronger_colors | `eq=saturation=<1+0.35f>` |
| weaker_colors | `eq=saturation=<max(0,1-0.35f)>` |
| brighter | `eq=brightness=<0.06f>` |
| darker | `eq=brightness=<-0.06f>` |
| higher_contrast | `eq=contrast=<1+0.25f>` |
| warmer | `colorbalance=rs=<0.08f>:gs=<0.03f>:bs=<-0.04f>` |
| cooler | `colorbalance=rs=<-0.05f>:gs=0:bs=<0.08f>` |
| negative | `negate` |
| none | (no filters) |

**Manual adjust filter** (`buildColorStyleAdvancedFilter`, appended after preset filters, only emitted if any adjust != 0):
```
eq=brightness=<brightnessAdjust/250>:contrast=<1+contrastAdjust/100>:saturation=<max(0,1+saturationAdjust/100)>
```

**Exact command:**
```
-i <input>
-map 0:v:0 -map 0:a?
[-vf "<presetFilters,advancedFilter joined by comma>"]     # omitted entirely if no filters
-b:v <sourceBitrate>k -maxrate <sourceBitrate>k -bufsize <max(2x,1x)sourceBitrate>k    # IF source video bitrate known from ffprobe
    (else) -crf 18 -preset medium
-c:v libx264
-fps_mode passthrough
-movflags +faststart
-map_metadata 0
-map_chapters 0
-c:a copy
<output>
```
Audio is always stream-copied (`-c:a copy`), never re-encoded — only the video filter chain changes.

**Output ext:** hardcoded `mp4`. Output suffix: `-color-style`.

---

### 8. `add_watermark` — Add watermark image
**Category:** video
**Description:** Overlays a scaled, alpha-blended image on the video at a fixed corner or percentage-based manual position.

**Params** (`fields: ["coverImage", "watermarkPosition", "watermarkSize", "watermarkOpacity", "watermarkX", "watermarkY"]`), `extraInputs.coverImage` required (file path) — throws if missing:
- `coverImage`: file (image), required
- `watermarkPosition`: `top-left | top-right | bottom-left | bottom-right | manual`. Default `top-right`.
- `watermarkSize`: 1–100 (%), default 18 — percent of **source video width**
- `watermarkOpacity`: 0–100 (%), default 90
- `watermarkX`, `watermarkY`: 0–100 (%), only used when position is `manual`

**Position math** (`getWatermarkOverlayPosition` for corners, 20px margin hardcoded):
| position | x,y expr |
|---|---|
| top-left | `20 : 20` |
| top-right | `main_w-overlay_w-20 : 20` |
| bottom-left | `20 : main_h-overlay_h-20` |
| bottom-right | `main_w-overlay_w-20 : main_h-overlay_h-20` |
| manual | `main_w*<X>/100 : main_h*<Y>/100` |

**Watermark scale/alpha filter** (`buildWatermarkOverlayFilter`; watermark width computed in JS from `inputInfo.width * sizePercent/100`, falls back to ffmpeg's `iw` keyword if source width unknown):
```
[1:v]scale=<watermarkWidth>:-1,format=rgba,colorchannelmixer=aa=<opacity0to1>[wm];
[0:v][wm]overlay=x=<xExpr>:y=<yExpr>:format=auto[v]
```

**Exact command:**
```
-i <input>
-i <coverImage>
-filter_complex "<above two-stage filter>"
-map [v] -map 0:a?
-c:v libx264 -b:v <srcBitrate>k -maxrate <srcBitrate>k -bufsize <2x>k   # or -crf 18 -preset medium if bitrate unknown
-fps_mode passthrough
-pix_fmt yuv420p
-movflags +faststart
-map_metadata 0
-map_chapters 0
-c:a copy
-shortest
<output>
```
`-shortest` guards against the still-image watermark input extending output duration.

**Output ext:** hardcoded `mp4`.

---

### 9. `burn_subtitles` — Burn-in subtitles
**Category:** video
**Description:** Hardcodes SRT/VTT/ASS subtitle text into the video frame (irreversible, unlike soft-subs).

**Params** (`fields: ["subtitles"]`), `extraInputs.subtitles` = path to `.srt/.vtt/.ass` file, required.

**Path escaping for the `subtitles` filter** (`escapeSubtitlePath` — required because ffmpeg's filtergraph parser treats `:`, `'`, `[`, `]` as syntax):
```js
path.replace(/\\/g, "/").replace(/:/g, "\\:").replace(/'/g, "\\'").replace(/\[/g, "\\[").replace(/\]/g, "\\]")
```

**Exact command:**
```
-i <input> -vf "subtitles='<escapedPath>'" -c:v libx264 -c:a aac <output>
```

**Output ext:** hardcoded `mp4`.

---

### 10. `video_speed` — Change video speed
**Category:** video
**Description:** Speeds up/slows down video and (if present) audio together, keeping them in sync.

**Params** (`fields: ["speed"]`, override: label "Speed change", range -100..100, step 1, default "0", `showScale:false`):
- `speed`: integer percent, -100..100. 0 = no change. Negative = slower, positive = faster.

**Speed factor math** (`getVideoSpeedFactors`):
```
if percent == 0: videoFactor=1, audioFactor=1
if percent > 0:  speed = 1 + percent/100;  videoFactor = 1/speed; audioFactor = speed
if percent < 0:  speed = 1 + abs(percent)/100; videoFactor = speed; audioFactor = 1/speed
```

**Video timing filter:** `setpts=<videoFactor>*PTS`

**Audio tempo filter — chained atempo for out-of-range values** (`buildAudioTempoFilter`, the clever recipe: ffmpeg's `atempo` only accepts 0.5–2.0 per instance, so values outside that are achieved by chaining multiple `atempo=2` stages):
```js
remaining = audioFactor (clamped 0.5..100)
while remaining > 2: emit "atempo=2"; remaining /= 2
emit `atempo=${remaining clamped 0.5..2}`
// joined with commas
```
Note: only handles the >2 case by halving repeatedly; does NOT symmetrically chain for remaining < 0.5 (audioFactor is already clamped to a 0.5 floor by `decimalInRange`, so sub-0.5 chaining is never needed given speed's own -100..100 range, but this is a latent gap if audioFactor could go lower).

**Audio codec selection** (`getSpeedAudioCodec`, tries to match source codec so re-encoding this operation doesn't unnecessarily change format):
```
mp3→libmp3lame, opus→libopus, vorbis→libvorbis, default→aac
```

**Exact command:**
```
-i <input>
-map 0:v:0 -map 0:a?
-vf "setpts=<videoFactor>*PTS"
[if hasAudio:]
  -af "<atempo chain>" -c:a <codecFromSource> [-b:a <sourceAudioBitrate>k]
[else:]
  -an
[if sourceVideoBitrate known:]
  -c:v libx264 -b:v <bitrate>k -maxrate <bitrate>k -bufsize <max(2x,1x)>k
[else:]
  -c:v libx264 -crf 18 -preset medium
-fps_mode passthrough
-movflags +faststart
-map_metadata 0
-map_chapters 0
<output>
```

**Output ext:** hardcoded `mp4`.

---

## AUDIO OPERATIONS

### 11. `convert_audio` — Audio format and settings
**Category:** audio
**Description:** Transcodes to a target audio format/container with optional loudness normalization and manual volume boost.

**Params** (`fields: ["audioFormat", "normalizeLoudness", "volumeBoostPercent", "outputSampleRate", "bitrate"]`):
- `audioFormat`: `mp3|wav|flac|ogg|opus|aac|m4a|aiff`. Default `mp3`.
- `normalizeLoudness`: boolean, default false
- `volumeBoostPercent`: 0–200, default 0
- `outputSampleRate`: constrained per-format/encoder (see `AUDIO_SAMPLE_RATES_BY_FORMAT`/`_BY_ENCODER` — e.g. MP3 max 48kHz no 88.2/96kHz; Opus only 48/24/16/12/8kHz; Vorbis/OGG max 48kHz). Validated via `validateOutputSampleRateForAudioFormat`; throws if invalid.
- `bitrate`: `96k|128k|160k|192k|256k|320k`, default `320k` — only applied if format supports bitrate.

**Format → codec config** (`getAudioFormatConfig`):
| format | ext | codecArgs | supportsBitrate |
|---|---|---|---|
| mp3 | mp3 | `-c:a libmp3lame` | yes |
| wav | wav | `-c:a pcm_s16le` | no |
| flac| flac| `-c:a flac` | no |
| ogg | ogg | `-c:a libvorbis` | yes |
| opus| opus| `-c:a libopus` | yes |
| aac | aac | `-c:a aac` | yes |
| m4a | m4a | `-c:a aac` | yes |
| aiff| aiff| `-c:a pcm_s16be`| no |

**Filters** (`buildAudioConversionArgs`):
- If `normalizeLoudness`: `loudnorm=I=-16:TP=-1.5:LRA=11` (EBU R128 one-pass loudnorm, fixed target -16 LUFS / -1.5dB true peak / 11 LU range)
- If `volumeBoostPercent > 0`: `volume=<1 + boost/100>` (e.g. +50% → `volume=1.5`)
- Filters joined by comma into single `-af`.

**Exact command:**
```
-i <input>
-map 0:a:0?
[-af "<loudnorm?>,<volume?>"]
-ar <safeSampleRate>
<codecArgs...>
[-b:a <bitrate>]      # only if format supportsBitrate
<output>
```

**Output ext:** matches `audioFormat`.

---

### 12. `trim_audio` — Trim audio
**Category:** audio
**Description:** Cuts a segment from an audio file (stream-copy start/duration cut).

**Params** (`fields: ["startTime", "duration"]`): same as trim_video (`HH:MM:SS`).

**Exact command:**
```
-ss <startTime> -t <duration> -i <input> -map 0:a:0? <output>
```
Note: no explicit `-c:a copy` or re-encode codec specified — ffmpeg will pick a default encoder matching the output container/extension (mp3 by default here since output ext is hardcoded mp3), i.e. this always re-encodes to mp3 regardless of source format. No format selection param exposed to the user for this op.

**Output ext:** hardcoded `mp3`.

---

### 13. `audio_settings` — Channel/reverse/fade
**Category:** audio
**Description:** Combines channel-layout change, full-reverse, and fade in/out into a single audio filter pass.

**Params** (`fields: ["audioChannelMode", "reverseAudioEnabled", "fadeInDuration", "fadeOutDuration"]`):
- `audioChannelMode`: `keep|mono|stereo`, default `keep`
- `reverseAudioEnabled`: boolean, default false
- `fadeInDuration`: 0–30s, step 0.1, default 0
- `fadeOutDuration`: 0–30s, step 0.1, default 0

**Validation** (`validateAudioSettingsParams`): throws if no actual change requested (channelMode=keep AND !reverse AND fadeIn<=0 AND fadeOut<=0). If fadeOut>0, requires known `inputInfo.durationSeconds` (from ffprobe) and that `fadeOutDuration < durationSeconds`, else throws.

**Filter assembly** (`buildAudioSettingsArgs`, order matters — channel conversion first, then reverse, then fades):
```
[aformat=channel_layouts=mono]      # if mono
[aformat=channel_layouts=stereo]    # if stereo
[areverse]                          # if reverseAudioEnabled
[afade=t=in:st=0:d=<fadeInDuration>]
[afade=t=out:st=<max(0,duration-fadeOutDuration)>:d=<fadeOutDuration>]
```

**Exact command:**
```
-i <input> -map 0:a:0? [-af "<filters joined by comma>"] <output>
```
Same as trim_audio: no explicit codec — defaults to mp3 encoder via output extension.

**Output ext:** hardcoded `mp3`.

**Non-obvious ordering gotcha:** `reverseAudioEnabled` + fade combination is order-sensitive. Because `areverse` runs before the fades in the filter chain, `afade=t=out` (computed against the *original* duration/timestamp) is applied to the *already-reversed* stream — meaning a "fade out" after reversing effectively fades in at the *original* start of the track (now the end after playback), not a true "fade out of the reversed audio." This is subtle but matches the intent (fade out at the tail of final output) since PTS timing is preserved through `areverse`.

---

### 14. `add_metadata` — Add/remove ID3-style metadata + cover art
**Category:** audio
**Description:** The most complex non-encoding operation — writes/clears text metadata tags and embeds/strips cover art, choosing a strategy based on what the target container actually supports.

**Params** (`fields: ["coverImage", "metadataArtist", "metadataTitle", "metadataReleaseDate", "metadataGenre", "removeCover", "removeAllMetadata"]`), plus batch-only `metadataPerFileOptions` (map of `inputPath → {outputBaseName, metadataArtist, metadataTitle, metadataReleaseDate, metadataGenre, coverMode: inherit|keep|replace|remove, coverPath}`):
- `coverImage`: file (image), optional — global default cover
- `metadataArtist/Title/Genre`: free text
- `metadataReleaseDate`: digits only (non-digit chars stripped on normalize)
- `removeCover`: boolean
- `removeAllMetadata`: boolean — mutually exclusive with any other metadata/cover change (`validateAddMetadataParams` throws `"Remove all cannot be combined with new metadata or cover"` if both set)

**Per-format support matrix** (`getAudioMetadataSupportProfile`, keyed by **output file extension**, not codec):
| ext | text tags | can add cover | can remove cover |
|---|---|---|---|
| mp3 | yes | yes | yes |
| flac | yes | no | yes |
| m4a | yes | no | yes |
| ogg | yes | no | no |
| opus| yes | no | no |
| wav | no | no | no |
| aiff/aif | no | no | no |
| aac | no | no | no |

**Cover resolution priority** (`resolveAudioMetadataCoverPlanForFile`, per file):
1. `removeAllMetadata` → mode `remove`
2. per-file override `coverMode === "replace"` with a `coverPath` → mode `replace`
3. per-file override `coverMode === "remove"` → mode `remove`
4. global `removeCover` checkbox → mode `remove`
5. global `coverImage` set → mode `replace`
6. else → mode `keep` (no cover change)

**Case A — `removeAllMetadata`:**
```
-i <input> -map 0:a? -map_metadata -1 -map_chapters -1 -c:a copy <output>
```

**Case B — no actual change requested** (`hasAnyRequestedChange === false`): file is plain-copied via Node `fs.copyFile`, no ffmpeg invoked at all.

**Case C — requested change unsupported by container** (e.g. asking for a cover on OGG): plain-copied via `fs.copyFile`, result flagged `status: "warning"` with a message listing which parts were skipped.

**Case D — normal write** (`buildAddMetadataArgs`):
```
[if canWriteCover:]
  -i <input> -i <coverPath> -map 0:a:0? -map 1:v:0? -c:a copy -c:v mjpeg -id3v2_version 3 -disposition:v:0 attached_pic
[elif shouldRemoveCover:]
  -i <input> -map 0:a? -c:a copy
[else:]
  -i <input> -map 0 -c copy
<-metadata key=value>...   # one pair per set field (artist/title/date/genre), only if profile.text
<output>
```
Text metadata keys map directly to ffmpeg's `-metadata` tag names: `artist, title, date, genre`.

**Output ext:** derived from **input** extension via `getAudioMetadataOutputExtension` (with `m4b→m4a` alias), i.e. add_metadata never changes container/format — it's an in-place-format tag operation.

---

### 15. `audio_speed` — Change audio speed
**Category:** audio
**Description:** Simple single-stage tempo change (no chaining — range is clamped to ffmpeg's native `atempo` limits).

**Params** (`fields: ["speed"]`, override `showScale:false`): range -50..100 in the help text, but builder clamps via `decimalInRange(speed, 1.0, 0.5, 2, 3)` — i.e. actual usable range is **0.5x–2.0x**, not a percent-based -50..100 like the UI implies; the raw `speed` field's own default/min/max from `FIELD_DEFS` (`0.5–100`) is used directly as the atempo multiplier, clamped to `[0.5, 2]`.

**Exact command:**
```
-i <input> -map 0:a:0? -filter:a "atempo=<speed clamped 0.5-2>" <output>
```

**Output ext:** hardcoded `mp3`.

---

## IMAGE OPERATIONS

### 16. `resize_image` — Size and format
**Category:** image
**Description:** Resizes/reformats a still image, optionally with a user-confirmed crop when the target aspect ratio doesn't match the source.

**Params** (`fields: ["imageAspectRatio", "imageResizePreset", "imageCustomWidth", "imageCustomHeight", "imageOutputFormat"]`):
- `imageAspectRatio`: one of 20 ratio strings (`1:1, 16:9, 9:16, 4:3, 3:4, 3:2, 2:3, 5:4, 4:5, 21:9, 9:21, 2:1, 1:2, 18:9, 9:18, 19.5:9, 9:19.5, 20:9, 9:20, 5:7, 7:5`). Default `1:1`.
- `imageResizePreset`: `"manual"` or a preset `WxH` string from a ratio-specific table (e.g. for 1:1: `1080x1080, 1440x1440, 2160x2160, 4096x4096`), or `WxH` regex-matched custom string.
- `imageCustomWidth/Height`: 1–8192px, used when preset is `manual`.
- `imageOutputFormat`: `jpg|png|webp|bmp|tiff`. Default `png`.

**Format → codec config** (`getImageOutputFormatConfig`):
| format | ext | codecArgs |
|---|---|---|
| jpg | jpg | `-c:v mjpeg -q:v 2` |
| png | png | `-c:v png` |
| webp| webp| `-c:v libwebp -lossless 0 -quality 92` |
| bmp | bmp | `-c:v bmp` |
| tiff| tiff| `-c:v tiff` |

**Scale/crop logic** (`buildImageConversionArgs`):
- Upscale detection: if target W or H exceeds source W/H, append `:flags=lanczos` to the scale filter (uses higher-quality resampling only when enlarging).
- If `inputInfo.cropData` present (user confirmed a manual crop client-side): prepend `crop=<w>:<h>:<x>:<y>,` before the scale filter. Crop values individually clamped against source bounds.
- Else, if source aspect ratio and target aspect ratio differ by more than 1% (`Math.abs(sourceRatio - targetRatio) > 0.01`), **throws** `"Selected size requires confirming an image crop"` — forces the caller (renderer UI) to prompt for a crop before this ffmpeg call can succeed.

**Exact command:**
```
-i <input>
-vf "[crop=<w>:<h>:<x>:<y>,]scale=<targetW>:<targetH>[:flags=lanczos]"
<codecArgs...>
-frames:v 1
[-update 1]     # only for png output
<output>
```

**Output ext:** matches `imageOutputFormat`.

---

### 17. `rotate_image` — Rotate and flip
**Category:** image
**Description:** Rotates 90°/180° and/or flips an image.

**Params** (`fields: ["rotation", "flipHorizontal", "flipVertical"]`):
- `rotation`: `none|cw|ccw|180` (note: **no `mirror` option here** — that's video-only). Default `none`.
- `flipHorizontal`, `flipVertical`: booleans, default false.

**Filter chain** (order: rotation, then hflip, then vflip; filters with no effect are omitted, comma-joined):
```
[transpose=1]     # cw
[transpose=2]     # ccw
[hflip,vflip]     # 180 (both flips, not a transpose — same visual result)
[hflip]           # if flipHorizontal
[vflip]           # if flipVertical
```

**Exact command:**
```
-i <input> [-vf "<filters>"] -frames:v 1 -update 1 <output>
```

**Output ext:** hardcoded `png`.

---

### 18. `grayscale_image` — Black and white
**Category:** image
**Description:** Converts to black-and-white via a partial-desaturation color matrix (not a hard `format=gray`), blended by an "intensity" percentage, then applies a mode-specific contrast/brightness preset plus manual trim.

**Params** (`fields: ["blackWhiteMode", "blackWhiteIntensity", "blackWhiteContrast", "blackWhiteBrightness"]`):
- `blackWhiteMode`: `classic|bright|contrast|dark|soft`. Default `classic`.
- `blackWhiteIntensity`: 0–100, default 100 — blends between original color and full grayscale.
- `blackWhiteContrast`: -100..100 (operation override changes FIELD_DEFS default range), default 0
- `blackWhiteBrightness`: -100..100 (op override), default 0

**Grayscale blend filter** (`buildBlackWhiteMixerFilter`) — this is a **luma-weighted `colorchannelmixer`** that linearly interpolates each output channel between "keep original channel" and "full ITU-R BT.601 luma" based on `intensity = blackWhiteIntensity/100`:
```
keep = 1 - intensity
luma = {r: 0.299*intensity, g: 0.587*intensity, b: 0.114*intensity}
colorchannelmixer=<keep+luma.r>:<luma.g>:<luma.b>:0:<luma.r>:<keep+luma.g>:<luma.b>:0:<luma.r>:<luma.g>:<keep+luma.b>:0
```
At intensity=100 this reduces to standard BT.601 grayscale on all 3 channels; at 0 it's the identity matrix (no change) — a genuinely clever "partial B&W" trick using a single mixer matrix instead of blending two rendered frames.

**Preset contrast/brightness table** (`buildBlackWhiteImageFilter`):
| mode | contrast | brightness |
|---|---|---|
| classic | 1 | 0 |
| bright | 0.92 | 0.035 |
| contrast | 1.18 | 0 |
| dark | 1.08 | -0.045 |
| soft | 0.85 | 0.015 |

Manual sliders are folded in as multipliers/offsets: `userContrast = (100 + (contrastPercent/100)*50)/100`, `userBrightness = ((brightnessPercent/100)*30)/250`, then `finalContrast = clamp(preset.contrast * userContrast, 0.5, 1.5)`, `finalBrightness = clamp(preset.brightness + userBrightness, -0.3, 0.3)`. An `eq=contrast=...:brightness=...` filter stage is appended only if either differs from identity (1, 0).

**Exact command:**
```
-i <input> -vf "<colorchannelmixer>[,eq=contrast=<c>:brightness=<b>]" -frames:v 1 -update 1 <output>
```

**Output ext:** hardcoded `png`.

---

### 19. `sharpness_image` — Sharpness/blur
**Category:** image
**Description:** Applies box blur and/or unsharp-mask sharpening, percent-scaled to ffmpeg filter parameter ranges.

**Params** (`fields: ["sharpenAmount", "blurStrength"]`, op overrides change FIELD_DEFS defaults/ranges to 0–100 each, `showScale:false`):
- `sharpenAmount`: 0–100 (%), default 0 → scaled to `0..20` for the `unsharp` filter's amount parameter
- `blurStrength`: 0–100 (%), default 0 → scaled to `0..40` for `boxblur`'s radius parameter

**Filters** (`buildImageSharpnessArgs`, blur first, then sharpen — both optional, comma-joined):
```
[boxblur=<blurStrength*0.4>]                          # 0-100% mapped to 0-40
[unsharp=5:5:<sharpenAmount*0.2>:5:5:0.0]              # 0-100% mapped to 0-20; luma 5x5 matrix, chroma matrix params fixed at 5:5:0.0 (chroma amount 0 = luma-only sharpening)
```

**Exact command:**
```
-i <input> [-vf "<boxblur?>,<unsharp?>"] -frames:v 1 -update 1 <output>
```

**Output ext:** hardcoded `png`.

---

## GLOBAL PATTERNS

### Binary path resolution
`resolveFfmpegPath` / `resolveFfprobePath` (main.cjs):
- Dev mode (`!app.isPackaged`): use the path exported directly by npm packages `ffmpeg-static` and `ffmpeg-ffprobe-static`.
- Packaged mode: try, in order, `process.resourcesPath/bin/<binary>.exe`, then `process.resourcesPath/app.asar.unpacked/node_modules/<pkg>/<basename-of-package-path>`; first existing file wins, else falls back to the first candidate anyway (no explicit error if neither exists — surfaces later as a spawn ENOENT).
- Both binaries are `.exe` — **this app is effectively Windows-only in its packaged binary resolution** (see Bugs).

### FFprobe-based media info detection
Two info-gathering paths:
1. `runFfprobeJson` — generic JSON probe: `ffprobe -v error -show_entries stream=index,codec_type,codec_name,profile,pix_fmt,bit_rate,duration,avg_frame_rate,r_frame_rate,width,height,sample_rate,channels,channel_layout:stream_disposition=attached_pic:stream_tags=language,DURATION:format=bit_rate,duration -of json <input>`.
2. `probeVideoMetadataForFile` — wraps (1), extracts first video+audio stream, computes duration/bitrate/frame-rate with graceful multi-source fallback (`pickFirstFiniteNumber` tries stream-level, then tag-level `DURATION`, then container `format.duration`).
3. `probeAudioStreamsForFile` — enumerates ALL audio streams (not just first) with **per-stream bitrate estimation from raw packet sizes** (clever recipe — see below).
4. `probeImageFile` — uses Electron's `nativeImage` (not ffprobe) for image dimensions, with **manual JPEG/TIFF EXIF orientation parsing done by hand** (byte-level IFD/TIFF walk in pure JS, not via ffmpeg `-noautorotate`/`-autorotate` or an exif library) to detect orientation tags 5–8 and swap W/H accordingly.
5. Fallback path for images/videos that fail structured probing: spawn bare `ffmpeg -i <input>` and regex-parse stderr (`Video: ... WxH`, `Duration: HH:MM:SS`, bitrate `kb/s`, presence of `Audio:` stream) — a last-resort text-scrape probe.

### Output path / overwrite handling
- Global arg `-y` (in `FFMPEG_GLOBAL_ARGS`) means ffmpeg silently overwrites without prompting — safety instead enforced at the app layer.
- `findFirstOutputConflict` (main.cjs) pre-computes every output filename for the whole batch (case-insensitively, via `.toLowerCase()`) and returns the **first** conflict found, either `reason: "batch"` (two inputs would produce the same output filename) or `reason: "existing"` (a file already exists on disk at that path) — checked via `fs.access` before any ffmpeg run. This runs as a separate IPC round-trip (`ffmpeg:check-output-conflicts`) before the actual batch job.
- Output filenames built by `buildOutputFileName`: `<sanitizedBaseName><_suffixIfAny>.<ext>`; base name sanitization strips `<>:"/\|?*` and trailing dots (Windows-reserved-char stripping, again suggesting a Windows-first design).

### Progress parsing
- `parseDurationFromStderr`: regex `/Duration:\s*(\d+:\d{2}:\d{2}(?:\.\d+)?)/` against accumulated stderr, run once until first successful match.
- `parseProgressTime`: regex `/time=(\d+:\d{2}:\d{2}(?:\.\d+)?)/g`, takes the **last** match in each stderr chunk (ffmpeg's default progress line format, not `-progress pipe:` machine-readable output).
- Both rely on ffmpeg's default human-readable stderr status line — **not** `-progress <url>` or `-stats_period`, which would be far more robust (see Bugs).
- Progress payload sent to renderer via `ffmpeg:progress` IPC event includes `completedFiles/totalFiles/currentFile/inputDurationSeconds/progressSeconds` for a queue-relative + per-file-relative progress bar.

### Error handling
- On non-zero exit, the last 8 lines of the (locale/version-dependent) stderr tail are surfaced as the error message (`stderr.trim().split(/\r?\n/).slice(-8).join("\n")`).
- `MAX_STDERR_BUFFER_CHARS = 64 * 1024` — stderr is truncated to the **last** 64KB as it streams in (`trimStderrBuffer` keeps `.slice(-MAX)`), to bound memory on long-running jobs; since only the tail is ever surfaced anyway, this doesn't lose the diagnostically useful part, but could truncate an early fatal messages if ffmpeg prints megabytes of warnings before failing near the start (rare in practice).

### Metadata preservation / determinism policy
`applyMetadataPreservationPolicy` (applied to almost every command, via `runSingleNativeJob`/`runExtractAudioNativeJob`/`runAddMetadataNativeJob`, unless the op already set its own `-map_metadata`) injects, right before the output path arg:
```
-map_metadata 0        # if not already present, and preserveSourceMetadata!=false
-map_chapters 0         # ditto
-fflags +bitexact
-flags:a +bitexact
-flags:v +bitexact
-metadata encoder=
-metadata:s:a:0 encoder=
-metadata:s:v:0 encoder=
```
Purpose: (1) copy source metadata/chapters forward by default across nearly every transform, (2) strip ffmpeg's own "Lavf/Lavc" encoder signature tags for reproducible/anonymized output, (3) `bitexact` flags disable non-deterministic encoder behavior (e.g. timestamp/UUID embedding) for byte-reproducible builds. This is a "determinism + privacy scrub" pass layered transparently onto every operation.

### Embedded artwork preservation
`applyEmbeddedArtworkPreservation` + `getArtworkPreservationConfig` — only engages when the output extension is `.mp3` / `.flac` / `.m4a` AND the command doesn't already have an explicit `-c:v` (i.e. only for ops that didn't already handle cover art themselves, like `convert_audio`, `trim_audio`, `audio_settings`, `audio_speed` when the source file happens to carry embedded art). Before running, main.cjs probes for an `attached_pic` disposition video stream via `probeAttachedPictureStream`. If found, injects before the output path:
```
mp3:        -map 0:<artworkStreamIndex>? -c:v mjpeg -disposition:v:0 attached_pic -id3v2_version 3
flac/m4a:   -map 0:<artworkStreamIndex>? -c:v copy  -disposition:v:0 attached_pic
```
This lets "unrelated" audio operations (trim, speed, settings, format-convert) transparently keep a track's cover image without the operation's own arg builder needing to know about art at all.

### CPU limiting
`buildFfmpegRuntimeArgs` (only applied if `cpuLimitPercent !== 100`, one of `10..100` step 10 in `CPU_LIMIT_OPTIONS`):
```
threadCount = cpuCount==1 ? 1 : clamp(floor(cpuCount * percent/100), 1, cpuCount-1)
prepend: -threads <threadCount>
```
Additionally, after spawn, `os.setPriority(pid, ...)` is called (wrapped in try/catch — ignored if OS rejects):
- `percent <= 30` → `PRIORITY_LOW`
- `percent <= 70` → `PRIORITY_BELOW_NORMAL`
- else → `PRIORITY_NORMAL`
This is OS-level process priority + ffmpeg's own thread-count throttle combined, a reasonably complete "be a good citizen on the user's machine" mechanism — not just `-threads`.

---

## HIGH-VALUE / CLEVEREST RECIPES WORTH STEALING

1. **Two-pass GIF palette generation** (`buildGifPaletteArgs` + `buildGifOutputArgs`): `palettegen=stats_mode=diff` → `paletteuse=dither=bayer:bayer_scale=5:diff_mode=rectangle`. Produces materially better GIF quality/size than a naive single-pass GIF encode. Directly reusable.

2. **Letterbox/fit-into-canvas one-liner**: `scale=W:H:force_original_aspect_ratio=decrease,pad=W:H:(ow-iw)/2:(oh-ih)/2:black`. Clean, reusable "fit inside frame, center, pad" pattern for any resize-with-orientation-change need.

3. **atempo chaining for out-of-range speed changes** (`buildAudioTempoFilter`): halves the factor repeatedly emitting `atempo=2` stages to work around ffmpeg's native 0.5–2.0 per-filter limit, then applies the remainder. Directly portable logic for any speed-change feature needing >2x or <0.5x with audio.

4. **Percent-based watermark overlay with dynamic scale-to-source-width**: `[1:v]scale=<pxFromPercent>:-1,format=rgba,colorchannelmixer=aa=<opacity>[wm];[0:v][wm]overlay=x=...:y=...`. Clean pattern for resolution-independent watermarking, including a manual-percent-position mode via `main_w*X/100`.

5. **Single-matrix partial grayscale via `colorchannelmixer`** (blackwhite intensity): interpolates the color-preserving identity matrix and the BT.601 luma matrix by a blend factor in one filter invocation — avoids a two-pass blend/overlay approach entirely. Same interpolation trick reused for the sepia preset (interpolating identity → sepia matrix).

6. **Audio-stream silence detection via `volumedetect` before extraction**: `-af volumedetect -f null <null-output>`, parse `max_volume: X dB` from stderr, skip streams `<=-60dB`. Cheap way to avoid producing junk output files for muted/silent tracks in multi-track extraction.

7. **Per-stream bitrate estimation from raw ffprobe packet data** (`estimateAudioStreamBitrate`): when container/stream metadata doesn't reliably report bitrate (common for some audio-in-video muxes), sums `packet=size` over the stream's actual packet span (`pts_time`/`duration_time`) and derives `bits = bytes*8 / durationSpan`, then snaps to the nearest "common" bitrate (24/32/.../512 kbps) if within 8% — turns a noisy raw number into a believable nominal bitrate. Directly reusable ffprobe recipe: `ffprobe -select_streams a:<i> -show_entries packet=size,pts_time,dts_time,duration_time -of json <input>`.

8. **Manual EXIF orientation parsing in pure code, no external lib** (`parseJpegExifOrientation`/`parseTiffExifOrientation`): walks JPEG APP1/Exif segments and TIFF IFD entries by hand to find tag `0x0112` (Orientation) and reads only first 256KB of the file (fast, avoids loading entire large images just for orientation). Good pattern to replicate in Python with a manual byte-parser or a lightweight lib (`exifread`/`piexif`) instead of shelling out.

---

## BUGS / FRAGILE PATTERNS (candidates to fix / not carry forward as-is)

1. **`"NUL"` hardcoded as ffmpeg's null output sink** in `analyzeAudioStreamActivity` (`-f null NUL`) — `NUL` is the Windows null device. On Linux/macOS this would create/attempt to write to a literal file named `NUL` in the working directory instead of discarding output (ffmpeg's cross-platform null muxer target is `-` or `/dev/null`, or better, just `-f null -` works everywhere). Given `resolveFfmpegPath`/`resolveFfprobePath` also hardcode `.exe` binary names and Windows-reserved-filename sanitization elsewhere, this app appears to be **Windows-only in practice** despite Electron's cross-platform capability — worth normalizing to `os.devNull` or `-f null -` in a portable engine.

2. **Progress parsing relies on ffmpeg's default human-readable stderr `time=` lines**, which are locale-dependent in formatting for some ffmpeg builds/versions and not guaranteed to appear at a fixed cadence. `-progress pipe:1` (or a file/socket target) with `-nostats` would give a stable `key=value` machine-readable stream instead — worth adopting for the Python engine rather than porting the regex-scrape approach.

3. **`rotate_video`'s `"mirror"` option has no filter mapping** — `getRotationFilter`/`buildRotateArgs`'s filter map only has `none/cw/ccw/180`; `mirror` falls through to `null`, meaning selecting "Mirror" in the UI silently produces a straight re-encode with **no rotation/flip applied at all**. This looks like a genuine bug (the FIELD_DEFS/help text/field override explicitly advertise a 5th "Odbicie lustrzane"/"Mirror" option that does nothing).

4. **`trim_audio` and `audio_settings` never set an explicit audio codec**, relying on ffmpeg's default encoder selection from the output file extension (hardcoded `.mp3`). This is *fragile-by-omission*: if the default `libmp3lame` behavior/quality settings change across ffmpeg versions, output characteristics shift silently; an explicit `-c:a libmp3lame -b:a <default>` would be more future-proof. It also means these two ops implicitly ignore any user "keep original format" expectation — always MP3 regardless of source.

5. **64KB tail-only stderr buffering** (`MAX_STDERR_BUFFER_CHARS`) discards the *beginning* of stderr on very verbose, very long-running failing jobs — acceptable in practice since ffmpeg errors are almost always at the end, but worth flagging if replicating: a size-capped **ring buffer of both head and tail** would be strictly safer for post-mortem debugging.

6. **Duplicate/dead code**: `buildRotateArgs`'s inline `filter` map and the separate top-level `getRotationFilter` function are byte-for-byte identical dictionaries maintained in two places — `buildImageRotateFlipArgs` uses `getRotationFilter` while `buildRotateArgs` (video) has its own private copy. Low risk but a drift hazard (e.g. the `mirror` bug above would be easy to "fix in one and not the other").

7. **Silent bitrate-source fallback chain** in several video ops (color-style, watermark, speed) falls back to `-crf 18 -preset medium` when source bitrate is unknown, but the "prefer source bitrate" path uses `sourceBitrateKbps*2` for `-bufsize` unconditionally — for very low source bitrates this can produce a tiny buffer that increases likelihood of momentary rate-control overshoot; not wrong, just worth tuning constants when porting rather than copying verbatim.

8. **`add_metadata`'s "no supported change" path silently falls back to a plain file copy** (`copyFileWithoutMetadataChanges`) with only a `status:"warning"` result — a Python port should decide explicitly whether "silently produce an unmodified copy" is the desired UX vs. surfacing a hard error, since it's easy to miss the warning in a large batch.
