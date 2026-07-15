# Hardware Transcoding — Execution Plan

Status as of 2026-07-15. Living document; update as phases land.

## Where we are

**Phase 1 — plumbing: DONE** (commit `5971503`, branch `feat/hardware-transcoding`)

- Debian's stock ffmpeg already ships `h264_qsv` / `h264_vaapi` / `h264_nvenc`
  (+ HEVC variants) and `--enable-libmfx`. **No jellyfin-ffmpeg or custom build
  needed** — the only gap was a VA-API driver in the image.
- `Dockerfile` installs iHD (Intel Gen8+), i965 (pre-Gen8), mesa (AMD), `vainfo`.
  Drivers are inert with no GPU device, so one image serves everyone.
- Opt-in overlays; base `docker-compose.yml` stays GPU-less:
  - `docker-compose.gpu.yml` — Intel + AMD (both are `/dev/dri`)
  - `docker-compose.gpu-nvidia.yml` — NVENC via nvidia-container-toolkit
- `RENDER_GID` is required + host-specific (Ubuntu 24.04: 993, Debian 12: 106).

Verified on Intel UHD 630 (Coffee Lake) passed through to a KVM guest:
`VAProfileH264High : VAEntrypointEncSlice`, and both `h264_vaapi` and `h264_qsv`
complete real encodes **inside the container**.

## The vision — typed workers + a split slider

Today transcodarr runs two live-resizable pools (auto from the watchdog, manual
from the UI). The feature adds a **second axis: backend**.

Every worker becomes a **HW worker** or a **SW worker**, and a slider splits the
budget between the two lanes:

```
Total workers: 4
[ SW ●●○○ HW ]   → 2 software + 2 hardware
```

- Hard left  → pure software (today's behaviour)
- Hard right → pure hardware (max throughput, near-zero CPU)
- Middle     → **both silicon budgets working at once**

Why the mix matters: an iGPU encoding via QSV and CPU cores encoding via x264 are
*independent* silicon. Running both is strictly more throughput than either alone
— HW clears the backlog fast, SW does the keepers at max quality.

**Wide support is a first-class goal**: capability-detect per host and offer
QSV/VAAPI (Intel/AMD) or NVENC (NVIDIA), with **software always available as the
guaranteed fallback lane**. Same UI everywhere; a Pi runs all-SW, a 3060 box runs
all-NVENC.

## Remaining phases

### Phase 2 — Capability detection
Probe at startup and publish a capability record **per node**:
`{ node, backends: [qsv|vaapi|nvenc|sw], codecs, devices, max_sessions }`
via `ffmpeg -encoders/-hwaccels`, device-node presence, and a `vainfo` probe.
Surface it in the System tab. **Node-scoped from day one** (see Multi-node).

### Phase 3 — Encoder abstraction
`ffmpeg/transcode.py`: `_VIDEO_ENCODER` becomes `(codec, backend) → encoder`,
and `_video_encoder_args()` / `build_ffmpeg_cmd()` branch per backend:
- HW needs a different filter graph (`hwupload`, `scale_vaapi`/`scale_qsv`, `format`)
- Quality params don't share a scale — translate
  `x264 CRF ↔ QSV global_quality/ICQ ↔ NVENC cq`
- Software path stays exactly as-is (no regression risk)

### Phase 4 — Typed workers + slider
- Worker identity carries backend (and node — see below)
- Slider allocates the pool across lanes; job → lane routing
- **HW failure auto-retries on the SW lane** (makes "hardware on" safe by default)

### Phase 5 — HDR
Current tonemap is software `zscale`. HW tonemap (VAAPI/QSV + OpenCL) is the
fiddly bit; `VAEntrypointVideoProc` is confirmed available on Gen9.5.
v1: route HDR titles to the SW lane, add HW tonemap later.

## Multi-node — decide NOW, build later

Multi-node is the next major feature after this. These choices are cheap now and
expensive to retrofit:

1. **A worker is `(node, backend)`, not just `backend`.** Ship the node dimension
   immediately with `node_id = "local"`. Otherwise multi-node is a rewrite of the
   pool, not an extension.
2. **Capability registry is per node.** One entry today, N entries later reported
   by node agents. Don't assume the local host's encoders are *the* encoders.
3. **Scheduling becomes node-aware.** Routing must ask *which node can do this*
   (QSV here, NVENC there, SW anywhere) — a natural fit for the existing
   Auto-preset rule engine.
4. **Data locality is the real constraint.** Video is too big to ship per job, so
   nodes realistically need shared storage (NFS/SMB) — or the scheduler must
   respect "this node can see this path". Decide the storage contract before the
   scheduler.
5. **No localhost assumptions** in paths, device probing, or progress reporting.

### Per-backend concurrency caps (important)

HW encoders have hard session limits — unlike CPU, which just gets slower:

| Backend | Realistic concurrent 1080p |
|---|---|
| Intel UHD 630 (QSV/VAAPI) | ~2–3 before throughput/quality degrade |
| NVIDIA consumer (NVENC) | **driver-capped** — Pascal/GTX 1080 = 2 sessions |
| Software (x264) | bounded by cores, degrades gracefully |

So the HW lane must have a **sane per-device cap**, not an arbitrary slider max —
otherwise users will set "8 HW workers" on an iGPU and wonder why it thrashes.
Detect or ship documented defaults per backend.

## Open questions

1. HW vs SW lanes: separate presets, or one preset with quality auto-translated
   per backend?
2. Job → lane routing: first-free-slot, or rule-based (4K/HDR → SW, rest → HW)?
3. Should the slider be per-node once multi-node lands, or a global pool view?
