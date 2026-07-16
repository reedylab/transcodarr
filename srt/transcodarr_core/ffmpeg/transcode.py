# src/transcodarr_core/ffmpeg/transcode.py
from __future__ import annotations
import os, subprocess, logging, contextlib, json
from dataclasses import dataclass
from ..config import Settings, get_setting
from ..ffmpeg.probe import get_duration_seconds, ffprobe_json, detect_hdr
from ..ffmpeg.limits import backend_of_cmd, hw_slot
from ..subtitles.sanitize import sanitize_for_movtext

@dataclass
class Progress:
    percent: float
    seconds: float
    message: str

def format_progress_bar(percent, length=30):
    filled = int(length * percent)
    return "[" + "█" * filled + "░" * (length - filled) + "]"

def bytes_to_gb(bytes_val: int | float) -> float:
    return float(bytes_val) / (1024 ** 3)

# ───────────────────── codec dispatch ─────────────────────

_VIDEO_ENCODER = {
    "h264": "libx264",
    "h265": "libx265",
    "hevc": "libx265",
    "vp9":  "libvpx-vp9",
    "av1":  "libsvtav1",
}

# Hardware encoders per backend. Only codecs we can actually drive are listed —
# capability detection still has the final say per host (a GPU may ship the
# encoder but not the encode entrypoint, e.g. VP9 on Gen9.5).
_HW_VIDEO_ENCODER = {
    "qsv":   {"h264": "h264_qsv",   "h265": "hevc_qsv",   "hevc": "hevc_qsv"},
    "vaapi": {"h264": "h264_vaapi", "h265": "hevc_vaapi", "hevc": "hevc_vaapi"},
    "nvenc": {"h264": "h264_nvenc", "h265": "hevc_nvenc", "hevc": "hevc_nvenc"},
}

# Quality knob per backend. All are ~0-51 quantizer scales like x264's CRF, so the
# configured number carries over unscaled — but a hardware encoder at a given QP
# is generally less efficient than x264 at the same CRF, so expect to spend a few
# points to match software quality.
_HW_QUALITY_FLAG = {"qsv": "-global_quality", "vaapi": "-qp", "nvenc": "-cq"}

# x264-style preset names -> NVENC p-levels (p1 fastest .. p7 slowest).
_NVENC_PRESET_MAP = {
    "ultrafast": "p1", "superfast": "p1", "veryfast": "p2", "faster": "p3",
    "fast": "p4", "medium": "p4", "slow": "p5", "slower": "p6", "veryslow": "p7",
}

# QSV accepts x264-style preset names but has no ultrafast/superfast.
_QSV_PRESET_MAP = {"ultrafast": "veryfast", "superfast": "veryfast"}

_AUDIO_ENCODER = {
    "aac":  "aac",
    "ac3":  "ac3",
    "eac3": "eac3",
    "flac": "flac",
    "opus": "libopus",
}

# SVT-AV1 uses numeric presets (0=slowest/best, 13=fastest). Map our x264-style names.
_SVTAV1_PRESET_MAP = {
    "ultrafast": "12", "superfast": "11", "veryfast": "10", "faster": "9",
    "fast": "8", "medium": "6", "slow": "4", "slower": "3", "veryslow": "2",
}

# libvpx-vp9 uses -cpu-used (0=slowest, 5=fastest).
_VP9_CPU_USED_MAP = {
    "ultrafast": "5", "superfast": "5", "veryfast": "4", "faster": "4",
    "fast": "3", "medium": "2", "slow": "1", "slower": "0", "veryslow": "0",
}


