# Linux Port Assessment — media-by-outlaw2082

Repo: `Outlaw2082/media-by-outlaw2082` (upstream)
Scope: Electron + ffmpeg/ffprobe media processing app, currently Windows-only (portable .exe).
Analysis is read-only; no files were modified.

## 1. FFmpeg/ffprobe binary handling

The app already uses cross-platform npm packages, not vendored Windows binaries directly in code:

- `ffmpeg-static` (^5.3.0) — resolves the correct platform binary via its own `require`-time logic (`package.json` dependency, imported in `electron/main.cjs:2` as `ffmpegPathFromPackage`).
- `ffmpeg-ffprobe-static` (^6.1.1) — same pattern, imported at `electron/main.cjs:3`, exposes `.ffprobePath`.

Both packages ship platform-specific binaries and pick the right one for `process.platform`/`process.arch` at install time — **they are genuinely cross-platform**, they just happen to have only installed the Windows `.exe` variant here because installs targeted Windows.

Runtime path resolution — `electron/main.cjs`:

```js
function resolveFfmpegPath(language = "pl") {
  if (!ffmpegPathFromPackage) throw new Error(...);
  if (!app.isPackaged) return ffmpegPathFromPackage;   // dev mode: use package's own resolved path (already platform-correct)
  return resolvePackagedToolPath("ffmpeg.exe", "ffmpeg-static", ffmpegPathFromPackage);  // packaged mode: HARDCODES "ffmpeg.exe"
}

function resolveFfprobePath(language = "pl") {
  const ffprobePathFromPackage = ffmpegProbeStatic?.ffprobePath;
  if (!ffprobePathFromPackage) throw new Error(...);
  if (!app.isPackaged) return ffprobePathFromPackage;
  return resolvePackagedToolPath("ffprobe.exe", "ffmpeg-ffprobe-static", ffprobePathFromPackage);
}

function resolvePackagedToolPath(binaryName, packageDir, packagePath) {
  const candidates = [
    path.join(process.resourcesPath, "bin", binaryName),
    path.join(process.resourcesPath, "app.asar.unpacked", "node_modules", packageDir, path.basename(packagePath))
  ];
  return candidates.find((candidate) => fsSync.existsSync(candidate)) || candidates[0];
}
```

Findings:
- **In dev mode (`!app.isPackaged`), the code is already platform-agnostic** — it just trusts whatever `ffmpeg-static`/`ffmpeg-ffprobe-static` resolved, which on Linux would be a Linux binary automatically (assuming the packages are installed with a Linux target, or run under Linux npm so `postinstall` fetches the Linux binary). `npm start` on Linux should work close to out-of-the-box once deps are installed on a Linux host.
- **In packaged mode, `resolveFfmpegPath`/`resolveFfprobePath` hardcode `"ffmpeg.exe"` / `"ffprobe.exe"`** as the binary name passed to `resolvePackagedToolPath` (`main.cjs:515`, `main.cjs:528`). This must become platform-conditional, e.g. `process.platform === "win32" ? "ffmpeg.exe" : "ffmpeg"` (no extension on Linux binaries).
- The `-c:v`/spawn invocation itself (`spawn(ffmpegPath, args, { windowsHide: true, stdio: [...] })`) is fine cross-platform — `windowsHide` is a no-op on non-Windows, `shell` is never set to `true` anywhere (confirmed via grep — zero `shell: true` occurrences), so no shell-quoting/injection issues to fix.
- One genuine Windows-only runtime bug: `electron/main.cjs:708`, in `analyzeAudioStreamActivity`, the ffmpeg null-output sink is hardcoded as the Windows null device:
  ```js
  "-f", "null",
  "NUL"
  ```
  On Linux ffmpeg this must be `/dev/null` (or, more robustly, just `-f null -` piping to nowhere / using the platform-neutral pattern `process.platform === "win32" ? "NUL" : "/dev/null"`). This is the **one concrete runtime-breaking Windows-ism** in the ffmpeg invocation code — on Linux, this analysis call would try to write to a file literally named `NUL` and either fail or silently produce a stray file.

**What needs to change to use Linux ffmpeg:** (a) make `resolvePackagedToolPath`'s binary name platform-conditional, (b) fix the `NUL` literal, (c) ensure `ffmpeg-static`/`ffmpeg-ffprobe-static` install the Linux binaries during `npm install` on Linux (this is automatic behavior of those packages — no code change needed, but CI/dev docs should stop claiming "Windows only" tooling).

