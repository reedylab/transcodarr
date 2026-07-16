# web/shared_state.py
# Module-level state, constants, and helper functions extracted from api.py
from urllib.parse import quote
from threading import Thread
from pathlib import Path
from transcodarr_core import start_watchdog, get_duration_seconds
from transcodarr_core.posters import ensure_poster
from transcodarr_core.database import (
    init_database, get_transcode_history, get_all_transcode_history,
    upsert_movie, get_movie, get_all_movies,
    upsert_tv_episode, get_tv_episode, get_all_tv_episodes,
    get_media_metadata,
    set_ignored, remove_ignored, is_ignored, get_all_ignored, get_ignored_paths,
    get_setting, set_setting, get_all_settings, bulk_set_settings,
    insert_storage_snapshot, get_storage_history, prune_storage_history,
)
from transcodarr_core.metadata import fetch_movie_metadata, fetch_series_metadata
from transcodarr_core.enrich import enrich_media
from dotenv import dotenv_values
import os, re, json, time, math, subprocess, logging, collections
import psutil
from contextlib import suppress
import threading as _threading


# ----------------------- module-level state -----------------------
_state = {"running": False, "thread": None}

# ----------------------- system stats collector -----------------------
_stats_lock = _threading.Lock()
_cpu_history = collections.deque(maxlen=2880)       # 24h at 30s intervals
_ram_history = collections.deque(maxlen=2880)
_stats_timestamps = collections.deque(maxlen=2880)
_collector_started = False


def _stats_collector():
    """Background daemon: sample CPU/RAM every ~30s, disk every ~5 min."""
    tick = 0
    while True:
        now = time.time()
        cpu = psutil.cpu_percent(interval=1)   # 1s blocking sample for accuracy
        mem = psutil.virtual_memory()

        with _stats_lock:
            _stats_timestamps.append(now)
            _cpu_history.append(cpu)
            _ram_history.append(mem.percent)

        # Every 10 ticks (~5 min) record disk snapshot to DB
        if tick % 10 == 0:
            try:
                from transcodarr_core.config import Settings
                output = Settings().OUTPUT_FOLDER
                if output:
                    disk = psutil.disk_usage(output)
                    insert_storage_snapshot(disk.total, disk.used, disk.free)
                    prune_storage_history(keep_days=90)
            except Exception:
                pass

        tick += 1
        time.sleep(29)  # ~30s total with the 1s cpu sample


def start_stats_collector():
    """Start the stats collector daemon thread (idempotent)."""
    global _collector_started
    if _collector_started:
        return
    _collector_started = True
    t = Thread(target=_stats_collector, daemon=True)
    t.start()
    logging.info("[STATS] System stats collector started")


# ----------------------- run lock helpers -----------------------
def acquire_run_lock(path: str):
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    with os.fdopen(fd, "w") as f:
        f.write(str(os.getpid()))

def release_run_lock(path: str):
    with suppress(FileNotFoundError):
        os.remove(path)

def is_running_lock(path: str) -> bool:
    return os.path.exists(path)


# ----------------------- watchdog bg thread -----------------------
def _bg(settings, stop_flag_fn, set_stop_flag_fn, lock_path, debounce_sec: float):
    try:
        _state["running"] = True
        set_stop_flag_fn(False)

        # Check AUTO_WORKERS — if 0, don't run watchdog, just idle
        from transcodarr_core.config import get_setting as config_get_setting
        try:
            auto_workers = int(config_get_setting("AUTO_WORKERS", settings.AUTO_WORKERS))
        except (ValueError, TypeError):
            auto_workers = settings.AUTO_WORKERS

        if auto_workers > 0:
            start_watchdog(
                settings=settings,
                stop_flag_fn=stop_flag_fn,
                debounce_sec=debounce_sec,
            )
        else:
            logging.info("[BG] AUTO_WORKERS=0, watchdog disabled. Idling...")
            while not stop_flag_fn():
                time.sleep(1)
    finally:
        _state["running"] = False
        release_run_lock(lock_path)