def _video_encoder_args(codec: str, preset: str, profile: str, crf: str, threads: str) -> list[str]:
    """Build per-codec video encoder args. Silently drops params that don't apply."""
    codec = (codec or "h264").lower()
    encoder = _VIDEO_ENCODER.get(codec, "libx264")
    args: list[str] = ["-c:v", encoder]

    if codec == "h264":
        if threads:
            args += ["-x264-params", f"threads={threads}"]
        if preset:
            args += ["-preset", preset]
        if profile:
            args += ["-profile:v", profile]
        if crf:
            args += ["-crf", crf]
    elif codec in ("h265", "hevc"):
        if threads:
            args += ["-x265-params", f"pools={threads}"]
        if preset:
            args += ["-preset", preset]
        if crf:
            args += ["-crf", crf]
    elif codec == "vp9":
        cpu_used = _VP9_CPU_USED_MAP.get((preset or "fast").lower(), "2")
        if threads:
            args += ["-threads", threads]
        if crf:
            args += ["-b:v", "0", "-crf", crf]
        args += ["-cpu-used", cpu_used, "-row-mt", "1"]
    elif codec == "av1":
        svt_preset = _SVTAV1_PRESET_MAP.get((preset or "fast").lower(), "8")
        args += ["-preset", svt_preset]
        if crf:
            args += ["-crf", crf, "-b:v", "0"]
        if threads:
            args += ["-svtav1-params", f"lp={threads}"]
    else:
        if preset:
            args += ["-preset", preset]
        if crf:
            args += ["-crf", crf]

    return args


def _resolve_backend(requested: str | None, codec: str, hdr_action: str,
                     file_path: str) -> tuple[str, str | None]:
    """Pick the encode backend, returning (backend, device).

    Always degrades to software rather than failing a job: an unavailable GPU,
    an unsupported codec, or a failed probe costs speed, not the transcode.
    """
    backend = (requested or "software").lower()
    if backend in ("", "software", "sw", "none"):
        return "software", None

    name = os.path.basename(file_path)

    # HDR stays on software until the hardware tonemap path lands: the tonemap
    # chain is software (zscale) and passthrough needs 10-bit hardware surfaces.
    if hdr_action in ("tonemap", "passthrough"):
        logging.info("[HW] %s: HDR (%s) not supported on %s yet — using software",
                     name, hdr_action, backend)
        return "software", None

    if backend not in _HW_VIDEO_ENCODER:
        logging.warning("[HW] unknown backend %r — using software", backend)
        return "software", None
    if codec not in _HW_VIDEO_ENCODER[backend]:
        logging.info("[HW] %s: %s has no %s encoder — using software", name, backend, codec)
        return "software", None

    try:
        from .capabilities import get_backend
        info = get_backend(backend)
    except Exception as e:
        logging.warning("[HW] capability probe failed (%s) — using software", e)
        return "software", None

    if not info or not info.get("available"):
        logging.warning("[HW] %s unavailable (%s) — using software",
                        backend, (info or {}).get("reason"))
        return "software", None
    if codec not in info.get("encoders", {}):
        logging.warning("[HW] %s cannot encode %s on this host — using software", backend, codec)
        return "software", None

    return backend, info.get("device")


def _hw_device_args(backend: str, device: str | None) -> list[str]:
    """Hardware device init. Must precede -i, and is empty for software."""
    if backend == "vaapi" and device:
        return ["-vaapi_device", device]
    if backend == "qsv":
        if device:
            # Pin QSV to a specific render node via a VAAPI parent — a bare
            # "qsv=hw" grabs an arbitrary GPU on multi-GPU hosts.
            return ["-init_hw_device", f"vaapi=va:{device}",
                    "-init_hw_device", "qsv=hw@va", "-filter_hw_device", "hw"]
        return ["-init_hw_device", "qsv=hw", "-filter_hw_device", "hw"]
    return []


def _hw_upload_filters(backend: str) -> list[str]:
    """Filters that move frames onto the GPU. NVENC takes software frames directly."""
    if backend == "vaapi":
        return ["format=nv12", "hwupload"]
    if backend == "qsv":
        return ["format=nv12", "hwupload=extra_hw_frames=64"]
    return []


def _hw_video_encoder_args(codec: str, backend: str, preset: str, profile: str,
                           quality: str) -> list[str]:
    """Build hardware video encoder args, mirroring _video_encoder_args' shape."""
    encoder = _HW_VIDEO_ENCODER[backend][codec]
    args: list[str] = ["-c:v", encoder]

    if preset:
        if backend == "qsv":
            args += ["-preset", _QSV_PRESET_MAP.get(preset.lower(), preset)]
        elif backend == "nvenc":
            args += ["-preset", _NVENC_PRESET_MAP.get(preset.lower(), "p4")]
        # VAAPI has no preset knob (it exposes -compression_level instead).

    # Match the software path: profile is only meaningful for h264 here.
    if profile and codec == "h264":
        args += ["-profile:v", profile]

    if quality:
        args += [_HW_QUALITY_FLAG[backend], quality]

    return args