## 2. Build / packaging

`package.json` "build" section (electron-builder):

```json
"win": {
  "signAndEditExecutable": false,
  "icon": "build-resources/app-icon.ico",
  "target": ["portable"]
}
```

- Only a `win` target block exists. No `linux` block. electron-builder needs a `linux` key with a `target` array, e.g. `"target": ["AppImage"]` (simplest, single-file, closest analog to the "portable" experience) or `["AppImage", "deb"]` for a repo-friendly release too.
- `extraResources` currently hardcode `.exe` sources:
  ```json
  "extraResources": [
    { "from": "node_modules/ffmpeg-static/ffmpeg.exe", "to": "bin/ffmpeg.exe" },
    { "from": "node_modules/ffmpeg-ffprobe-static/ffprobe.exe", "to": "bin/ffprobe.exe" },
    { "from": "third_party/ffmpeg", "to": "licenses/ffmpeg" },
    { "from": "THIRD_PARTY_NOTICES.md", "to": "licenses/THIRD_PARTY_NOTICES.md" }
  ]
  ```
  These need to become platform-specific blocks. electron-builder supports per-target `extraResources` inside the `win`/`linux` config blocks (or a build script step) — e.g. under `linux.extraResources` reference `node_modules/ffmpeg-static/ffmpeg` and `node_modules/ffmpeg-ffprobe-static/ffprobe` (no extension). The top-level `files` array also explicitly excludes `.exe` files from being packed into asar (`"!node_modules/ffmpeg-static/ffmpeg.exe"` etc.) — a Linux equivalent exclusion for the no-extension binaries would be needed to avoid double-bundling inside asar.
- `icon: "build-resources/app-icon.ico"` is Windows-specific (`.ico`). Linux targets (AppImage/deb) expect a PNG (ideally 512x512) via `linux.icon` — the app already has `ico.png` at repo root used for the BrowserWindow icon at runtime (`main.cjs:24,560`), so a Linux packaging icon likely already exists or is trivial to derive.
- `appId`/`productName`/`artifactName` are platform-neutral and reusable.
- `signAndEditExecutable: false` and lack of code signing is a Windows-target-specific concern only (SmartScreen warning) — not applicable to Linux; no Linux code-signing is configured or required for AppImage.

**Batch files / npm scripts:**
- `BUDUJ_PORTABLE.bat` and `START_TUTAJ.bat` are `.bat` (cmd.exe) launcher scripts, both Windows-only by definition — they'd need Linux shell-script equivalents (`.sh`) for parity, or just be documented as Windows-only convenience wrappers while `npm run build:portable` / `npm start` remain the cross-platform entry points.
- `package.json` scripts:
  - `"start": "electron ."` — cross-platform, fine as-is.
  - `"build:portable": "npm run clean:portable && electron-builder --win portable && ..."` — hardcodes `--win portable`. Would need a new script, e.g. `"build:linux": "npm run clean:portable && electron-builder --linux AppImage && node scripts/prepare-linux-release.cjs"` (new script, mirroring the portable-release prep).
  - `"pack": "electron-builder --dir"` — already platform-neutral (builds for host platform unpacked).

**`scripts/clean-portable.cjs`** — pure Node fs, no Windows-isms, works as-is on Linux.

**`scripts/prepare-portable-release.cjs`** — hardcodes `dist/Media by Outlaw2082.exe` as source and destination filenames:
  ```js
  const distExe = path.join(root, "dist", "Media by Outlaw2082.exe");
  const releaseExe = path.join(releaseDir, "Media by Outlaw2082.exe");
  ```
  This is packaging-script logic, not runtime — would need a Linux-specific variant (or parametrize by extension/artifact name) since electron-builder's Linux AppImage output filename won't be `....exe`. Purely a "needed to PACKAGE a Linux release" item, not a "needed to RUN" item.

**`scripts/verify-release-compliance.cjs`** — this is a release-compliance/license checker, not a build step, but it hardcodes Windows binary expectations:
  ```js
  const binaries = {
    "ffmpeg.exe": "node_modules/ffmpeg-static/ffmpeg.exe",
    "ffprobe.exe": "node_modules/ffmpeg-ffprobe-static/ffprobe.exe"
  };
  ```
  and its forbidden-tracked-files check explicitly allow-lists `.exe` files only (`/\.exe$/i.test(file)`) without an equivalent check for a Linux binary being accidentally committed. For a genuine dual-platform release process this script would need to accept a platform argument or run per-target. This only matters for **packaging/release QA**, not for running the app.