# ----------------------- settings schema -----------------------
SETTINGS_SCHEMA = {
    "encoding": {
        "label": "Encoding",
        "fields": {
            "VIDEO_STREAM_MODE": {"label": "Video Stream", "type": "select", "group": "video", "options": [
                {"value": "encode", "label": "Encode (transcode)"},
                {"value": "copy", "label": "Copy (passthrough)"},
            ]},
            "AUDIO_STREAM_MODE": {"label": "Audio Stream", "type": "select", "group": "audio", "options": [
                {"value": "encode", "label": "Encode (transcode)"},
                {"value": "copy", "label": "Copy (passthrough)"},
            ]},
            "TARGET_VIDEO_CODEC": {"label": "Video Codec", "type": "select", "group": "video", "options": [
                {"value": "h264", "label": "H.264 / AVC"},
                {"value": "h265", "label": "H.265 / HEVC"},
                {"value": "vp9", "label": "VP9"},
                {"value": "av1", "label": "AV1"},
            ]},
            "TARGET_AUDIO_CODEC": {"label": "Audio Codec", "type": "select", "group": "audio", "options": [
                {"value": "aac", "label": "AAC"},
                {"value": "ac3", "label": "AC3 (Dolby Digital)"},
                {"value": "eac3", "label": "EAC3 (Dolby Digital Plus)"},
                {"value": "flac", "label": "FLAC"},
                {"value": "opus", "label": "Opus"},
            ]},
            "TARGET_CONTAINER": {"label": "Container", "type": "select", "group": "video", "options": [
                {"value": ".mp4", "label": "MP4 (.mp4)"},
                {"value": ".mkv", "label": "Matroska (.mkv)"},
                {"value": ".webm", "label": "WebM (.webm)"},
            ]},
            "TARGET_RESOLUTION": {"label": "Resolution", "type": "select", "group": "video", "options": [
                {"value": "source", "label": "Match Source"},
                {"value": "1280x720", "label": "720p (1280x720)"},
                {"value": "1920x1080", "label": "1080p (1920x1080)"},
                {"value": "1080p_max", "label": "1080p Max (no upscale)"},
                {"value": "2560x1440", "label": "1440p (2560x1440)"},
                {"value": "3840x2160", "label": "4K (3840x2160)"},
            ]},
            "TARGET_PRESET": {"label": "Preset", "show_if": {"HW_BACKEND": ["", "software", "qsv", "nvenc"]}, "type": "select", "group": "video", "options": [
                {"value": "ultrafast", "label": "Ultrafast"},
                {"value": "superfast", "label": "Superfast"},
                {"value": "veryfast", "label": "Veryfast"},
                {"value": "faster", "label": "Faster"},
                {"value": "fast", "label": "Fast"},
                {"value": "medium", "label": "Medium"},
                {"value": "slow", "label": "Slow"},
                {"value": "slower", "label": "Slower"},
                {"value": "veryslow", "label": "Veryslow"},
            ]},
            "TARGET_PROFILE": {"label": "Profile", "type": "select", "group": "video", "options": [
                {"value": "baseline", "label": "Baseline"},
                {"value": "main", "label": "Main"},
                {"value": "high", "label": "High"},
            ]},
            "TARGET_AUDIO_BITRATE": {"label": "Audio Bitrate", "type": "select", "group": "audio", "options": [
                {"value": "128k", "label": "128k"},
                {"value": "192k", "label": "192k"},
                {"value": "256k", "label": "256k"},
                {"value": "320k", "label": "320k"},
                {"value": "448k", "label": "448k"},
            ]},
            "TARGET_AUDIO_CHANNELS": {"label": "Audio Channels", "type": "select", "group": "audio", "options": [
                {"value": "2", "label": "2 (Stereo)"},
                {"value": "6", "label": "6 (5.1)"},
                {"value": "8", "label": "8 (7.1)"},
            ]},
            "TARGET_CRF": {"label": "Quality (CRF / QP)", "hint": "Lower = better. Software uses CRF; hardware maps it to global_quality (QSV), qp (VAAPI) or cq (NVENC). Leave blank ONLY on software — hardware reads blank as no quality target and drops to a poor default bitrate.", "type": "select", "group": "video", "options": [
                {"value": "", "label": "Default (codec decides)"},
                {"value": "18", "label": "18 (Visually Lossless)"},
                {"value": "20", "label": "20"},
                {"value": "23", "label": "23 (default)"},
                {"value": "26", "label": "26"},
                {"value": "28", "label": "28"},
                {"value": "30", "label": "30"},
            ]},
            "TARGET_AUDIO_NORMALIZE": {"label": "Audio Normalization", "type": "select", "group": "audio", "options": [
                {"value": "true", "label": "Enabled"},
                {"value": "false", "label": "Disabled"},
            ]},
            "TARGET_HDR_MODE": {"label": "HDR Handling", "type": "select", "group": "video", "options": [
                {"value": "auto", "label": "Auto (tonemap for 8-bit codecs, passthrough for AV1/HEVC)"},
                {"value": "tonemap", "label": "Tonemap to SDR (always)"},
                {"value": "passthrough", "label": "Passthrough HDR (always)"},
            ]},
            # Option labels get annotated with what this host actually detected —
            # see _schema_with_hw_availability() in routers/settings.py.
            "HW_BACKEND": {"label": "Encode Backend", "type": "select", "group": "video",
                           "hint": "Hardware is far faster; software gives the best quality per bitrate. Unavailable backends fall back to software automatically.",
                           "options": [
                {"value": "software", "label": "Software (libx264/x265)"},
                {"value": "qsv", "label": "Intel Quick Sync (QSV)"},
                {"value": "vaapi", "label": "VA-API (Intel/AMD)"},
                {"value": "nvenc", "label": "NVIDIA NVENC"},
            ]},
            "TARGET_TONEMAP": {"label": "HDR Tonemapping", "type": "select", "group": "video",
                               "show_if": {"HW_BACKEND": ["qsv", "vaapi", "nvenc"]},
                               "hint": "Where HDR->SDR conversion runs. GPU is much faster; software gives the best picture. Falls back to software automatically when the GPU can't do it.",
                               "options": [
                {"value": "auto", "label": "Auto (GPU if available, else software)"},
                {"value": "software", "label": "Software (zscale + hable) — best quality"},
                {"value": "vaapi", "label": "GPU: VA-API"},
                {"value": "opencl", "label": "GPU: OpenCL"},
            ]},
            "REQUIRE_SUBTITLES": {"label": "Require Subtitles", "type": "select", "group": "audio", "options": [
                {"value": "true", "label": "Required (skip if unavailable)"},
                {"value": "false", "label": "Optional (transcode without if unavailable)"},
            ]},
            "FFMPEG_THREADS": {"label": "FFmpeg Threads", "type": "select", "group": "advanced", "options": [
                {"value": "1", "label": "1"},
                {"value": "2", "label": "2"},
                {"value": "4", "label": "4"},
                {"value": "8", "label": "8"},
                {"value": "0", "label": "Auto (all cores)"},
            ]},
            "ENCODER_THREADS": {"label": "Encoder Threads", "show_if": {"HW_BACKEND": ["", "software"]}, "type": "select", "group": "advanced", "options": [
                {"value": "1", "label": "1"},
                {"value": "2", "label": "2"},
                {"value": "4", "label": "4"},
                {"value": "8", "label": "8"},
                {"value": "0", "label": "Auto (all cores)"},
            ]},
        }
    },
    "integrations": {
        "label": "Integrations",
        "type": "integrations",
        "fields": {
            "RADARR_URL": {"label": "URL", "type": "text", "placeholder": "http://localhost:7878"},
            "RADARR_API_KEY": {"label": "API Key", "type": "password", "placeholder": ""},
            "RADARR_PATH_FROM": {"label": "Path Override (Radarr sees)", "type": "text", "placeholder": "", "hint": "Optional — auto-detected if blank. Only set if auto-detection fails."},
            "RADARR_PATH_TO": {"label": "Path Override (Transcodarr sees)", "type": "text", "placeholder": "", "hint": "Optional — auto-detected if blank. Only set if auto-detection fails."},
            "SONARR_URL": {"label": "URL", "type": "text", "placeholder": "http://localhost:8989"},
            "SONARR_API_KEY": {"label": "API Key", "type": "password", "placeholder": ""},
            "SONARR_PATH_FROM": {"label": "Path Override (Sonarr sees)", "type": "text", "placeholder": "", "hint": "Optional — auto-detected if blank. Only set if auto-detection fails."},
            "SONARR_PATH_TO": {"label": "Path Override (Transcodarr sees)", "type": "text", "placeholder": "", "hint": "Optional — auto-detected if blank. Only set if auto-detection fails."},
            "JELLYFIN_URL": {"label": "URL", "type": "text", "placeholder": "http://localhost:8096"},
            "JELLYFIN_API_KEY": {"label": "API Key", "type": "password", "placeholder": ""},
            "TVDB_API_KEY": {"label": "API Key", "type": "password", "placeholder": ""},
            "TMDB_API_KEY": {"label": "API Key", "type": "password", "placeholder": ""},
            "OMDB_API_KEY": {"label": "API Key", "type": "password", "placeholder": ""},
        },
        "cards": {
            "radarr":   {"label": "Radarr",   "desc": "Movie management",   "has_webhook": True,  "fields": ["RADARR_URL", "RADARR_API_KEY", "RADARR_PATH_FROM", "RADARR_PATH_TO"]},
            "sonarr":   {"label": "Sonarr",   "desc": "TV management",      "has_webhook": True,  "fields": ["SONARR_URL", "SONARR_API_KEY", "SONARR_PATH_FROM", "SONARR_PATH_TO"]},
            "jellyfin": {"label": "Jellyfin", "desc": "Media server",       "has_webhook": False, "fields": ["JELLYFIN_URL", "JELLYFIN_API_KEY"]},
            "tvdb":     {"label": "TVDB",     "desc": "TV metadata",        "has_webhook": False, "fields": ["TVDB_API_KEY"]},
            "tmdb":     {"label": "TMDB",     "desc": "Movie metadata",     "has_webhook": False, "fields": ["TMDB_API_KEY"]},
            "omdb":     {"label": "OMDB",     "desc": "Movie metadata",     "has_webhook": False, "fields": ["OMDB_API_KEY"]},
        }
    },
    "subtitles": {
        "label": "Subtitles",
        "type": "subtitle_providers",
        "fields": {
            "FFSUBSYNC_MAX_OFFSET": {"label": "Max Sync Offset", "type": "text", "placeholder": "0.5"},
        }
    },
    "general": {
        "label": "General",
        "type": "general_grouped",
        "fields": {
            "MOVIES_WATCH_LABEL": {"label": "Movies Watch Path", "type": "text", "placeholder": "/watch/movies", "readonly": True, "group": "paths"},
            "TV_WATCH_LABEL": {"label": "TV Watch Path", "type": "text", "placeholder": "/watch/tv", "readonly": True, "group": "paths"},
            "MOVIES_OUTPUT_LABEL": {"label": "Movies Output Path", "type": "text", "placeholder": "/output/movies", "readonly": True, "group": "paths"},
            "TV_OUTPUT_LABEL": {"label": "TV Output Path", "type": "text", "placeholder": "/output/tv", "readonly": True, "group": "paths"},
            "MEDIA_TEMP_LABEL": {"label": "Temp Folder", "type": "text", "placeholder": "/temp", "readonly": True, "group": "paths"},
            "POSTGRES_HOST": {"label": "PostgreSQL Host", "type": "text", "placeholder": "localhost", "readonly": True, "group": "database"},
            "POSTGRES_PORT": {"label": "PostgreSQL Port", "type": "text", "placeholder": "5432", "readonly": True, "group": "database"},
            "POSTGRES_DB": {"label": "Database Name", "type": "text", "placeholder": "transcodarr", "readonly": True, "group": "database"},
            "POSTGRES_USER": {"label": "Username", "type": "text", "placeholder": "transcodarr", "readonly": True, "group": "database"},
            "POSTGRES_PASSWORD": {"label": "Password", "type": "password", "placeholder": "", "readonly": True, "group": "database"},
            "MANUAL_WORKERS": {"label": "Manual Workers", "type": "select", "hint": "Workers for UI-triggered transcodes", "group": "advanced", "options": [
                {"value": "0", "label": "0 (Disabled)"},
                {"value": "1", "label": "1"},
                {"value": "2", "label": "2"},
                {"value": "3", "label": "3"},
                {"value": "4", "label": "4"},
            ]},
            "AUTO_WORKERS": {"label": "Auto Workers", "type": "select", "hint": "Workers for automatic watchdog transcodes", "group": "advanced", "options": [
                {"value": "0", "label": "0 (Disabled)"},
                {"value": "1", "label": "1"},
                {"value": "2", "label": "2"},
                {"value": "3", "label": "3"},
                {"value": "4", "label": "4"},
            ]},
            "HW_MAX_WORKERS": {"label": "Hardware Encode Cap", "type": "select", "hint": "Max concurrent hardware encodes across all workers — a GPU session limit, not a preference. Blank uses what the detected device reports.", "group": "advanced", "options": [
                {"value": "", "label": "Auto (detect from device)"},
                {"value": "1", "label": "1"},
                {"value": "2", "label": "2"},
                {"value": "3", "label": "3"},
                {"value": "4", "label": "4"},
            ]},
            "TRANSCODARR_URL": {"label": "Transcodarr URL", "type": "text", "placeholder": "http://localhost:5025", "hint": "External URL for webhooks (auto-detected if empty)", "group": "advanced"},
            "WATCH_DEBOUNCE_SEC": {"label": "Watch Debounce (sec)", "type": "text", "placeholder": "20", "group": "advanced"},
        },
        "groups": {
            "advanced": {"label": "Advanced", "hint": ""},
            "paths":    {"label": "Media Paths", "hint": "Set via docker-compose volume mounts. Restart required to change."},
            "database": {"label": "Database", "hint": "Set via environment variables or docker-compose. Restart required to change."},
        }
    },
}