def _audio_encoder_args(codec: str, bitrate: str, channels: str) -> list[str]:
    """Build per-codec audio encoder args. FLAC is lossless so bitrate is skipped."""
    codec = (codec or "aac").lower()
    encoder = _AUDIO_ENCODER.get(codec, "aac")
    args: list[str] = ["-c:a", encoder]
    if channels:
        args += ["-ac", channels]
    if codec == "flac":
        args += ["-compression_level", "5"]
    elif bitrate:
        args += ["-b:a", bitrate]
    return args


def _resolve_hdr_action(hdr_mode: str, video_codec: str) -> str:
    """Return 'tonemap', 'passthrough', or 'none'.

    Auto mode tonemaps only for 8-bit-only codecs (h264) to prevent the
    classic HDR-on-SDR-pipeline grey-washout bug. For AV1/HEVC/VP9 we keep
    HDR so the player can render BT.2020+PQ properly.
    """
    mode = (hdr_mode or "auto").lower()
    codec = (video_codec or "h264").lower()
    if mode == "tonemap":
        return "tonemap"
    if mode == "passthrough":
        return "passthrough"
    return "tonemap" if codec == "h264" else "passthrough"

def build_ffmpeg_cmd(file_path: str, srt_path: str, out_temp: str, settings=None,
                     settings_override: dict | None = None,
                     backend: str | None = None) -> list[str]:
    """Assemble the ffmpeg command.

    `backend` selects the video encode backend ("software", "qsv", "vaapi",
    "nvenc"); None falls back to the HW_BACKEND setting. Anything hardware is
    validated against this node's detected capabilities and silently degrades to
    software, so callers can ask for hardware without handling absence.
    """
    def _get(key, default):
        if settings_override and key in settings_override:
            return settings_override[key]
        return get_setting(key, default)

    ffmpeg_threads = _get("FFMPEG_THREADS", "1")
    # ENCODER_THREADS supersedes the legacy X264_THREADS. For override dicts
    # we check the new key first and only fall back if it's missing, so an
    # unmigrated preset JSON still supplies a thread count.
    override = settings_override or {}
    if "ENCODER_THREADS" in override:
        encoder_threads = override["ENCODER_THREADS"]
    elif "X264_THREADS" in override:
        encoder_threads = override["X264_THREADS"]
    else:
        encoder_threads = get_setting("ENCODER_THREADS", "4")

    # Read encoding settings (override -> DB -> env -> defaults)
    video_codec = _get("TARGET_VIDEO_CODEC", "h264")
    audio_codec = _get("TARGET_AUDIO_CODEC", "aac")
    resolution = _get("TARGET_RESOLUTION", "1920x1080")
    preset = _get("TARGET_PRESET", "fast")
    profile = _get("TARGET_PROFILE", "high")
    audio_bitrate = _get("TARGET_AUDIO_BITRATE", "448k")
    audio_channels = _get("TARGET_AUDIO_CHANNELS", "6")
    crf = _get("TARGET_CRF", "")
    normalize = _get("TARGET_AUDIO_NORMALIZE", "true")
    hdr_mode = _get("TARGET_HDR_MODE", "auto")
    video_mode = _get("VIDEO_STREAM_MODE", "encode")
    audio_mode = _get("AUDIO_STREAM_MODE", "encode")

    # Sanitize the SRT for mov_text robustness (if provided)
    srt_safe = sanitize_for_movtext(srt_path) if srt_path else None

    # Probe and pick the backend before assembling the command: hardware device
    # init has to sit ahead of -i. Only relevant when we're actually encoding.
    hdr_info: dict | None = None
    hdr_action = "none"
    enc_backend = "software"
    hw_device: str | None = None
    if video_mode != "copy":
        hdr_info = detect_hdr(file_path)
        hdr_action = _resolve_hdr_action(hdr_mode, video_codec) if hdr_info["is_hdr"] else "none"
        requested = backend if backend is not None else _get("HW_BACKEND", "software")
        enc_backend, hw_device = _resolve_backend(
            requested, (video_codec or "h264").lower(), hdr_action, file_path
        )

    cmd = [
        "ffmpeg", "-y", "-y", "-threads", ffmpeg_threads,
        "-progress", "pipe:1", "-nostats",
    ]
    cmd += _hw_device_args(enc_backend, hw_device)
    cmd += ["-i", file_path]
    if srt_safe:
        cmd += ["-sub_charenc", "UTF-8", "-i", srt_safe]
    cmd += [
        "-map", "0:v:0",
        "-map", "0:a:0?",
    ]
    if srt_safe:
        cmd += ["-map", "1:0"]
    if video_mode == "copy":
        cmd += ["-c:v", "copy"]
    else:
        # hdr_info / hdr_action were resolved above (before the device args).

        # Build composable video filter chain
        vf_filters: list[str] = []

        # 1. HDR → SDR tone mapping (must come before scaling)
        if hdr_action == "tonemap":
            logging.info("[HDR] Tonemapping HDR→SDR for %s", os.path.basename(file_path))
            vf_filters += [
                "zscale=t=linear:npl=100",
                "format=gbrpf32le",
                "zscale=p=bt709",
                "tonemap=hable:desat=0",
                "zscale=t=bt709:m=bt709:r=tv",
                "format=yuv420p",
            ]
        elif hdr_action == "passthrough":
            logging.info("[HDR] Passthrough HDR for %s (codec=%s)",
                         os.path.basename(file_path), video_codec)

        # 2. Resolution scaling (aspect-ratio-preserving)
        if resolution and resolution.lower() == "1080p_max":
            src_h = hdr_info["height"]
            if src_h > 1080 or src_h == 0:
                # src_h == 0 means probe failed; assume downscale needed
                vf_filters.append("scale=-2:1080")
        elif resolution and resolution.lower() != "source":
            w, h = resolution.split("x")
            vf_filters.append(f"scale={w}:{h}")

        # VAAPI/QSV need frames uploaded to the GPU as the last filter step.
        # Scaling stays on the CPU for now — the aspect-preserving scale=-2:1080
        # behaviour is shared with the software path, and encode is the expensive
        # part we're offloading. A full GPU pipeline (hwaccel decode +
        # scale_vaapi) is a later optimisation.
        upload_filters = _hw_upload_filters(enc_backend)
        vf_all = vf_filters + upload_filters
        if vf_all:
            cmd += ["-vf", ",".join(vf_all)]

        # Pixel format: tonemap chain already ends with format=yuv420p.
        # HDR passthrough needs 10-bit. SDR encode gets standard 8-bit yuv420p.
        # Skipped when uploading to the GPU — the hwupload chain sets the format
        # and an explicit -pix_fmt fights the hardware frames context.
        if not upload_filters:
            if hdr_action == "passthrough":
                cmd += ["-pix_fmt", "yuv420p10le"]
            elif hdr_action == "none":
                cmd += ["-pix_fmt", "yuv420p"]

        if enc_backend == "software":
            cmd += _video_encoder_args(video_codec, preset, profile, crf, encoder_threads)
        else:
            cmd += _hw_video_encoder_args((video_codec or "h264").lower(), enc_backend,
                                          preset, profile, crf)

        # Preserve source color metadata on HDR passthrough so players render correctly.
        if hdr_action == "passthrough":
            color_primaries = hdr_info.get("color_primaries") or "bt2020"
            color_transfer = hdr_info.get("color_transfer") or "smpte2084"
            cmd += [
                "-color_primaries", color_primaries,
                "-color_trc", color_transfer,
                "-colorspace", "bt2020nc",
            ]

    if audio_mode == "copy":
        cmd += ["-c:a", "copy"]
    else:
        # Audio normalization
        if normalize.lower() != "false":
            cmd += ["-af", "loudnorm=I=-14:TP=-1:LRA=11"]
        cmd += _audio_encoder_args(audio_codec, audio_bitrate, audio_channels)
    # Decide container-sensitive flags off the actual output extension, not the
    # user setting — the fallback path writes to a forced .mp4 temp regardless.
    out_ext = os.path.splitext(out_temp)[1].lower()
    if srt_safe:
        # mov_text is MP4-only. MKV uses srt natively.
        sub_codec = "mov_text" if out_ext == ".mp4" else "srt"
        cmd += ["-c:s", sub_codec, "-metadata:s:s:0", "language=eng"]
    cmd += [
        "-map_metadata", "-1",
        "-map_chapters", "-1",
    ]
    # +faststart is MP4-specific (moves moov atom to the front).
    if out_ext == ".mp4":
        cmd += ["-movflags", "+faststart"]
    cmd += [
        "-fflags", "+genpts",
        "-max_muxing_queue_size", "4096",
        "-max_interleave_delta", "0",  # helps with odd interleaving
        "-avoid_negative_ts", "make_zero",  # safer timestamps
        out_temp
    ]

    return cmd