**`scripts/layout-smoke.cjs`** — pure Electron/BrowserWindow automation, no OS-specific code; runs fine on Linux (needs an X/Wayland display or `xvfb-run`, same as any Electron on Linux, not project-specific).

## 3. Runtime OS-specific code — full occurrence list

Grep of `electron/`, `src/`, `scripts/` for `process.platform`, `win32`, `darwin`, `.exe`, backslash/`C:\`/`NUL` literals, `cmd`/`powershell`, `shell: true`:

| File:Line | Pattern | Assessment |
|---|---|---|
| `electron/main.cjs:26` | `if (process.platform === "win32") { app.setAppUserModelId(...) }` | Correct, intentional Windows-only branch (AppUserModelId is a Windows taskbar/notification concept). No change needed — already platform-guarded. |
| `electron/main.cjs:1803` | `if (!isLayoutSmoke && process.platform !== "darwin")` | Standard Electron idiom (don't quit on window-all-closed on macOS). Correct as written; Linux falls into the `app.quit()` branch same as Windows — fine. |
| `electron/main.cjs:515` | `resolvePackagedToolPath("ffmpeg.exe", ...)` | **Needs fix** — hardcoded `.exe`, must branch on `process.platform`. |
| `electron/main.cjs:528` | `resolvePackagedToolPath("ffprobe.exe", ...)` | **Needs fix** — same as above. |
| `electron/main.cjs:708` | `"NUL"` (ffmpeg null-sink output path in `analyzeAudioStreamActivity`) | **Needs fix** — Windows null device literal; breaks on Linux. Use `/dev/null` or platform-conditional. |
| `scripts/prepare-portable-release.cjs:5,7,15` | `"Media by Outlaw2082.exe"` (×3) | Packaging-script only; needs Linux artifact-name equivalent for a Linux release script. |
| `scripts/verify-release-compliance.cjs:49-50` | `ffmpeg.exe` / `ffprobe.exe` map keys, and path values pointing at `.exe` | Release-compliance-only; would need per-platform binary map. |
| `scripts/verify-release-compliance.cjs:79` | `/\.exe$/i.test(file) && !/^approved-assets\//` (forbidden-tracked-files) | Release-compliance-only; fine as-is for Windows, would want an equivalent guard for Linux binaries if they're ever accidentally tracked. |
| `scripts/verify-release-compliance.cjs:88` | `.includes("C:\\Users\\")` (checks source files don't leak a Windows author's home path) | Compliance-only, no functional impact; harmless on Linux (will just never match, which is fine). |
| `package.json:build.win.icon` | `"build-resources/app-icon.ico"` | Windows packaging config only. |
| `package.json:build.extraResources` (×2 ffmpeg/ffprobe.exe entries) | `.exe` extraResources | **Needs Linux equivalent** in packaging config. |
| `BUDUJ_PORTABLE.bat`, `START_TUTAJ.bat` | Windows batch scripts | Windows-only launcher convenience, not app logic. |

