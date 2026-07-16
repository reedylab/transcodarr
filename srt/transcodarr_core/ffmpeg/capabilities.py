# srt/transcodarr_core/ffmpeg/capabilities.py
"""
Hardware-transcoding capability detection.

Answers "what can this node actually encode with?" — the input to backend
selection and, later, to HW/SW worker allocation.

A compiled-in ffmpeg encoder is NOT proof a backend works: Debian's ffmpeg ships
h264_qsv/h264_vaapi/h264_nvenc on every host, GPU or not. So a backend is only
reported available when all of these hold:

  * ffmpeg has the encoder compiled in   (ffmpeg -encoders)
  * the device node exists               (/dev/dri/renderD*, /dev/nvidia*)
  * the driver confirms an encode entrypoint (vainfo -> VAEntrypointEncSlice)

Results are node-scoped (`node_id`) from the outset so multi-node stays an
extension rather than a rewrite of this module.
"""
from __future__ import annotations

import glob
import logging
import os
import re
import subprocess
import tempfile
import threading
import time

_PROBE_TIMEOUT = 15  # seconds; probes must never hang startup

# Our codec keys -> the encoder each backend uses. Mirrors _VIDEO_ENCODER in transcode.py.
_SW_ENCODERS = {
    "h264": "libx264",
    "h265": "libx265",
    "hevc": "libx265",
    "vp9": "libvpx-vp9",
    "av1": "libsvtav1",
}

_HW_ENCODERS = {
    "qsv": {"h264": "h264_qsv", "h265": "hevc_qsv", "hevc": "hevc_qsv", "vp9": "vp9_qsv"},
    "vaapi": {"h264": "h264_vaapi", "h265": "hevc_vaapi", "hevc": "hevc_vaapi",
              "vp9": "vp9_vaapi", "av1": "av1_vaapi"},
    "nvenc": {"h264": "h264_nvenc", "h265": "hevc_nvenc", "hevc": "hevc_nvenc",
              "av1": "av1_nvenc"},
}

# VA profile prefix -> our codec key, for reading vainfo output.
_VA_PROFILE_CODECS = [
    ("VAProfileH264", "h264"),
    ("VAProfileHEVC", "hevc"),
    ("VAProfileVP9", "vp9"),
    ("VAProfileAV1", "av1"),
]

# Concurrent encode sessions a backend can sustain before throughput/quality suffer.
# These are hard ceilings on HW (unlike software, which just gets slower), so the
# worker allocator must respect them. Heuristic defaults; override via settings.
_DEFAULT_MAX_SESSIONS = {
    "qsv": 3,       # Intel iGPU: ~2-3 concurrent 1080p
    "vaapi": 3,
    "nvenc": 2,     # consumer NVIDIA driver cap (Pascal-era); newer allow more
    "software": None,  # bounded by cores, not a hard limit
}

_BACKEND_LABELS = {
    "qsv": "Intel Quick Sync (QSV)",
    "vaapi": "VA-API (Intel/AMD)",
    "nvenc": "NVIDIA NVENC",
    "software": "Software (libx264/x265)",
}

_cache: dict | None = None
_cache_lock = threading.Lock()


def _run(cmd: list[str]) -> str:
    """Run a probe command, returning stdout+stderr. Empty string on any failure."""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=_PROBE_TIMEOUT)
        return (p.stdout or "") + (p.stderr or "")
    except Exception as e:
        logging.debug("[CAPS] probe failed %s: %s", cmd[0], e)
        return ""


def _ffmpeg_encoders() -> set[str]:
    """Encoder names ffmpeg was built with."""
    out = _run(["ffmpeg", "-hide_banner", "-encoders"])
    # lines look like: " V....D h264_vaapi           H.264/AVC (VAAPI) (codec h264)"
    # the name charset also skips the legend rows (" V..... = Video").
    return set(re.findall(r"^\s*[A-Z.]{6}\s+([A-Za-z0-9_-]+)", out, re.MULTILINE))


def _ffmpeg_hwaccels() -> set[str]:
    out = _run(["ffmpeg", "-hide_banner", "-hwaccels"])
    lines = [ln.strip() for ln in out.splitlines()[1:]]
    return {ln for ln in lines if ln and " " not in ln}


def _render_nodes() -> list[str]:
    return sorted(glob.glob("/dev/dri/renderD*"))


def _nvidia_devices() -> list[str]:
    return sorted(glob.glob("/dev/nvidia[0-9]*"))


def _vainfo(device: str) -> dict[str, list[str]]:
    """Map VA profile -> entrypoints for a render node. {} if unusable."""
    out = _run(["vainfo", "--display", "drm", "--device", device])
    profiles: dict[str, list[str]] = {}
    for line in out.splitlines():
        m = re.match(r"\s*(VAProfile\S+)\s*:\s*(VAEntrypoint\S+)", line)
        if m:
            profiles.setdefault(m.group(1), []).append(m.group(2))
    return profiles


