# Changelog

## v1.3.0 (2026-07-17)

Hardware-accelerated transcoding.

### Added
- **Hardware transcoding** — full GPU pipeline (decode → scale → HDR tone-map → encode) for Intel Quick Sync (QSV), VA-API (Intel/AMD), and NVIDIA NVENC. Frames stay on the GPU end-to-end; ~4× faster and ~13× less CPU than software on an Intel iGPU. Opt-in via GPU compose overlays (`docker-compose.gpu.yml`, `docker-compose.gpu-nvidia.yml`) — the base image stays GPU-less.
- **Per-node capability detection** — probes each backend with a real test encode (not just `ffmpeg -encoders`), reporting the encode/decode codecs and tone-mappers it can actually use. Exposed at `GET /api/system/capabilities`.
- **Settings → Hardware** — detected backends, read-only passthrough config, and how to enable.
- **Per-preset encode backend** — hardware vs. software is an encoding-preset setting routed by the Auto rules; hardware presets are seeded per detected backend (e.g. `4K Downscale (QSV)` / `4K Downscale (VA-API)`).
- **GPU HDR→SDR tone-mapping** (VA-API / OpenCL), chosen per file, with a software fallback.
- **Automatic software fallback** — any backend, codec, or source that isn't hardware-capable (e.g. AV1 on a GPU without AV1 decode) drops to software per-job instead of failing; the encode backend is labeled in the logs.
- `HW_MAX_WORKERS` — global cap on concurrent hardware encodes for GPU session limits.
- README **Hardware Transcoding** section with screenshots.

### Fixed
- Re-transcode loop for files whose metadata sidecar was missing — the source is now identified via a Radarr/Sonarr path lookup and cleaned up, and the transcode-history circuit breaker records again (a swallowed `NameError` had silently disabled it).
- `build_ffmpeg_cmd` tests read the live settings database — they passed in CI but failed on a configured host, so they were guarding nothing there.

## v1.2.0 (2026-06-27)

First release published as a prebuilt Docker image — `docker pull ghcr.io/reedylab/transcodarr:1.2.0` (or `:latest`).

### Added
- **Real-time UI via Server-Sent Events** — status, media, logs, and system streams replace polling; keyed row diffing updates table rows in place instead of rebuilding the whole table
- **Server-side pagination** with infinite scroll, status filtering, total counts, and bulk-by-filter actions (transcode / ignore / set-output by filter, plus select-all-matching)
- **Postgres-backed media cache** with one-shot JSON→DB migration; cache survives container rebuilds
- **Re-encode-only mode** with optional watch paths, for batch re-encoding existing libraries
- **Re-transcode circuit breaker** to stop repeated failed re-transcodes
- Auto-start watchdog on container boot when `AUTO_WORKERS > 0`
- Official prebuilt Docker image published to GHCR via GitHub Actions CI (#3, #5)
- Mobile-responsive UI (header wrap, 2-column tile grid, full-screen modal) and a floating scroll-to-top button
- Auto-detect Radarr/Sonarr path remapping; configurable extra-file-extensions
- Static assets auto cache-busted by file mtime

### Changed
- **BREAKING:** renamed `X264_THREADS` → `ENCODER_THREADS`; the thread setting now applies to all codecs, not just x264
- **BREAKING:** replaced hardcoded media folder names with four configurable volume mounts (`MOVIES_WATCH_PATH`, `TV_WATCH_PATH`, `MOVIES_OUTPUT_PATH`, `TV_OUTPUT_PATH`); compose refuses to start if any is unset
- Consolidated the settings UI from 10 sections to 4
- ffmpeg command builder now honors codec and HDR settings
- SSE media stream narrowed to in-flight items only, shrinking the first-connect payload
- README overhaul: restructured Quick Start, refreshed screenshots, updated API table

### Fixed
- Tile-view progress percentage showing 100× too large
- Hardened `meta.json` handling against malformed/partial files
- Bulk enrich now walks the output folders instead of the (possibly empty) DB cache
- Radarr/Sonarr path-lookup fallback and corrected path-remap direction for metadata enrichment

## v1.1.0 (2026-03-31)

### Changed
- **Backend migrated from Flask+Gunicorn to FastAPI+Uvicorn** — async-native framework, auto-generated API docs at `/docs`, identical API contract
- Web layer split from single 3600-line file into 9 focused router modules
- Entrypoint switched to `uvicorn` with single-worker model (in-process state preserved)
- UI dark theme aligned with shared media stack CSS variables (`--bg: #0d1117`, `--bg-card: #161b22`, `--accent: #58a6ff`)
- Logs panel now fills available viewport height

### Added
- `/health` endpoint for container health checks
- `/docs` and `/openapi.json` auto-generated API documentation (FastAPI built-in)
- Metadata enrichment system — fetch metadata, NFO files, and posters for individual or all media files
- Per-episode plot descriptions pulled from Sonarr and written into episode NFOs
- Bulk enrichment with progress tracking and cancellation (`/media/enrich-all`)
- "Meta" button on individual items and "Enrich All" bulk action in the web UI
- Optional centralized syslog logging support via `SYSLOG_ADDRESS` env var

### Fixed
- 10-bit SDR content (anime, Blu-ray rips) no longer misdetected as HDR, which caused zscale tonemapping crashes
- Subtitle fallback path crash (`NameError: tmp_no_subs`) when primary transcode failed
- Verify rejecting valid transcodes of large files — duration tolerance relaxed from 2% to 5%

### Removed
- Flask, Flask-CORS, Gunicorn dependencies
- `FLASK_ENV` environment variable (no longer needed)

## v1.0.0 (2026-03-11)

- Initial public release