**Renderer-side (`src/renderer.js`) path-separator handling** — these are actually *good, defensive* cross-platform code, not bugs:
- `src/renderer.js:1052` — `String(filePath || "").replace(/\\/g, "/")` — normalizes backslashes to forward slashes (handles paths that arrive from the Windows-style native dialog); harmless/no-op on Linux paths which never contain backslashes.
- `src/renderer.js:2504`, `3207`, `4777` — `.split(/[\\/]/).pop()` — splits on either separator to extract a basename; works correctly on both platforms.
- `src/renderer.js:1567`, `3345` — `/[<>:"/\\|?*]/` — filename-sanitization regex stripping characters illegal in **Windows** filenames (also strips `/` and `\`, which is safe/desirable on Linux too, just stricter than Linux requires). Not a bug — overly conservative but not broken.

**Not found anywhere in the tree:** registry access, `cmd.exe`/PowerShell invocation, `shell: true` in any `spawn`/`exec` call, Windows-only Electron APIs (e.g. Squirrel/`app.setAppUserModelId` is the only one, already guarded), hand-built `C:\` paths in application logic (only appears in the compliance-checker string literal above), SmartScreen/code-signing logic embedded in runtime code (it's purely an electron-builder config flag).

No `.github/workflows/*.yml` CI exists (only issue templates under `.github/ISSUE_TEMPLATE/`), so there is no CI matrix to extend — a Linux build job would need to be created from scratch if the PR wants CI coverage, but that's optional/separate from the port itself.

## 4. Overall verdict

**Difficulty: Small.**

The app was architecturally already prepared for this — it uses the cross-platform `ffmpeg-static`/`ffmpeg-ffprobe-static` npm packages rather than vendoring `.exe` files directly in source, spawns processes as argument arrays with no shell (`shell: true` nowhere), and the one `process.platform === "win32"` branch already in the code is correctly guarded rather than assumed. The blockers are a handful of hardcoded strings, not structural rework.

**Windows-specific touch-point count: 12** distinct locations across 4 files needing a change (2 in `electron/main.cjs` runtime code, 1 `NUL` literal, 3 in `prepare-portable-release.cjs`, 2 in `verify-release-compliance.cjs`, 2 in `package.json` build config, 2 `.bat` launcher files) — plus 2 correctly-already-guarded `process.platform` checks that need no change.

### Checklist — needed to RUN on Linux (dev/`npm start`, no packaging)

1. Fix `electron/main.cjs:708` — replace the hardcoded `"NUL"` ffmpeg null-sink argument with a platform-conditional (`process.platform === "win32" ? "NUL" : "/dev/null"`), used in `analyzeAudioStreamActivity`'s silent-audio-detection ffmpeg call. This is the only genuine runtime bug blocking correct behavior on Linux — everything else in dev mode (`!app.isPackaged`) already resolves ffmpeg/ffprobe paths correctly via the npm packages.
2. Run `npm install` on a Linux host so `ffmpeg-static`/`ffmpeg-ffprobe-static` postinstall fetches Linux binaries (automatic, no code change — just confirm/document it in `BUILDING.md`, which currently says "Development and packaging target Windows").
3. Smoke-test `npm start` and `npm run test:layout` on Linux (Electron on Linux needs a display server; `layout-smoke.cjs` itself has no OS-specific code).
4. Run `npm run test:security` — pure Node, should pass unchanged.

### Checklist — additionally needed to PACKAGE a Linux release

5. Add a `linux` block to `package.json` `build` config with `target: ["AppImage"]` (or `["AppImage", "deb"]`) and a Linux-appropriate `icon` (PNG, e.g. reuse/derive from existing `ico.png`).
6. Fix `resolveFfmpegPath`/`resolveFfprobePath` in `electron/main.cjs` (lines 515, 528) to pass a platform-conditional binary name (`"ffmpeg"`/`"ffprobe"` with no extension on non-Windows) into `resolvePackagedToolPath`.
7. Add Linux-specific `extraResources` entries (or a shared/platform-conditional config) pointing at `node_modules/ffmpeg-static/ffmpeg` and `node_modules/ffmpeg-ffprobe-static/ffprobe` (no `.exe`), mirroring the existing Windows entries, and add corresponding `files` exclusions to keep them out of asar.
8. Add a `build:linux` (or generalized `build:portable`) npm script that calls `electron-builder --linux AppImage`.
9. Generalize or duplicate `scripts/prepare-portable-release.cjs` for the Linux artifact name/extension (electron-builder's Linux output won't be `Media by Outlaw2082.exe`).
10. Generalize `scripts/verify-release-compliance.cjs`'s hardcoded `binaries` map and forbidden-file `.exe` check to cover the Linux binary variants for release QA parity.
11. (Optional, nice-to-have) Add Linux shell-script equivalents of `BUDUJ_PORTABLE.bat`/`START_TUTAJ.bat` for contributor convenience.
12. (Optional) Add a GitHub Actions CI workflow building both Windows and Linux artifacts — none currently exists.
13. Update `docs/BUILDING.md` (currently states "Development and packaging target Windows" and gives only PowerShell commands) and `docs/FFMPEG.md`/`third_party/ffmpeg/` GPL compliance docs to also cover the Linux ffmpeg binary's build info/checksums/license notices (same GPL obligations apply, just a different binary artifact — `ffmpeg-static`'s Linux build provenance would need documenting alongside the existing Windows gyan.dev entry).

Items 1-4 are what a contributor needs to run/develop against Linux locally. Items 5-13 are what's needed to ship an official Linux release artifact from this repo's build tooling — that's the larger, more process-heavy half of the work, but still mechanical/config-level, not a rewrite.