def _va_driver_name(device: str) -> str | None:
    out = _run(["vainfo", "--display", "drm", "--device", device])
    m = re.search(r"Driver version:\s*(.+)", out)
    return m.group(1).strip() if m else None


def _canonical(codec: str) -> str:
    """Collapse codec aliases ("h265" -> "hevc") so capability lists don't double-count."""
    return "hevc" if codec == "h265" else codec


def _encodable_codecs(profiles: dict[str, list[str]]) -> set[str]:
    """Codecs the driver reports a hardware ENCODE entrypoint for."""
    codecs: set[str] = set()
    for profile, entrypoints in profiles.items():
        if not any(e.startswith("VAEntrypointEncSlice") for e in entrypoints):
            continue
        for prefix, codec in _VA_PROFILE_CODECS:
            if profile.startswith(prefix):
                codecs.add(codec)
    return codecs


def _node_id() -> str:
    return os.environ.get("NODE_ID") or "local"


def _ffmpeg_filters() -> set[str]:
    out = _run(["ffmpeg", "-hide_banner", "-filters"])
    return set(re.findall(r"^\s*[A-Z.]{3}\s+(\S+)", out, re.MULTILINE))


def _detect_tonemappers(filters: set[str], dri_device: str | None) -> list[str]:
    """
    GPU HDR->SDR tonemappers usable on this node, fastest first.

    The filter existing in ffmpeg proves nothing — tonemap_opencl ships on every
    build but needs an OpenCL ICD, and tonemap_vaapi needs driver VPP support. So
    each is actually run once.

    The probe clip must carry HDR10 mastering-display metadata: tonemap_vaapi
    rejects input without it, so probing with a plain testsrc reports a false
    negative. (tonemap_opencl accepts either, which is why it stays in the list
    even though it measured ~4x slower than vaapi on Gen9.5.)
    """
    if not dri_device:
        return []

    found: list[str] = []
    with tempfile.TemporaryDirectory() as td:
        src = os.path.join(td, "probe.mp4")
        _run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
              "-f", "lavfi", "-i", "testsrc2=size=64x64:rate=1:duration=1",
              "-vf", "format=yuv420p10le", "-c:v", "libx265",
              "-x265-params",
              "colorprim=bt2020:transfer=smpte2084:colormatrix=bt2020nc:"
              "master-display=G(8500,39850)B(6550,2300)R(35400,14600)WP(15635,16450)L(10000000,1)",
              src])
        if not os.path.exists(src):
            return []

        if "tonemap_vaapi" in filters:
            out = _run(["ffmpeg", "-hide_banner", "-loglevel", "error",
                        "-vaapi_device", dri_device, "-i", src,
                        "-vf", "format=p010,hwupload,tonemap_vaapi=format=nv12",
                        "-c:v", "h264_vaapi", "-frames:v", "1", "-f", "null", "-"])
            if not _probe_failed(out):
                found.append("vaapi")

        if "tonemap_opencl" in filters:
            out = _run(["ffmpeg", "-hide_banner", "-loglevel", "error",
                        "-init_hw_device", f"vaapi=va:{dri_device}",
                        "-init_hw_device", "opencl=ocl@va", "-filter_hw_device", "ocl",
                        "-i", src,
                        "-vf", "format=p010,hwupload,"
                               "tonemap_opencl=tonemap=hable:desat=0:format=nv12,"
                               "hwmap=derive_device=vaapi:reverse=1",
                        "-c:v", "h264_vaapi", "-frames:v", "1", "-f", "null", "-"])
            if not _probe_failed(out):
                found.append("opencl")

    return found


def _probe_failed(out: str) -> bool:
    return any(m in out for m in ("Error", "Invalid", "Failed", "No mastering", "failed"))


def _software_backend(encoders: set[str]) -> dict:
    available = {c: e for c, e in _SW_ENCODERS.items() if e in encoders}
    return {
        "id": "software",
        "label": _BACKEND_LABELS["software"],
        "available": bool(available),
        "device": None,
        "driver": None,
        "encoders": available,
        "codecs": sorted({_canonical(c) for c in available}),
        "max_sessions": _DEFAULT_MAX_SESSIONS["software"],
        "reason": None if available else "no software encoders in this ffmpeg build",
    }