def build_sub_copy_mux_cmd(video_no_subs: str, srt_path: str, out_with_subs: str) -> list[str]:
    ffmpeg_threads = get_setting("FFMPEG_THREADS", "1")
    # Always sanitize for mov_text
    srt_safe = sanitize_for_movtext(srt_path)
    return [
        "ffmpeg","-y", "-threads", ffmpeg_threads,"-loglevel","error",
        "-i", video_no_subs,
        "-sub_charenc","UTF-8","-i", srt_safe,
        "-map","0:v:0","-map","0:a:0?","-map","1:0",
        "-c","copy","-c:s","mov_text","-metadata:s:s:0","language=eng",
        "-map_metadata","-1","-map_chapters","-1","-movflags","+faststart",
        "-max_muxing_queue_size","4096",
        "-max_interleave_delta", "0",  # helps with odd interleaving
        "-avoid_negative_ts", "make_zero",  # safer timestamps
        out_with_subs
    ]

def _probe_subs(path: str) -> tuple[int, list[str]]:
    try:
        cmd = [
            "ffprobe","-v","error","-select_streams","s",
            "-show_entries","stream=codec_name:stream_tags=language",
            "-of","json", path
        ]
        p = subprocess.run(cmd, capture_output=True, text=True)
        if p.returncode != 0:
            return 0, []
        data = json.loads(p.stdout or "{}")
        streams = data.get("streams", []) or []
        langs = []
        for s in streams:
            t = s.get("tags", {}) or {}
            if s.get("codec_name") == "mov_text":
                langs.append((t.get("language") or "").lower() or "und")
        return len(streams), langs
    except Exception:
        return 0, []