# ----------------------- subtitle providers -----------------------
SUBTITLE_PROVIDERS = {
    "opensubtitlescom": {
        "name": "OpenSubtitles.com",
        "requires_auth": True,
        "supports_multiple_accounts": True,
        "config_key": "SUBLIMINAL_OSCOM_ACCOUNTS",
        "enabled_key": "SUBLIMINAL_OSCOM_ENABLED",
        "legacy_user_key": "SUBLIMINAL_OSCOM_USER",
        "legacy_pass_key": "SUBLIMINAL_OSCOM_PASS",
    },
    "podnapisi": {
        "name": "Podnapisi",
        "requires_auth": False,
        "supports_multiple_accounts": False,
        "config_key": "SUBLIMINAL_PODNAPISI_ENABLED",
        "enabled_key": "SUBLIMINAL_PODNAPISI_ENABLED",
    },
    "addic7ed": {
        "name": "Addic7ed",
        "requires_auth": True,
        "supports_multiple_accounts": True,
        "config_key": "SUBLIMINAL_ADDIC7ED_ACCOUNTS",
        "enabled_key": "SUBLIMINAL_ADDIC7ED_ENABLED",
    },
    "tvsubtitles": {
        "name": "TVsubtitles",
        "requires_auth": False,
        "supports_multiple_accounts": False,
        "config_key": "SUBLIMINAL_TVSUBTITLES_ENABLED",
        "enabled_key": "SUBLIMINAL_TVSUBTITLES_ENABLED",
    },
}


# ----------------------- compression tier validation -----------------------
VALID_PRESETS = {"ultrafast","superfast","veryfast","faster","fast","medium","slow","slower","veryslow"}
VALID_CRFS = {"","18","19","20","21","22","23","24","25","26","28","30"}


# ----------------------- env file helpers -----------------------
def get_env_path() -> Path:
    """Get the .env file path."""
    custom_path = os.environ.get("ENV_FILE_PATH")
    if custom_path:
        return Path(custom_path)
    return Path(__file__).parent.parent / ".env"


def set_env_key(env_path: Path, key: str, value: str) -> None:
    """Set a key in .env file without using temp files."""
    lines = []
    if env_path.exists():
        with open(env_path, "r", encoding="utf-8") as f:
            lines = f.readlines()

    key_found = False
    new_lines = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith(f"{key}=") or stripped == key:
            if value:
                if any(c in value for c in [' ', '"', "'", '\n', '\t', '#']):
                    new_lines.append(f'{key}="{value}"\n')
                else:
                    new_lines.append(f"{key}={value}\n")
            else:
                new_lines.append(f"{key}=\n")
            key_found = True
        else:
            new_lines.append(line)

    if not key_found:
        if value:
            if any(c in value for c in [' ', '"', "'", '\n', '\t', '#']):
                new_lines.append(f'{key}="{value}"\n')
            else:
                new_lines.append(f"{key}={value}\n")
        else:
            new_lines.append(f"{key}=\n")

    with open(env_path, "w", encoding="utf-8") as f:
        f.writelines(new_lines)


# ----------------------- video constants -----------------------
VIDEO_EXTS = {".mp4", ".mkv", ".mov", ".avi"}
SENTINEL_NAME = ".transcodarr-nosub"


def has_sentinel(file_path: str) -> bool:
    """Check if a sentinel file exists in the media file's folder."""
    sentinel_path = os.path.join(os.path.dirname(file_path), SENTINEL_NAME)
    return os.path.exists(sentinel_path)


def remove_sentinel(file_path: str) -> bool:
    """Remove sentinel file from the media file's folder if it exists."""
    sentinel_path = os.path.join(os.path.dirname(file_path), SENTINEL_NAME)
    if os.path.exists(sentinel_path):
        os.remove(sentinel_path)
        return True
    return False


# ----------------------- media cache -----------------------
_media_cache = {
    "movies": {"items": [], "last_scan": 0, "scanning": False},
    "tv": {"items": [], "last_scan": 0, "scanning": False},
}
_CACHE_DIR = Path(__file__).parent / "cache"

def _get_cache_path(media_type: str) -> Path:
    _CACHE_DIR.mkdir(exist_ok=True)
    return _CACHE_DIR / f"{media_type}_cache.json"