def _dri_backend(backend: str, encoders: set[str], hwaccels: set[str]) -> dict:
    """Build the record for a /dev/dri-based backend (qsv or vaapi)."""
    enc_map = {c: e for c, e in _HW_ENCODERS[backend].items() if e in encoders}
    entry = {
        "id": backend,
        "label": _BACKEND_LABELS[backend],
        "available": False,
        "device": None,
        "driver": None,
        "encoders": {},
        "codecs": [],
        "max_sessions": _DEFAULT_MAX_SESSIONS[backend],
        "reason": None,
    }

    if not enc_map:
        entry["reason"] = f"ffmpeg has no {backend} encoders compiled in"
        return entry
    if backend not in hwaccels:
        entry["reason"] = f"ffmpeg does not list {backend} as a hwaccel"
        return entry

    nodes = _render_nodes()
    if not nodes:
        entry["reason"] = "no /dev/dri render node (pass a GPU into the container)"
        return entry

    # Prefer the first render node whose driver reports an encode entrypoint.
    for node in nodes:
        profiles = _vainfo(node)
        if not profiles:
            continue
        driver_codecs = _encodable_codecs(profiles)  # canonical: h264/hevc/vp9/av1
        if not driver_codecs:
            continue
        # Keep encoders the driver can actually encode with ("h265" is an alias of "hevc").
        usable = {c: e for c, e in enc_map.items() if _canonical(c) in driver_codecs}
        if not usable:
            continue
        entry.update(
            available=True,
            device=node,
            driver=_va_driver_name(node),
            encoders=usable,
            codecs=sorted({_canonical(c) for c in usable}),
            reason=None,
        )
        return entry

    entry["device"] = nodes[0]
    entry["reason"] = "render node present but driver reports no encode entrypoint"
    return entry


def _nvenc_backend(encoders: set[str], hwaccels: set[str]) -> dict:
    enc_map = {c: e for c, e in _HW_ENCODERS["nvenc"].items() if e in encoders}
    entry = {
        "id": "nvenc",
        "label": _BACKEND_LABELS["nvenc"],
        "available": False,
        "device": None,
        "driver": None,
        "encoders": {},
        "codecs": [],
        "max_sessions": _DEFAULT_MAX_SESSIONS["nvenc"],
        "reason": None,
    }

    if not enc_map:
        entry["reason"] = "ffmpeg has no nvenc encoders compiled in"
        return entry

    devices = _nvidia_devices()
    if not devices:
        entry["reason"] = "no /dev/nvidia* device (needs nvidia-container-toolkit)"
        return entry

    driver = None
    out = _run(["nvidia-smi", "--query-gpu=name,driver_version", "--format=csv,noheader"])
    if out.strip():
        driver = out.strip().splitlines()[0]

    entry.update(
        available=True,
        device=devices[0],
        driver=driver,
        encoders=enc_map,
        codecs=sorted({_canonical(c) for c in enc_map}),
        reason=None,
    )
    return entry


def detect_capabilities(force: bool = False) -> dict:
    """
    Probe this node's transcoding capabilities.

    Cached after the first call — probing shells out to ffmpeg/vainfo and is far
    too slow to run per job. Pass force=True to re-probe (e.g. after a GPU is
    attached).
    """
    global _cache
    with _cache_lock:
        if _cache is not None and not force:
            return _cache

        encoders = _ffmpeg_encoders()
        hwaccels = _ffmpeg_hwaccels()

        backends = [
            _dri_backend("qsv", encoders, hwaccels),
            _dri_backend("vaapi", encoders, hwaccels),
            _nvenc_backend(encoders, hwaccels),
            _software_backend(encoders),
        ]

        # Tonemappers are node-level, not per-backend: they're filters, and they
        # all run on the same render node.
        dri = next((b["device"] for b in backends
                    if b["available"] and b["id"] in ("vaapi", "qsv") and b["device"]), None)
        tonemappers = _detect_tonemappers(_ffmpeg_filters(), dri)

        caps = {
            "node_id": _node_id(),
            "probed_at": time.time(),
            "backends": backends,
            "hardware_available": any(b["available"] and b["id"] != "software" for b in backends),
            "tonemappers": tonemappers,
        }
        logging.info("[CAPS] %s: GPU tonemappers: %s",
                     caps["node_id"], ", ".join(tonemappers) or "none (HDR tonemaps on CPU)")

        for b in backends:
            if b["available"]:
                logging.info("[CAPS] %s: %s available (device=%s, codecs=%s)",
                             caps["node_id"], b["id"], b["device"], ",".join(b["codecs"]))
            else:
                logging.debug("[CAPS] %s: %s unavailable — %s", caps["node_id"], b["id"], b["reason"])

        _cache = caps
        return caps


def get_backend(backend_id: str, force: bool = False) -> dict | None:
    """Return one backend's record, or None if unknown."""
    for b in detect_capabilities(force=force)["backends"]:
        if b["id"] == backend_id:
            return b
    return None


def available_backends(force: bool = False) -> list[str]:
    """Backend ids usable on this node, hardware first, software last."""
    return [b["id"] for b in detect_capabilities(force=force)["backends"] if b["available"]]