def run_ffmpeg_with_progress(cmd: list[str], total_duration: float | None, progress_file: str | None = None, source_path: str = "", register_path: str = ""):
    from ..worker_pool import register_proc, unregister_proc

    reg_key = register_path or source_path
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        bufsize=1, universal_newlines=True
    )
    register_proc(proc, reg_key)
    recent: list[str] = []
    last_progress_write = 0.0
    try:
        for line in proc.stdout:  # type: ignore[arg-type]
            line = (line or "").strip()
            if len(recent) > 200:
                recent.pop(0)
            recent.append(line)

            if total_duration and line.startswith("out_time_ms"):
                try:
                    elapsed_sec = int(line.split("=")[1]) / 1_000_000
                except Exception:
                    elapsed_sec = None
                if elapsed_sec is not None and total_duration > 0:
                    pct = max(0.0, min(1.0, elapsed_sec / total_duration))
                    # Write progress to file periodically (every 1%)
                    if progress_file and (pct - last_progress_write) >= 0.01:
                        try:
                            _update_progress_file(progress_file, pct)
                            last_progress_write = pct
                        except Exception:
                            pass
                    yield Progress(percent=pct, seconds=elapsed_sec,
                                   message=f"{pct*100:6.2f}% ({elapsed_sec:.1f}s)")
            else:
                logging.debug(line)
        rc = proc.wait()
        if rc != 0:
            tail = "\n".join(recent[-30:])
            raise RuntimeError(f"ffmpeg exited with {rc}\n--- ffmpeg tail ---\n{tail}\n--- end tail ---")
    finally:
        unregister_proc(proc, reg_key)
        with contextlib.suppress(Exception):
            proc.kill()