def load_cache(media_type: str) -> list[dict]:
    """Load cache into memory. Prefers Postgres (Phase 3), falls back to JSON file."""
    # Try DB first — survives container rebuilds AND queryable.
    try:
        from transcodarr_core.database import (
            cache_count_movies, cache_count_tv_episodes,
            get_all_movies, get_all_tv_episodes,
        )
        if media_type == "movies":
            count = cache_count_movies()
            if count > 0:
                items = get_all_movies()
                _media_cache["movies"]["items"] = items
                _media_cache["movies"]["last_scan"] = int(time.time())
                logging.info("[CACHE] Loaded %d movies from DB", len(items))
                return items
        else:
            count = cache_count_tv_episodes()
            if count > 0:
                items = get_all_tv_episodes()
                _media_cache["tv"]["items"] = items
                _media_cache["tv"]["last_scan"] = int(time.time())
                logging.info("[CACHE] Loaded %d TV episodes from DB", len(items))
                return items
    except Exception as e:
        logging.warning("[CACHE] DB load failed for %s, falling back to JSON: %s", media_type, e)

    # Fallback: JSON file (legacy persistence path)
    cache_path = _get_cache_path(media_type)
    if cache_path.exists():
        try:
            with open(cache_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                _media_cache[media_type]["items"] = data.get("items", [])
                _media_cache[media_type]["last_scan"] = data.get("last_scan", 0)
                return _media_cache[media_type]["items"]
        except Exception:
            pass
    return []

def save_cache(media_type: str, items: list[dict]):
    """Save to disk and memory."""
    _media_cache[media_type]["items"] = items
    _media_cache[media_type]["last_scan"] = int(time.time())
    cache_path = _get_cache_path(media_type)
    try:
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump({"items": items, "last_scan": _media_cache[media_type]["last_scan"]}, f)
    except Exception:
        pass

def get_media_cache():
    """Return the media cache dict for direct access."""
    return _media_cache


# ----------------------- enrichment state -----------------------
enrich_state = {"running": False, "total": 0, "processed": 0, "nfo_written": 0, "posters_downloaded": 0, "errors": 0}


# ----------------------- formatting helpers -----------------------
def bytes_to_gb(n: int) -> float:
    try:
        return round(n / (1024**3), 2)
    except Exception:
        return 0.0


def ffprobe_metadata(path: str) -> dict:
    """Return detailed metadata dict using ffprobe."""
    result_dict = {
        "vcodec": None, "acodec": None, "resolution": None, "runtime_min": None,
        "video_bitrate": None, "audio_bitrate": None, "total_bitrate": None,
        "frame_rate": None, "audio_channels": None, "audio_sample_rate": None,
    }
    try:
        cmd = subprocess.run(
            ["ffprobe", "-v", "error",
             "-show_entries", "stream=codec_type,codec_name,width,height,bit_rate,r_frame_rate,channels,sample_rate",
             "-show_entries", "format=duration,bit_rate",
             "-of", "json", path],
            capture_output=True, text=True, check=False, timeout=15
        )
        data = json.loads(cmd.stdout or "{}")

        for stream in data.get("streams", []):
            if stream.get("codec_type") == "video" and not result_dict["vcodec"]:
                result_dict["vcodec"] = stream.get("codec_name")
                w, h = stream.get("width"), stream.get("height")
                if w and h:
                    result_dict["resolution"] = f"{w}x{h}"
                vbr = stream.get("bit_rate")
                if vbr:
                    try:
                        result_dict["video_bitrate"] = int(vbr)
                    except Exception:
                        pass
                fps_str = stream.get("r_frame_rate")
                if fps_str and "/" in fps_str:
                    try:
                        num, den = fps_str.split("/")
                        result_dict["frame_rate"] = round(int(num) / int(den), 3)
                    except Exception:
                        pass
                elif fps_str:
                    try:
                        result_dict["frame_rate"] = float(fps_str)
                    except Exception:
                        pass

            elif stream.get("codec_type") == "audio" and not result_dict["acodec"]:
                result_dict["acodec"] = stream.get("codec_name")
                abr = stream.get("bit_rate")
                if abr:
                    try:
                        result_dict["audio_bitrate"] = int(abr)
                    except Exception:
                        pass
                ch = stream.get("channels")
                if ch:
                    result_dict["audio_channels"] = ch
                sr = stream.get("sample_rate")
                if sr:
                    try:
                        result_dict["audio_sample_rate"] = int(sr)
                    except Exception:
                        pass

        fmt = data.get("format", {})
        duration = fmt.get("duration")
        if duration:
            result_dict["runtime_min"] = int(round(float(duration) / 60.0))
        total_br = fmt.get("bit_rate")
        if total_br:
            try:
                result_dict["total_bitrate"] = int(total_br)
            except Exception:
                pass

    except Exception:
        pass
    return result_dict


def format_bitrate(bps: int | None) -> str | None:
    """Format bitrate in human-readable form."""
    if not bps:
        return None
    if bps >= 1_000_000:
        return f"{bps / 1_000_000:.1f} Mbps"
    return f"{bps // 1000} kbps"


def format_audio_channels(ch: int | None) -> str | None:
    """Format audio channels nicely."""
    if not ch:
        return None
    mapping = {1: "Mono", 2: "Stereo", 6: "5.1", 8: "7.1"}
    return mapping.get(ch, f"{ch}ch")


def format_timestamp(ts: int | float | None) -> str | None:
    """Format timestamp as relative or absolute date."""
    if not ts:
        return None
    now = time.time()
    diff = now - ts
    if diff < 60:
        return "Just now"
    elif diff < 3600:
        mins = int(diff // 60)
        return f"{mins}m ago"
    elif diff < 86400:
        hrs = int(diff // 3600)
        return f"{hrs}h ago"
    elif diff < 86400 * 7:
        days = int(diff // 86400)
        return f"{days}d ago"
    else:
        from datetime import datetime
        dt = datetime.fromtimestamp(ts)
        return dt.strftime("%b %d, %Y")


def format_duration(seconds: float | int | None) -> str | None:
    """Format duration in human-readable form."""
    if not seconds:
        return None
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    elif seconds < 3600:
        mins = seconds // 60
        secs = seconds % 60
        return f"{mins}m {secs}s" if secs else f"{mins}m"
    else:
        hrs = seconds // 3600
        mins = (seconds % 3600) // 60
        return f"{hrs}h {mins}m" if mins else f"{hrs}h"


_year_re = re.compile(r"\((\d{4})\)")
_sxe_re = re.compile(r"[sS](\d{1,2})[eE](\d{1,3})")

def year_from_name(name: str):
    m = _year_re.search(name)
    return int(m.group(1)) if m else None

def strip_year_from_title(name: str) -> str:
    """Remove trailing (YYYY) from title."""
    return re.sub(r"\s*\(\d{4}\)\s*$", "", name).strip()

def parse_sxe(name: str):
    m = _sxe_re.search(name)
    if not m:
        return None, None
    return int(m.group(1)), int(m.group(2))


def parse_multi_episode(name: str) -> list[int] | None:
    """Parse multi-episode codes from filename."""
    concat_match = re.search(r'[sS](\d{1,2})([eE]\d{1,3})+', name)
    if concat_match:
        episodes = [int(m.group(1)) for m in re.finditer(r'[eE](\d{1,3})', name)]
        if len(episodes) > 1:
            return episodes

    range_match = re.search(r'[sS]\d{1,2}[eE](\d{1,3})-[eE]?(\d{1,3})', name)
    if range_match:
        start = int(range_match.group(1))
        end = int(range_match.group(2))
        if end > start:
            return list(range(start, end + 1))

    return None


def find_video_for_meta(meta_file: Path, video_exts: set) -> Path | None:
    """Given a .meta.json file, find its matching video file in the same folder."""
    logger = logging.getLogger("transcodarr.api")

    folder = meta_file.parent
    meta_stem = meta_file.stem
    if meta_stem.endswith(".meta"):
        meta_stem = meta_stem[:-5]

    logger.debug(f"[_find_video_for_meta] Looking for video matching meta_stem='{meta_stem}'")

    video_files = [f for f in folder.iterdir()
                   if f.is_file() and f.suffix.lower() in video_exts]
    if not video_files:
        logger.debug(f"[_find_video_for_meta] No video files found in {folder}")
        return None

    logger.debug(f"[_find_video_for_meta] Video files in folder: {[v.name for v in video_files]}")

    # 1. Try exact stem match
    for vf in video_files:
        if vf.stem == meta_stem:
            logger.debug(f"[_find_video_for_meta] Exact stem match found: {vf.name}")
            return vf

    logger.debug(f"[_find_video_for_meta] No exact stem match. Video stems: {[v.stem for v in video_files]}")

    # 2. Try episode code match (for TV)
    meta_ep = parse_sxe(meta_file.name)
    logger.debug(f"[_find_video_for_meta] meta_ep parsed from '{meta_file.name}': {meta_ep}")
    if meta_ep[0] is not None:
        for vf in video_files:
            video_ep = parse_sxe(vf.name)
            logger.debug(f"[_find_video_for_meta] Comparing meta_ep={meta_ep} with video_ep={video_ep} from '{vf.name}'")
            if video_ep == meta_ep:
                logger.debug(f"[_find_video_for_meta] Episode code match found: {vf.name}")
                return vf
        logger.debug(f"[_find_video_for_meta] No episode code match found for {meta_ep}")
        return None

    # 3. Non-TV content: fall back to first video file
    return video_files[0] if video_files else None


def get_worker_pool_processing_paths() -> dict:
    """Get paths currently being processed by worker pool with their job info."""
    from transcodarr_core.worker_pool import get_worker_pool, JobStatus

    result = {}
    worker_pool = get_worker_pool()
    if not worker_pool:
        return result

    for job in worker_pool.get_all_jobs(include_completed=False):
        elapsed = time.time() - job.started_at if job.started_at else None
        result[job.file_path] = {
            "status": "processing" if job.status == JobStatus.RUNNING else "queued",
            "progress": job.progress,
            "elapsed": elapsed,
            "elapsed_fmt": format_duration(elapsed) if elapsed else None,
            "job_id": job.job_id,
        }

    return result


# ----------------------- scan functions -----------------------
def scan_pending_movies(watch_root: Path, temp_root: Path | None) -> list[dict]:
    """Scan watch folder for movies waiting to be processed."""
    from transcodarr_core.config import get_media_paths
    _mp = get_media_paths()
    items: list[dict] = []
    movies_root = Path(_mp["movies_watch"])
    if not movies_root.exists():
        movies_root = watch_root / "_processing" / "movies"
    if not movies_root.exists():
        return items

    worker_pool_jobs = get_worker_pool_processing_paths()

    processing_stems = set()
    if temp_root and temp_root.exists():
        for search_path in [temp_root / movies_root.name, temp_root / "_processing" / movies_root.name]:
            if search_path.exists():
                for p in search_path.rglob("*.tmp.mp4"):
                    processing_stems.add(p.stem.replace(".tmp", ""))

    try:
        ignored_paths = get_ignored_paths()
    except Exception:
        ignored_paths = set()

    for meta_file in movies_root.rglob("*.meta.json"):
        try:
            folder = meta_file.parent

            video = find_video_for_meta(meta_file, VIDEO_EXTS)
            if not video:
                continue

            if video.stem in processing_stems:
                continue

            with open(meta_file, "r", encoding="utf-8") as f:
                data = json.load(f)

            stat = video.stat()
            raw_title = data.get("title") or folder.name
            title = strip_year_from_title(raw_title)
            year = data.get("year") or year_from_name(raw_title)

            poster_url = None
            poster_file = folder / "poster.jpg"
            if not poster_file.exists():
                try:
                    poster_meta = {
                        "imdb_id": data.get("imdb_id"),
                        "radarr_movie_id": data.get("radarr_movie_id"),
                    }
                    ensure_poster(str(folder), kind="movie", meta=poster_meta)
                except Exception:
                    pass

            if poster_file.exists():
                try:
                    rel_path = poster_file.relative_to(watch_root)
                    poster_url = f"/api/media/poster/watch/{quote(str(rel_path), safe='/')}"
                except ValueError:
                    pass

            mtime = int(stat.st_mtime)
            video_path_str = str(video)
            is_file_ignored = (video_path_str in ignored_paths) or has_sentinel(video_path_str)

            pool_job = worker_pool_jobs.get(video_path_str)
            if pool_job:
                status = pool_job["status"]
                progress = pool_job["progress"]
                elapsed = pool_job.get("elapsed")
                elapsed_fmt = pool_job.get("elapsed_fmt")
            else:
                status = "pending"
                progress = None
                elapsed = None
                elapsed_fmt = None

            item = {
                "title": title,
                "year": year,
                "path": video_path_str,
                "size_gb": bytes_to_gb(stat.st_size),
                "runtime_min": None,
                "container": video.suffix.lstrip(".").lower(),
                "vcodec": None,
                "acodec": None,
                "resolution": None,
                "mtime": mtime,
                "mtime_fmt": format_timestamp(mtime),
                "status": status,
                "progress": progress,
                "elapsed": elapsed,
                "elapsed_fmt": elapsed_fmt,
                "poster": poster_url,
                "ignored": is_file_ignored,
            }
            items.append(item)
        except Exception:
            continue

    return items


def scan_pending_tv(watch_root: Path, temp_root: Path | None) -> list[dict]:
    """Scan watch folder for TV episodes waiting to be processed."""
    logger = logging.getLogger("transcodarr.api")

    from transcodarr_core.config import get_media_paths
    _mp = get_media_paths()
    items: list[dict] = []
    tv_root = Path(_mp["tv_watch"])
    if not tv_root.exists():
        tv_root = watch_root / "_processing" / "tv"
    if not tv_root.exists():
        logger.debug(f"[_scan_pending_tv] TV watch path {_mp['tv_watch']} does not exist")
        return items

    logger.debug(f"[_scan_pending_tv] Scanning tv_root: {tv_root}")

    worker_pool_jobs = get_worker_pool_processing_paths()

    processing_stems = set()
    if temp_root and temp_root.exists():
        for search_path in [temp_root / tv_root.name, temp_root / "_processing" / tv_root.name]:
            if search_path.exists():
                for p in search_path.rglob("*.tmp.mp4"):
                    processing_stems.add(p.stem.replace(".tmp", ""))

    try:
        ignored_paths = get_ignored_paths()
    except Exception:
        ignored_paths = set()

    meta_files_found = list(tv_root.rglob("*.meta.json"))
    logger.debug(f"[_scan_pending_tv] Found {len(meta_files_found)} .meta.json files")

    for meta_file in meta_files_found:
        try:
            folder = meta_file.parent
            logger.debug(f"[_scan_pending_tv] Processing meta: {meta_file}")

            video = find_video_for_meta(meta_file, VIDEO_EXTS)
            if not video:
                video_files = [f for f in folder.iterdir() if f.is_file() and f.suffix.lower() in VIDEO_EXTS]
                logger.debug(f"[_scan_pending_tv] No matching video for {meta_file.name}. Videos in folder: {[v.name for v in video_files]}")
                continue

            if video.stem in processing_stems:
                logger.debug(f"[_scan_pending_tv] Skipping {video.name} - has temp file (main loop processing)")
                continue

            with open(meta_file, "r", encoding="utf-8") as f:
                data = json.load(f)

            stat = video.stat()

            series = data.get("series") or {}
            show = series.get("title") or folder.parent.name
            episode_data = data.get("episode") or {}
            season = episode_data.get("season")
            episodes = episode_data.get("episodes") or []
            episode = episodes[0] if episodes else None
            episodes_list = episodes if len(episodes) > 1 else None

            poster_url = None
            show_folder = folder.parent if "season" in folder.name.lower() else folder
            poster_file = show_folder / "poster.jpg"
            if not poster_file.exists():
                try:
                    poster_meta = {
                        "imdb_id": series.get("imdb_id"),
                        "sonarr_series_id": series.get("sonarr_series_id"),
                    }
                    ensure_poster(str(show_folder), kind="tv", meta=poster_meta)
                except Exception:
                    pass

            if poster_file.exists():
                try:
                    rel_path = poster_file.relative_to(watch_root)
                    poster_url = f"/api/media/poster/watch/{quote(str(rel_path), safe='/')}"
                except ValueError:
                    pass

            mtime = int(stat.st_mtime)
            episode_titles = episode_data.get("titles") or []
            if episodes_list and len(episode_titles) > 1:
                title = " + ".join(episode_titles)
            else:
                title = re.sub(r"^[sS]\d{1,2}[eE]\d{1,3}(?:[eE-]\d{1,3})*\s*[-–—]?\s*", "", video.stem)
            video_path_str = str(video)
            is_file_ignored = (video_path_str in ignored_paths) or has_sentinel(video_path_str)

            pool_job = worker_pool_jobs.get(video_path_str)
            if pool_job:
                status = pool_job["status"]
                progress = pool_job["progress"]
                elapsed = pool_job.get("elapsed")
                elapsed_fmt = pool_job.get("elapsed_fmt")
            else:
                status = "pending"
                progress = None
                elapsed = None
                elapsed_fmt = None

            item = {
                "show": show,
                "season": season,
                "episode": episode,
                "episodes": episodes_list,
                "title": title or video.stem,
                "path": video_path_str,
                "size_gb": bytes_to_gb(stat.st_size),
                "runtime_min": None,
                "container": video.suffix.lstrip(".").lower(),
                "vcodec": None,
                "acodec": None,
                "resolution": None,
                "mtime": mtime,
                "mtime_fmt": format_timestamp(mtime),
                "status": status,
                "progress": progress,
                "elapsed": elapsed,
                "elapsed_fmt": elapsed_fmt,
                "poster": poster_url,
                "ignored": is_file_ignored,
            }
            items.append(item)
            logger.debug(f"[_scan_pending_tv] Added item: {video.name} (status={status})")
        except Exception as e:
            logger.warning(f"[_scan_pending_tv] Error processing {meta_file}: {e}")
            continue

    logger.debug(f"[_scan_pending_tv] Total pending TV items found: {len(items)}")
    return items


def scan_processing_movies(temp_root: Path, watch_root: Path | None = None) -> list[dict]:
    """Scan temp folder for in-progress movie transcodes."""
    from transcodarr_core.config import get_media_paths
    _mp = get_media_paths()
    _movies_name = Path(_mp["movies_watch"]).name
    items: list[dict] = []
    movies_root = temp_root / _movies_name
    if not movies_root.exists():
        movies_root = temp_root / "_processing" / _movies_name
    if not movies_root.exists():
        return items

    for progress_file in movies_root.rglob("*.progress.json"):
        try:
            with open(progress_file, "r", encoding="utf-8") as f:
                data = json.load(f)

            tmp_video = progress_file.with_suffix("").with_suffix(".tmp.mp4")
            if not tmp_video.exists():
                continue

            stat = tmp_video.stat()
            title = data.get("title") or progress_file.stem.replace(".progress", "")

            poster_url = None
            source_file = data.get("source_file")
            if source_file and watch_root:
                source_folder = Path(source_file).parent
                poster_file = source_folder / "poster.jpg"
                if poster_file.exists():
                    try:
                        rel_path = poster_file.relative_to(watch_root)
                        poster_url = f"/api/media/poster/watch/{quote(str(rel_path), safe='/')}"
                    except ValueError:
                        pass

            started_at = data.get("started_at") or stat.st_mtime
            elapsed = time.time() - started_at if started_at else None

            item = {
                "title": title,
                "year": data.get("year"),
                "path": str(tmp_video),
                "source_path": source_file or "",
                "size_gb": bytes_to_gb(stat.st_size),
                "runtime_min": None,
                "container": "mp4",
                "vcodec": None,
                "acodec": None,
                "resolution": None,
                "mtime": int(started_at),
                "mtime_fmt": format_timestamp(started_at),
                "status": "processing",
                "progress": data.get("progress", 0),
                "elapsed": elapsed,
                "elapsed_fmt": format_duration(elapsed),
                "poster": poster_url,
            }
            items.append(item)
        except Exception:
            continue

    return items


def scan_processing_tv(temp_root: Path, watch_root: Path | None = None) -> list[dict]:
    """Scan temp folder for in-progress TV transcodes."""
    from transcodarr_core.config import get_media_paths
    _mp = get_media_paths()
    _tv_name = Path(_mp["tv_watch"]).name
    items: list[dict] = []
    tv_root = temp_root / _tv_name
    if not tv_root.exists():
        tv_root = temp_root / "_processing" / _tv_name
    if not tv_root.exists():
        return items

    for progress_file in tv_root.rglob("*.progress.json"):
        try:
            with open(progress_file, "r", encoding="utf-8") as f:
                data = json.load(f)

            tmp_video = progress_file.with_suffix("").with_suffix(".tmp.mp4")
            if not tmp_video.exists():
                continue

            stat = tmp_video.stat()
            show = data.get("show") or ""

            poster_url = None
            source_file = data.get("source_file")
            if source_file and watch_root:
                source_folder = Path(source_file).parent
                show_folder = source_folder.parent if "season" in source_folder.name.lower() else source_folder
                poster_file = show_folder / "poster.jpg"
                if poster_file.exists():
                    try:
                        rel_path = poster_file.relative_to(watch_root)
                        poster_url = f"/api/media/poster/watch/{quote(str(rel_path), safe='/')}"
                    except ValueError:
                        pass

            started_at = data.get("started_at") or stat.st_mtime
            elapsed = time.time() - started_at if started_at else None

            raw_title = data.get("title") or progress_file.stem.replace(".progress", "")
            title = re.sub(r"^[sS]\d{1,2}[eE]\d{1,3}(?:[eE-]\d{1,3})*\s*[-–—]?\s*", "", raw_title)

            episodes = data.get("episodes") or []
            episodes_list = episodes if len(episodes) > 1 else None

            item = {
                "show": show,
                "season": data.get("season"),
                "episode": data.get("episode"),
                "episodes": episodes_list,
                "title": title or raw_title,
                "path": str(tmp_video),
                "source_path": source_file or "",
                "size_gb": bytes_to_gb(stat.st_size),
                "runtime_min": None,
                "container": "mp4",
                "vcodec": None,
                "acodec": None,
                "resolution": None,
                "mtime": int(started_at),
                "mtime_fmt": format_timestamp(started_at),
                "status": "processing",
                "progress": data.get("progress", 0),
                "elapsed": elapsed,
                "elapsed_fmt": format_duration(elapsed),
                "poster": poster_url,
            }
            items.append(item)
        except Exception:
            continue

    return items


def scan_reencode_progress(temp_root: Path) -> dict[str, dict]:
    """Scan temp_root/_reencode/ for *.progress.json files."""
    result = {}
    reencode_dir = temp_root / "_reencode"
    if not reencode_dir.exists():
        return result

    for progress_file in reencode_dir.rglob("*.progress.json"):
        try:
            with open(progress_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            source_file = data.get("source_file")
            if not source_file:
                continue
            started_at = data.get("started_at")
            elapsed = time.time() - started_at if started_at else None
            result[source_file] = {
                "progress": data.get("progress", 0),
                "elapsed": elapsed,
                "elapsed_fmt": format_duration(elapsed) if elapsed else None,
            }
        except Exception:
            continue

    return result


def load_transcode_meta(video_path: Path) -> dict:
    """Load transcode metadata from database."""
    try:
        path_str = str(video_path)
        history = get_transcode_history(path_str)
        if history:
            return {
                "processed_at": history.get("processed_at"),
                "processing_duration": history.get("processing_duration"),
                "source_file": history.get("source_path"),
                "source_size": history.get("source_size"),
                "copied": history.get("copied", False),
            }
    except Exception as e:
        logging.debug("[_load_transcode_meta] Error loading history for %s: %s", video_path, e)
    return {}


def read_title_from_nfo(video_path: Path) -> str | None:
    """Read episode title from NFO file if it exists."""
    try:
        nfo_path = video_path.with_suffix(".nfo")
        if not nfo_path.exists():
            return None
        import xml.etree.ElementTree as ET
        tree = ET.parse(nfo_path)
        root = tree.getroot()
        title_el = root.find("title")
        if title_el is not None and title_el.text:
            return title_el.text.strip()
    except Exception:
        pass
    return None


def scan_movies_incremental(root: Path, existing_cache: dict[str, dict], reencode_map: dict[str, dict] | None = None) -> list[dict]:
    """Scan movies, reusing cached metadata when file hasn't changed."""
    items: list[dict] = []
    if not root.exists():
        return items

    for p in root.rglob("*"):
        if p.is_file() and p.suffix.lower() in VIDEO_EXTS:
            try:
                stat = p.stat()
                path_str = str(p)
                mtime = int(stat.st_mtime)
                size_gb = bytes_to_gb(stat.st_size)

                cached = existing_cache.get(path_str)
                if cached and cached.get("mtime") == mtime:
                    item = cached.copy()
                else:
                    raw_title = p.stem
                    try:
                        folder = p.parent.name
                        if folder and len(folder) > 3:
                            raw_title = folder
                    except Exception:
                        pass

                    year = year_from_name(raw_title)
                    title = strip_year_from_title(raw_title)

                    meta = ffprobe_metadata(path_str)
                    transcode_meta = load_transcode_meta(p)

                    source_size = transcode_meta.get("source_size")
                    compression_ratio = None
                    if source_size and stat.st_size:
                        compression_ratio = round(source_size / stat.st_size, 2)

                    item = {
                        "title": title,
                        "year": year,
                        "path": path_str,
                        "size_gb": size_gb,
                        "runtime_min": meta["runtime_min"],
                        "container": p.suffix.lstrip(".").lower(),
                        "vcodec": meta["vcodec"],
                        "acodec": meta["acodec"],
                        "resolution": meta["resolution"],
                        "mtime": mtime,
                        "status": "ready",
                        "video_bitrate": meta["video_bitrate"],
                        "audio_bitrate": meta["audio_bitrate"],
                        "total_bitrate": meta["total_bitrate"],
                        "frame_rate": meta["frame_rate"],
                        "audio_channels": meta["audio_channels"],
                        "audio_sample_rate": meta["audio_sample_rate"],
                        "video_bitrate_fmt": format_bitrate(meta["video_bitrate"]),
                        "audio_bitrate_fmt": format_bitrate(meta["audio_bitrate"]),
                        "total_bitrate_fmt": format_bitrate(meta["total_bitrate"]),
                        "audio_channels_fmt": format_audio_channels(meta["audio_channels"]),
                        "processed_at": transcode_meta.get("processed_at"),
                        "processing_duration": transcode_meta.get("processing_duration"),
                        "processing_duration_fmt": format_duration(transcode_meta.get("processing_duration")),
                        "source_size_gb": bytes_to_gb(source_size) if source_size else None,
                        "compression_ratio": compression_ratio,
                    }

                item["mtime_fmt"] = format_timestamp(mtime)
                if item.get("processed_at"):
                    item["processed_at_fmt"] = format_timestamp(item["processed_at"])
                poster_file = p.parent / "poster.jpg"
                item["poster"] = f"/api/media/poster/{quote(str(poster_file.relative_to(root.parent)), safe='/')}" if poster_file.exists() else None

                if reencode_map and path_str in reencode_map:
                    re_info = reencode_map[path_str]
                    item["reencode_progress"] = re_info["progress"]
                    item["reencode_elapsed_fmt"] = re_info.get("elapsed_fmt")
                    item["status"] = "re-encoding"

                items.append(item)
            except Exception:
                continue

    items.sort(key=lambda d: d["mtime"], reverse=True)
    return items


def scan_tv_incremental(root: Path, existing_cache: dict[str, dict], reencode_map: dict[str, dict] | None = None) -> list[dict]:
    """Scan TV, reusing cached metadata when file hasn't changed."""
    items: list[dict] = []
    if not root.exists():
        return items

    for p in root.rglob("*"):
        if p.is_file() and p.suffix.lower() in VIDEO_EXTS:
            try:
                stat = p.stat()
                path_str = str(p)
                mtime = int(stat.st_mtime)
                size_gb = bytes_to_gb(stat.st_size)

                rel = p.relative_to(root)
                parts = rel.parts
                show = parts[0] if parts else p.parent.name

                cached = existing_cache.get(path_str)
                if cached and cached.get("mtime") == mtime:
                    item = cached.copy()
                    if "episodes" not in item:
                        item["episodes"] = parse_multi_episode(p.name)
                else:
                    season, episode = parse_sxe(p.name)
                    episodes_list = parse_multi_episode(p.name)
                    meta = ffprobe_metadata(path_str)
                    transcode_meta = load_transcode_meta(p)

                    source_size = transcode_meta.get("source_size")
                    compression_ratio = None
                    if source_size and stat.st_size:
                        compression_ratio = round(source_size / stat.st_size, 2)

                    if episodes_list:
                        nfo_title = read_title_from_nfo(p)
                        if nfo_title:
                            title = nfo_title
                        else:
                            title = re.sub(r"^[sS]\d{1,2}[eE]\d{1,3}(?:[eE-]\d{1,3})*\s*[-–—]?\s*", "", p.stem)
                    else:
                        title = re.sub(r"^[sS]\d{1,2}[eE]\d{1,3}(?:[eE-]\d{1,3})*\s*[-–—]?\s*", "", p.stem)

                    item = {
                        "show": show,
                        "season": season,
                        "episode": episode,
                        "episodes": episodes_list,
                        "title": title or p.stem,
                        "path": path_str,
                        "size_gb": size_gb,
                        "runtime_min": meta["runtime_min"],
                        "container": p.suffix.lstrip(".").lower(),
                        "vcodec": meta["vcodec"],
                        "acodec": meta["acodec"],
                        "resolution": meta["resolution"],
                        "mtime": mtime,
                        "status": "ready",
                        "video_bitrate": meta["video_bitrate"],
                        "audio_bitrate": meta["audio_bitrate"],
                        "total_bitrate": meta["total_bitrate"],
                        "frame_rate": meta["frame_rate"],
                        "audio_channels": meta["audio_channels"],
                        "audio_sample_rate": meta["audio_sample_rate"],
                        "video_bitrate_fmt": format_bitrate(meta["video_bitrate"]),
                        "audio_bitrate_fmt": format_bitrate(meta["audio_bitrate"]),
                        "total_bitrate_fmt": format_bitrate(meta["total_bitrate"]),
                        "audio_channels_fmt": format_audio_channels(meta["audio_channels"]),
                        "processed_at": transcode_meta.get("processed_at"),
                        "processing_duration": transcode_meta.get("processing_duration"),
                        "processing_duration_fmt": format_duration(transcode_meta.get("processing_duration")),
                        "source_size_gb": bytes_to_gb(source_size) if source_size else None,
                        "compression_ratio": compression_ratio,
                    }

                item["mtime_fmt"] = format_timestamp(mtime)
                if item.get("processed_at"):
                    item["processed_at_fmt"] = format_timestamp(item["processed_at"])
                show_folder = root / show
                poster_file = show_folder / "poster.jpg"
                item["poster"] = f"/api/media/poster/{quote(str(poster_file.relative_to(root.parent)), safe='/')}" if poster_file.exists() else None

                if reencode_map and path_str in reencode_map:
                    re_info = reencode_map[path_str]
                    item["reencode_progress"] = re_info["progress"]
                    item["reencode_elapsed_fmt"] = re_info.get("elapsed_fmt")
                    item["status"] = "re-encoding"

                items.append(item)
            except Exception:
                continue

    items.sort(key=lambda d: d["mtime"], reverse=True)
    return items


def migrate_json_cache_to_db() -> dict:
    """One-shot: if the DB cache is empty AND a JSON cache file has items, copy
    JSON → DB and rename the JSON file to *.migrated. Idempotent — safe to run on every startup."""
    from transcodarr_core.database import (
        cache_count_movies, cache_count_tv_episodes,
        bulk_upsert_movies, bulk_upsert_tv_episodes,
    )
    result = {"movies_migrated": 0, "tv_migrated": 0}
    for media_type, count_fn, bulk_fn in (
        ("movies", cache_count_movies, bulk_upsert_movies),
        ("tv", cache_count_tv_episodes, bulk_upsert_tv_episodes),
    ):
        try:
            if count_fn() > 0:
                continue   # DB already populated, nothing to migrate
            cache_path = _get_cache_path(media_type)
            if not cache_path.exists():
                continue
            with open(cache_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            items = data.get("items", []) or []
            if not items:
                continue
            n = bulk_fn(items)
            result[f"{media_type}_migrated"] = n
            logging.info("[MIGRATION] Copied %d %s from JSON cache → DB", n, media_type)
            cache_path.rename(cache_path.with_suffix(".json.migrated"))
        except Exception as e:
            logging.warning("[MIGRATION] %s JSON→DB failed: %s", media_type, e)
    return result


def background_scan(media_type: str, root: Path):
    """Background thread to scan + persist to BOTH the DB cache (Phase 3) and the JSON cache (legacy fallback)."""
    if _media_cache[media_type]["scanning"]:
        return

    _media_cache[media_type]["scanning"] = True
    try:
        existing = {item["path"]: item for item in _media_cache[media_type]["items"]}

        if media_type == "movies":
            items = scan_movies_incremental(root, existing)
        else:
            items = scan_tv_incremental(root, existing)

        save_cache(media_type, items)

        # Persist to Postgres in addition to JSON. Drops rows for files that no longer exist.
        try:
            from transcodarr_core.database import (
                bulk_upsert_movies, bulk_upsert_tv_episodes,
                delete_movies_not_in, delete_tv_episodes_not_in,
            )
            paths = [it["path"] for it in items if it.get("path")]
            if media_type == "movies":
                bulk_upsert_movies(items)
                delete_movies_not_in(paths)
            else:
                bulk_upsert_tv_episodes(items)
                delete_tv_episodes_not_in(paths)
        except Exception as e:
            logging.warning("[SCAN] DB persistence failed for %s: %s", media_type, e)
    finally:
        _media_cache[media_type]["scanning"] = False


def compute_movies_view(settings) -> tuple[list[dict], bool]:
    """Compose the movies view (items + scanning) the same way GET /api/media/movies does,
    but without query filters. Used by both the REST handler and the SSE producer."""
    from transcodarr_core.config import get_media_paths
    _mp = get_media_paths(settings)
    watch_root = Path(settings.WATCH_FOLDER) if settings.WATCH_FOLDER else None
    temp_root = Path(settings.MEDIA_TEMP_FOLDER) if settings.MEDIA_TEMP_FOLDER else None

    if not _media_cache["movies"]["items"]:
        load_cache("movies")

    items = list(_media_cache["movies"]["items"])

    reencode_map = scan_reencode_progress(temp_root) if temp_root else {}
    if reencode_map:
        for item in items:
            re_info = reencode_map.get(item.get("path"))
            if re_info:
                item["reencode_progress"] = re_info["progress"]
                item["reencode_elapsed_fmt"] = re_info.get("elapsed_fmt")
                item["status"] = "re-encoding"

    if watch_root:
        items = scan_pending_movies(watch_root, temp_root) + items
    if temp_root:
        items = scan_processing_movies(temp_root, watch_root) + items

    return items, _media_cache["movies"]["scanning"]


def compute_tv_view(settings) -> tuple[list[dict], bool]:
    """Compose the TV view the same way GET /api/media/tv does, without filters."""
    from transcodarr_core.config import get_media_paths
    _mp = get_media_paths(settings)
    watch_root = Path(settings.WATCH_FOLDER) if settings.WATCH_FOLDER else None
    temp_root = Path(settings.MEDIA_TEMP_FOLDER) if settings.MEDIA_TEMP_FOLDER else None

    if not _media_cache["tv"]["items"]:
        load_cache("tv")

    items = list(_media_cache["tv"]["items"])

    reencode_map = scan_reencode_progress(temp_root) if temp_root else {}
    if reencode_map:
        for item in items:
            re_info = reencode_map.get(item.get("path"))
            if re_info:
                item["reencode_progress"] = re_info["progress"]
                item["reencode_elapsed_fmt"] = re_info.get("elapsed_fmt")
                item["status"] = "re-encoding"

    if watch_root:
        items = scan_pending_tv(watch_root, temp_root) + items
    if temp_root:
        items = scan_processing_tv(temp_root, watch_root) + items

    return items, _media_cache["tv"]["scanning"]


def maybe_trigger_background_scan(settings, media_type: str, force: bool = False) -> None:
    """Kick off a background scan if cache is empty/stale (>60s) or force=True."""
    from transcodarr_core.config import get_media_paths
    _mp = get_media_paths(settings)
    root = Path(_mp["movies_output"] if media_type == "movies" else _mp["tv_output"])

    cache_age = int(time.time()) - _media_cache[media_type]["last_scan"]
    if force or not _media_cache[media_type]["items"] or cache_age > 60:
        if not _media_cache[media_type]["scanning"]:
            t = Thread(target=background_scan, args=(media_type, root), daemon=True)
            t.start()


def read_log_tail(log_path: str, pos: int = 0, inode: str | None = None) -> dict:
    """Byte-offset tail with rotation detection.
    Returns {"text", "pos", "inode", "reset"} — same shape as GET /api/logs/tail."""
    p = Path(log_path)
    if not p.exists():
        return {"text": "", "pos": 0, "inode": None, "reset": True}

    st = p.stat()
    inode_token = f"{st.st_dev}:{st.st_ino}"

    reset = False
    if inode and inode != inode_token:
        reset = True
        pos = 0
    elif pos > st.st_size:
        reset = True
        pos = 0

    with open(p, "rb") as f:
        f.seek(pos)
        data = f.read()
        new_pos = pos + len(data)

    text = data.decode("utf-8", errors="replace").replace("\r\n", "\n")
    return {"text": text, "pos": new_pos, "inode": inode_token, "reset": reset}


_SORT_FIELDS = {"mtime", "size_gb", "title", "year", "show", "season", "episode"}


def status_key(item: dict) -> str:
    """Mirror of _statusKey() in ui.js — collapses raw status into UI-facing buckets."""
    s = item.get("status")
    if s in ("processing", "queued", "re-encoding"):
        return "processing"
    if s == "pending" and item.get("ignored"):
        return "ignored"
    if s == "pending":
        return "pending"
    return "ready"


def apply_filters(
    items: list[dict], q: str = "", limit: int = 0,
    sort: str = "", sort_order: str = "asc",
    status: str = "all",
    page: int = 0, page_size: int = 0,
) -> tuple[list[dict], int]:
    """Filter / sort / paginate.

    Returns (items, total_count) — total_count is the count BEFORE pagination, so the
    client can render "X of Y matching" without loading the rest.

    Backward-compat: pass page=0 or page_size=0 to skip pagination (return all).
    """
    if q:
        q_lower = q.lower()
        def _match(d: dict):
            blob = " ".join(str(v) for v in d.values() if isinstance(v, (str, int)))
            return q_lower in blob.lower()
        items = [d for d in items if _match(d)]

    if status and status != "all":
        items = [d for d in items if status_key(d) == status]

    if sort and sort in _SORT_FIELDS:
        reverse = sort_order.lower() == "desc"
        items = sorted(items, key=lambda d: (d.get(sort) is None, d.get(sort, "")), reverse=reverse)

    total = len(items)

    if page > 0 and page_size > 0:
        offset = (page - 1) * page_size
        items = items[offset:offset + page_size]
    elif limit and limit > 0:
        items = items[:limit]

    return items, total


# ----------------------- webhook helpers -----------------------
def remap_path(path: str, path_from: str, path_to: str) -> str:
    """Remap paths from container paths to host paths if configured."""
    if path and path_from and path_to and path.startswith(path_from):
        path = path_to + path[len(path_from):]
    if path:
        path = path.replace("/_processing/", "/")
    return path


_FILENAME_FORBIDDEN = re.compile(r'[/\\:*?"<>|\x00-\x1f]')


def _safe_stem(stem: str) -> str:
    """Replace filesystem-reserved characters so a stem is safe as a path component.

    Uses '+' as the replacement to match the convention qbittorrent/Sonarr
    already use on disk (e.g. "ronny/lily" → "ronny+lily"), keeping the
    meta-stem aligned with the video-stem for fast exact-match lookup.
    """
    cleaned = _FILENAME_FORBIDDEN.sub("+", stem)
    cleaned = re.sub(r"\s+", " ", cleaned).strip().rstrip(".")
    return cleaned[:200] or "untitled"


def write_meta_json(out_dir: Path, stem: str, data: dict) -> Path:
    """Write .meta.json file atomically."""
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = _safe_stem(stem)
    out_file = out_dir / f"{stem}.meta.json"
    tmp_file = out_dir / f".meta.{int(time.time())}.tmp"
    try:
        with open(tmp_file, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        tmp_file.replace(out_file)
        logging.info("[WEBHOOK] Wrote metadata: %s", out_file)
        return out_file
    except Exception as e:
        logging.error("[WEBHOOK] Failed to write %s: %s", out_file, e)
        if tmp_file.exists():
            tmp_file.unlink()
        raise


def find_existing_webhook(notifications: list, name_prefix: str) -> dict | None:
    """Find an existing Transcodarr webhook in the notifications list."""
    for n in notifications:
        if n.get("name", "").startswith(name_prefix):
            return n
    return None
