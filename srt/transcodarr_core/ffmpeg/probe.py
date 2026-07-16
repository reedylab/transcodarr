from __future__ import annotations
import subprocess, os
import logging
import json

from ..config import Settings, get_setting

def ffprobe_json(path: str) -> dict:
    """
    Run ffprobe and return a JSON dict with 'streams' and 'format'.
    Returns {} on any error.
    """
    try:
        p = subprocess.run(
            [
                "ffprobe",
                "-v", "error",
                "-print_format", "json",
                "-show_streams",
                "-show_format",
                path,
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if p.returncode != 0 or not p.stdout.strip():
            return {}
        return json.loads(p.stdout)
    except Exception as e:
        logging.warning(f"ffprobe_json failed for {path}: {e}")
        return {}

def get_duration_seconds(file_path):
    """Get video duration in seconds using ffprobe."""
    # Try container-level duration first (fast, works for most formats)
    for entries in ("format=duration", "stream=duration"):
        try:
            result = subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries", entries,
                 "-of", "default=noprint_wrappers=1:nokey=1", file_path],
                capture_output=True, text=True
            )
            for line in result.stdout.strip().splitlines():
                val = line.strip()
                if val and val.lower() != "n/a":
                    return float(val)
        except Exception:
            continue
    return None

def detect_hdr(path: str) -> dict:
    """Detect HDR content and extract source video metadata from ffprobe."""
    info = ffprobe_json(path)
    for st in info.get("streams", []):
        if st.get("codec_type") == "video":
            color_transfer = st.get("color_transfer", "")
            color_primaries = st.get("color_primaries", "")
            pix_fmt = st.get("pix_fmt", "")

            # 10-bit pixel formats alone don't mean HDR — anime and some Blu-ray
            # rips use 10-bit SDR.  Require actual HDR transfer characteristics
            # or BT.2020 primaries before applying tone mapping.
            is_hdr = (
                color_transfer in ("smpte2084", "arib-std-b67")
                or color_primaries == "bt2020"
            )

            if is_hdr:
                logging.info(
                    "[HDR] Detected HDR source: transfer=%s primaries=%s pix_fmt=%s",
                    color_transfer, color_primaries, pix_fmt,
                )

            return {
                "is_hdr": is_hdr,
                "color_transfer": color_transfer,
                "color_primaries": color_primaries,
                "pix_fmt": pix_fmt,
                "height": int(st.get("height", 0)),
                "width": int(st.get("width", 0)),
            }

    return {
        "is_hdr": False,
        "color_transfer": "",
        "color_primaries": "",
        "pix_fmt": "",
        "height": 0,
        "width": 0,
    }


def has_mastering_display(path: str) -> bool:
    """
    Whether the source carries HDR10 mastering-display metadata.

    tonemap_vaapi hard-fails without it ("No mastering display data from input"),
    and plenty of HDR files lack it — HLG broadcasts and re-encodes that dropped
    the SEI. Gating on this keeps a GPU tonemap from killing the job. Reads only
    the first frame.
    """
    try:
        p = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_frames", "-read_intervals", "%+#1",
             "-show_entries", "frame_side_data=side_data_type",
             "-of", "default=nw=1", path],
            capture_output=True, text=True, timeout=20,
        )
        return "Mastering display metadata" in (p.stdout or "")
    except Exception as e:
        logging.debug("[PROBE] mastering-display check failed for %s: %s", path, e)
        return False


def file_needs_transcode(file_path):
    try:
        target_video_codec = get_setting("TARGET_VIDEO_CODEC", "h264")
        target_audio_codec = get_setting("TARGET_AUDIO_CODEC", "aac")
        target_container = get_setting("TARGET_CONTAINER", ".mp4")
        target_resolution = get_setting("TARGET_RESOLUTION", "1920x1080")
        video_mode = get_setting("VIDEO_STREAM_MODE", "encode")
        audio_mode = get_setting("AUDIO_STREAM_MODE", "encode")

        if video_mode != "copy":
            result = subprocess.run([
                "ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                "stream=codec_name,width,height", "-of", "default=noprint_wrappers=1", file_path
            ], capture_output=True, text=True)

            video_info = result.stdout
            if target_video_codec not in video_info:
                return True

            # Skip resolution check when set to "source" (match any resolution)
            if target_resolution.lower() == "1080p_max":
                # Only needs transcode if source is above 1080p
                for line in video_info.splitlines():
                    if line.startswith("height="):
                        src_h = int(line.split("=")[1])
                        if src_h > 1080:
                            return True
                        break
            elif target_resolution.lower() != "source":
                w, h = target_resolution.split("x")
                if f"width={w}" not in video_info or f"height={h}" not in video_info:
                    return True

        if audio_mode != "copy":
            result = subprocess.run([
                "ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries",
                "stream=codec_name", "-of", "default=noprint_wrappers=1", file_path
            ], capture_output=True, text=True)

            if target_audio_codec not in result.stdout:
                return True

        if not file_path.endswith(target_container):
            return True

        if not os.path.isfile(file_path) or os.path.getsize(file_path) < 100 * 1024 * 1024:
            logging.warning(f"Skipping (too small or not ready): {file_path}")
            return False

        return False

    except Exception as e:
        logging.warning(f"Error checking {file_path}: {e}")
        return False

