# media-engine

A standalone, headless ffmpeg operation engine. Recipes ripped from
[media-by-outlaw2082](https://github.com/Outlaw2082/media-by-outlaw2082) (GPL-3.0,
a Windows Electron GUI over ffmpeg) and reimplemented clean in **stdlib-only Python**
so they can be driven agentically / headless — the video/audio sibling to the GIMP MCP.

Standalone by design: no coupling to the GIMP MCP or the evo-x2 gen tools
(`genme`/`clipme`/`swapvid`). Plug it in where you want it.

## Status

**All 19 inventoried operations ported and verified end-to-end on ffmpeg 4.4.2
(19/19 smoke pass).** Each is exposed automatically in both the CLI and MCP faces.

- **Video (10):** `video_to_gif`, `video_speed`, `add_watermark`, `convert_video`,
  `resize_video`, `trim_video`, `extract_audio`, `rotate_video`, `grayscale_video`,
  `burn_subtitles`
- **Audio (5):** `convert_audio`, `trim_audio`, `audio_settings`, `add_metadata`,
  `audio_speed`
- **Image (4):** `resize_image`, `rotate_image`, `grayscale_image`, `sharpness_image`

Highest-value recipes (see `recon/INVENTORY-operations.md` for the full spec): two-pass
GIF palette, atempo-chained speed, letterbox-in-one-filter, percent watermark overlay,
single-matrix grayscale/sepia.

### Fixes applied over the ripped source

- **`rotate_video` "Mirror"** — upstream exposed it in the UI with no filter mapping
  (silent no-op); now emits `hflip`.
- **`trim_audio` / `audio_settings`** — upstream set no explicit `-c:a`, relying on
  ffmpeg's fragile default-by-extension; now set the codec explicitly.
- **`audio_speed`** — upstream's UI said "percent" but clamped a raw 0.5–2.0 factor;
  now uses the same percent scale as `video_speed` with an atempo chain (no ceiling).
- **`sharpness_image`** — inventory's `unsharp` amount mapping (0..20) exceeds ffmpeg's
  valid `luma_amount` range `[-2, 5]` and errors on every ffmpeg; remapped to 0..5.
- Engine-wide: `-progress pipe:1` (not stderr scraping), `/dev/null` vs `NUL`,
  ffmpeg-version guard for `-fps_mode` / `-vsync`.

## Layout

```
engine/
  ffmpeg.py       binary resolution, -progress pipe:1 parsing, error tails,
                  cross-platform null device, ffmpeg-version capability probe
  probe.py        ffprobe -> MediaInfo (width/bitrate/audio presence)
  operations.py   OPERATIONS registry: Param specs + build(ctx)->[Pass]  (source of truth)
cli.py            CLI face:  list | info <op> | run <op> -i IN -o OUT --param val
server.py         MCP face:  auto-generates one typed tool per op from OPERATIONS
webserver.py      Web face:  stdlib HTTP server + drag-and-drop web UI
web/index.html    the single self-contained page (vanilla JS, no build step)
recon/            the ripped-source inventory + Linux-port assessment
```

All three faces drive the same `OPERATIONS` registry. Port an op once in
`operations.py` and it appears in the CLI, as an MCP tool, **and** in the web UI
automatically — zero per-face code.

## Usage

```
python3 cli.py list
python3 cli.py info video_to_gif
python3 cli.py run video_to_gif -i clip.mp4 -o out.gif --gifFpsPreset 15
python3 cli.py run video_speed  -i clip.mp4 -o fast.mp4 --speed 100
python3 cli.py run video_to_gif -i clip.mp4 -o out.gif --dry-run   # print ffmpeg cmds
```

### MCP face

`server.py` is an MCP stdio server. Each operation is exposed as a typed MCP tool
(choice params become enum-validated, `output_path` optional & derived, every tool
takes `dry_run`). Install and register it with any MCP-compatible client:

```
pip install -r requirements.txt      # only fastmcp; engine is stdlib
python3 server.py                    # stdio entrypoint your MCP client launches
```

An agent then calls `list_operations` to discover ops and invokes any op
(`video_to_gif`, `video_speed`, `add_watermark`, …) directly with a params dict.

### Web UI

`webserver.py` serves a drag-and-drop web UI over a stdlib HTTP server — **no
dependencies at all** (not even fastmcp; it's pure Python + one self-contained
HTML page). Drop in a file, pick an operation, tweak the auto-generated form,
run — the file is processed locally through ffmpeg and streamed straight back to
download. Deep-link an operation with `/#op_id`.

```
python3 webserver.py            # then open http://127.0.0.1:8765
python3 webserver.py --host 0.0.0.0 --port 9000
```

Binds localhost by default; it runs ffmpeg on uploaded input, so don't expose it
to an untrusted network.

## Improvements over the ripped source (recon-flagged)

- **`-progress pipe:1`** machine-readable progress instead of scraping locale-dependent
  stderr `time=` lines.
- **Cross-platform null device** (`/dev/null` vs `NUL`) — upstream hardcoded `NUL`, the
  one real runtime bug on non-Windows.
- **ffmpeg-version capability probe** — `-fps_mode` (≥5.0) vs legacy `-vsync` (older).
  Upstream bundled 6.1.1 and never hit this; a portable engine must.

## Next

- Add the MCP face (thin wrapper over `run_operation`) — the point of the whole thing.
- Port the remaining 16 ops from the inventory.
- Explicit audio codec on `trim_audio`/`audio_settings` (upstream relies on ext default).

## Give-back to upstream (independent track)

- Linux-support PR: fix the `NUL` literal (`main.cjs:708`) + add a `build.linux`
  AppImage block. Assessment: **small**. See `recon/ASSESSMENT-linux-port.md`.
- Bugfix PR: `rotate_video` "Mirror" option is exposed in the UI but has no filter
  mapping — silently no-ops.
</content>