def _update_progress_file(progress_file: str, percent: float):
    """Update just the progress field in an existing progress file."""
    import time
    try:
        with open(progress_file, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        data = {}
    data["progress"] = round(percent * 100, 1)
    data["updated_at"] = time.time()
    with open(progress_file, "w", encoding="utf-8") as f:
        json.dump(data, f)

def run_ffmpeg(file_path: str, srt_path: str, out_path: str, base_name: str, s: Settings, progress_file: str | None = None, register_path: str = "", settings_override: dict | None = None) -> None:
    total_duration = get_duration_seconds(file_path) or 0.0
    total_size_gb = bytes_to_gb(os.path.getsize(file_path))

    def _log_progress(cmd: list[str]):
        # Announce what's actually driving the encode. Read from the built command,
        # not the requested backend — a hardware request may have degraded to
        # software, and the logs should show what really ran.
        encoder = cmd[cmd.index("-c:v") + 1] if "-c:v" in cmd else "?"
        backend = backend_of_cmd(cmd)
        if backend == "software":
            tag = "SW"
            logging.info("[ENCODE] %s — software (%s)", base_name, encoder)
        else:
            tag = backend.upper()
            device = cmd[cmd.index("-vaapi_device") + 1] if "-vaapi_device" in cmd else None
            logging.info("[ENCODE] %s — HARDWARE %s (%s)%s", base_name, tag, encoder,
                         f" on {device}" if device else "")

        last_logged = -1.0
        for prog in run_ffmpeg_with_progress(cmd, total_duration, progress_file, source_path=file_path, register_path=register_path):
            if abs(prog.percent - last_logged) >= 0.0001 and prog.percent <= 1.0:
                bar = format_progress_bar(prog.percent)
                logging.info(
                    f"[TRANSCODING] [{tag}] {base_name} {bar} {prog.percent * 100:6.2f}%  "
                    f"[{total_size_gb * prog.percent:.2f} GB / {total_size_gb:.2f} GB]"
                )
                last_logged = prog.percent

    # 1) Combined encode (v+a+s)
    try:
        logging.info(f"[SUBS] Trying standard trancode with subs.")
        cmd = build_ffmpeg_cmd(file_path, srt_path, out_path, s, settings_override=settings_override)
        # Hardware commands hold a global slot so the auto and manual pools can't
        # jointly exceed the GPU's session ceiling; software passes straight through.
        with hw_slot(cmd):
            _log_progress(cmd)
        if not os.path.exists(out_path):
            raise RuntimeError(f"Expected output not found: {out_path}")
        return
    except Exception as e:
        logging.error(f"[FFMPEG] Combined encode failed: {e}")

    # 2) Fallback: transcode video+audio without subs, then mux subs separately
    tmp_no_subs = out_path + ".nosubs.mp4"
    tmp_with_subs = out_path + ".withsubs.mp4"

    try:
        logging.info("[SUBS] Trying fallback: transcode without subs, then mux subs.")
        cmd_no_subs = build_ffmpeg_cmd(file_path, None, tmp_no_subs, s, settings_override=settings_override)
        with hw_slot(cmd_no_subs):
            _log_progress(cmd_no_subs)
        if not os.path.exists(tmp_no_subs):
            raise RuntimeError(f"Fallback transcode produced no output: {tmp_no_subs}")
    except Exception as e:
        logging.error(f"[FFMPEG] Fallback transcode (no subs) also failed: {e}")
        raise

    # 3) Mux sanitized subs into that video (skip if no subs provided)
    if not srt_path:
        os.replace(tmp_no_subs, out_path)
        return

    try:
        logging.info("[SUBS] Muxing subs into fallback transcode output.")
        cmd = build_sub_copy_mux_cmd(tmp_no_subs, srt_path, tmp_with_subs)
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            raise RuntimeError(f"Sub copy-mux failed (rc={proc.returncode}): {proc.stderr or proc.stdout}")
        cnt, langs = _probe_subs(tmp_with_subs)
        logging.info(f"[SUBS] muxed subtitle tracks: {cnt} (langs={langs})")
        with contextlib.suppress(Exception):
            if os.path.exists(out_path):
                os.remove(out_path)
        os.replace(tmp_with_subs, out_path)
    finally:
        with contextlib.suppress(Exception):
            if os.path.exists(tmp_no_subs):
                os.remove(tmp_no_subs)
        with contextlib.suppress(Exception):
            if os.path.exists(tmp_with_subs):
                os.remove(tmp_with_subs)