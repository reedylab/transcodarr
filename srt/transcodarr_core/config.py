import os
from pathlib import Path
from dotenv import load_dotenv
from pydantic import BaseSettings, validator

# Resolve .env path: prefer ENV_FILE_PATH env var, fallback to project root
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_ENV_FILE = Path(os.environ.get("ENV_FILE_PATH", _PROJECT_ROOT / ".env"))

# Load .env as fallback — docker-compose environment vars take priority
if _ENV_FILE.exists():
    load_dotenv(_ENV_FILE, override=False)
else:
    import logging
    logging.warning("[CONFIG] .env file not found at %s", _ENV_FILE)


# ============================================================
# Database-backed settings
# ============================================================
# These keys are stored in the database (runtime configurable via UI)
# Everything else stays in docker-compose environment (infrastructure/init)
DB_BACKED_SETTINGS = {
    "TVDB_API_KEY", "TMDB_API_KEY", "OMDB_API_KEY",
    "RADARR_URL", "RADARR_API_KEY", "RADARR_TIMEOUT_S", "RADARR_PATH_FROM", "RADARR_PATH_TO",
    "SONARR_URL", "SONARR_API_KEY", "SONARR_TIMEOUT_S", "SONARR_PATH_FROM", "SONARR_PATH_TO",
    "JELLYFIN_URL", "JELLYFIN_API_KEY",
    "WATCH_DEBOUNCE_SEC",
    "VIDEO_STREAM_MODE", "AUDIO_STREAM_MODE",
    "TARGET_VIDEO_CODEC", "TARGET_AUDIO_CODEC", "TARGET_CONTAINER", "TARGET_RESOLUTION",
    "TARGET_PRESET", "TARGET_PROFILE", "TARGET_AUDIO_BITRATE", "TARGET_AUDIO_CHANNELS",
    "TARGET_CRF", "TARGET_AUDIO_NORMALIZE", "TARGET_HDR_MODE", "TARGET_TONEMAP",
    "ENCODER_THREADS", "X264_THREADS", "FFMPEG_THREADS", "HW_BACKEND", "HW_MAX_WORKERS",
    "SUBLIMINAL_OSCOM_USER", "SUBLIMINAL_OSCOM_PASS", "SUBLIMINAL_OSCOM_ACCOUNTS",
    "SUBLIMINAL_OSCOM_ENABLED", "SUBLIMINAL_PODNAPISI_ENABLED",
    "SUBLIMINAL_ADDIC7ED_ACCOUNTS", "SUBLIMINAL_ADDIC7ED_ENABLED",
    "SUBLIMINAL_TVSUBTITLES_ENABLED",
    "SUBLIMINAL_PROVIDER_ORDER",
    "FFSUBSYNC_MAX_OFFSET", "UI_REQUIRES_LOGIN", "MANUAL_WORKERS", "AUTO_WORKERS", "TRANSCODARR_URL",
    "REQUIRE_SUBTITLES",
    "ACTIVE_PRESET_ID",
}


# Fields that must be coerced from DB strings to specific Python types
_FLOAT_FIELDS = {"RADARR_TIMEOUT_S", "SONARR_TIMEOUT_S", "WATCH_DEBOUNCE_SEC", "FFSUBSYNC_MAX_OFFSET"}
_INT_FIELDS = {"MANUAL_WORKERS", "AUTO_WORKERS"}
_BOOL_FIELDS = {"UI_REQUIRES_LOGIN"}


def _coerce_db_value(name: str, db_val: str, default_val):
    """Coerce a raw DB string to the type expected by the Settings field."""
    if name in _FLOAT_FIELDS:
        try:
            return float(db_val)
        except (ValueError, TypeError):
            return default_val
    if name in _INT_FIELDS:
        try:
            return int(db_val)
        except (ValueError, TypeError):
            return default_val
    if name in _BOOL_FIELDS:
        return str(db_val).lower() in ("true", "1", "yes")
    return db_val  # str stays str


class Settings(BaseSettings):
    TVDB_API_KEY: str | None = None
    TMDB_API_KEY: str | None = None
    OMDB_API_KEY: str | None = None
    RADARR_URL: str | None = None
    RADARR_API_KEY: str | None = None
    RADARR_TIMEOUT_S: float | None = None
    RADARR_PATH_FROM: str | None = None
    RADARR_PATH_TO: str | None = None
    SONARR_URL: str | None = None
    SONARR_API_KEY: str | None = None
    SONARR_TIMEOUT_S: float | None = None
    SONARR_PATH_FROM: str | None = None
    SONARR_PATH_TO: str | None = None
    WATCH_DEBOUNCE_SEC: float = 20.0
    WATCH_FOLDER: str = "/watch"
    OUTPUT_FOLDER: str = "/output"
    MEDIA_TEMP_FOLDER: str = "/temp"
    SUBLIMINAL_OSCOM_USER: str | None = None
    SUBLIMINAL_OSCOM_PASS: str | None = None
    # Multiple accounts for round-robin rotation (JSON list): [{"user": "u1", "pass": "p1"}, ...]
    SUBLIMINAL_OSCOM_ACCOUNTS: str | None = None
    SUBLIMINAL_OSCOM_ENABLED: str | None = None  # "true" to enable (independent of accounts)
    # Additional subtitle providers
    SUBLIMINAL_PODNAPISI_ENABLED: str | None = None  # "true" to enable
    SUBLIMINAL_ADDIC7ED_ACCOUNTS: str | None = None  # JSON list like OSCOM
    SUBLIMINAL_ADDIC7ED_ENABLED: str | None = None  # "true" to enable (independent of accounts)
    SUBLIMINAL_TVSUBTITLES_ENABLED: str | None = None  # "true" to enable
    # Provider priority order (comma-separated): "opensubtitlescom,podnapisi,addic7ed,tvsubtitles"
    SUBLIMINAL_PROVIDER_ORDER: str | None = None
    FFSUBSYNC_MAX_OFFSET: float = 0.5
    VIDEO_STREAM_MODE: str = "encode"
    AUDIO_STREAM_MODE: str = "encode"
    TARGET_VIDEO_CODEC: str = "h264"
    TARGET_AUDIO_CODEC: str = "aac"
    TARGET_CONTAINER: str = ".mp4"
    TARGET_RESOLUTION: str = "1920x1080"
    TARGET_PRESET: str = "fast"
    TARGET_PROFILE: str = "high"
    TARGET_AUDIO_BITRATE: str = "448k"
    TARGET_AUDIO_CHANNELS: str = "6"
    TARGET_CRF: str = ""
    TARGET_AUDIO_NORMALIZE: str = "true"
    TARGET_HDR_MODE: str = "auto"
    REQUIRE_SUBTITLES: str = "true"
    ENCODER_THREADS: str = "4"
    X264_THREADS: str = "4"  # legacy — superseded by ENCODER_THREADS, kept for fallback reads
    FFMPEG_THREADS: str = "1"
    # Video encode backend: software | qsv | vaapi | nvenc. Defaults to software so
    # upgrades never silently move existing installs onto hardware; unavailable
    # backends fall back to software per-job anyway. Normally set per encoding
    # preset rather than globally.
    HW_BACKEND: str = "software"
    # Where HDR->SDR tonemapping runs: auto | software | vaapi | opencl. A filter,
    # not an encoder setting — but it only reaches the GPU when the encode does,
    # so it lives on the preset beside HW_BACKEND.
    TARGET_TONEMAP: str = "auto"
    # Max concurrent hardware encodes across ALL pools — a device ceiling, not an
    # encoding choice, so it lives here rather than in a preset. Blank means "use
    # what the detected device reports it can sustain".
    HW_MAX_WORKERS: str = ""
    FLASK_SECRET: str | None = None
    ADMIN_API_KEY: str | None = None
    UI_REQUIRES_LOGIN: bool = False

    # PostgreSQL Database
    POSTGRES_HOST: str = "localhost"
    POSTGRES_PORT: int = 5432
    POSTGRES_DB: str = "transcodarr"
    POSTGRES_USER: str = "transcodarr"
    POSTGRES_PASSWORD: str | None = None

    # Jellyfin
    JELLYFIN_URL: str | None = None
    JELLYFIN_API_KEY: str | None = None

    # Worker Pool
    MANUAL_WORKERS: int = 0   # Workers for UI-triggered manual transcodes (0 = disabled)
    AUTO_WORKERS: int = 2     # Workers for automatic watchdog transcodes (0 = disabled)

    # Transcodarr external URL (for webhooks)
    TRANSCODARR_URL: str | None = None

    # Clustering / multi-node (infrastructure — set via compose env, not the DB).
    # "master" (default) is a normal standalone install that can also farm jobs to
    # nodes; "node" is a headless ffmpeg executor that registers with a master.
    TRANSCODARR_MODE: str = "master"
    # Node-only: where to reach the master, e.g. http://192.168.20.34:5025
    MASTER_URL: str | None = None
    # Shared secret a node presents to the master (must match on both).
    NODE_TOKEN: str | None = None
    # Stable identity for a node in the master's registry (defaults to the hostname).
    NODE_ID: str | None = None
    # Master-only: farm eligible jobs out to connected nodes instead of always running
    # them locally. Off by default — leave off until node-side execute + master
    # post-processing are wired, or jobs handed to a node will never complete.
    CLUSTER_DISPATCH_ENABLED: bool = False

    # Media paths (infrastructure, set via docker-compose volumes)
    MOVIES_WATCH_PATH: str = "/watch/movies"
    TV_WATCH_PATH: str = "/watch/tv"
    MOVIES_OUTPUT_PATH: str = "/output/movies"
    TV_OUTPUT_PATH: str = "/output/tv"

    # Display labels — host paths passed through for UI visibility
    MOVIES_WATCH_LABEL: str = ""
    TV_WATCH_LABEL: str = ""
    MOVIES_OUTPUT_LABEL: str = ""
    TV_OUTPUT_LABEL: str = ""
    MEDIA_TEMP_LABEL: str = ""

    # Compression tiers (size-based preset/CRF overrides)
    COMPRESSION_TIERS_ENABLED: str = "false"
    COMPRESSION_TIERS: str = ""  # JSON array of tier objects

    def __getattribute__(self, name):
        """For DB-backed settings, check PostgreSQL first before env/defaults."""
        value = super().__getattribute__(name)
        if name in DB_BACKED_SETTINGS:
            try:
                from .database import get_setting as _db_get
                db_val = _db_get(name)
                if db_val is not None:
                    # Coerce DB string to match the pydantic field type
                    return _coerce_db_value(name, db_val, value)
            except Exception:
                pass  # DB not ready yet, use env/default
        return value

    # Handle empty strings from .env files
    @validator("RADARR_TIMEOUT_S", "SONARR_TIMEOUT_S", pre=True, always=True)
    def empty_str_to_none_float(cls, v):
        if v == "" or v is None:
            return None
        return v

    @validator("WATCH_DEBOUNCE_SEC", pre=True, always=True)
    def empty_str_to_default_debounce(cls, v):
        if v == "" or v is None:
            return 20.0
        return float(v)

    @validator("FFSUBSYNC_MAX_OFFSET", pre=True, always=True)
    def empty_str_to_default_float(cls, v):
        if v == "" or v is None:
            return 0.5  # default
        return v

    @validator("POSTGRES_PORT", pre=True, always=True)
    def empty_str_to_default_port(cls, v):
        if v == "" or v is None:
            return 5432  # default
        return v

    @validator("MANUAL_WORKERS", pre=True, always=True)
    def empty_str_to_default_manual_workers(cls, v):
        if v == "" or v is None:
            return 0  # default
        return int(v)

    @validator("AUTO_WORKERS", pre=True, always=True)
    def empty_str_to_default_auto_workers(cls, v):
        if v == "" or v is None:
            return 2  # default
        return int(v)

    @validator("UI_REQUIRES_LOGIN", "CLUSTER_DISPATCH_ENABLED", pre=True, always=True)
    def empty_str_to_false_bool(cls, v):
        if v == "" or v is None:
            return False
        if isinstance(v, str):
            return v.lower() in ("true", "1", "yes")
        return v

    class Config:
        env_file = str(_ENV_FILE)


def get_setting(key: str, default=None):
    """
    Get a setting value with database priority.

    Priority: Database -> Environment -> Default

    For DB_BACKED_SETTINGS keys, tries database first.
    For infrastructure keys (POSTGRES_*, WATCH_FOLDER, etc.), uses env only.
    """
    # Infrastructure keys never come from DB
    if key not in DB_BACKED_SETTINGS:
        return getattr(Settings(), key, default)

    # Try database first for runtime settings
    try:
        from .database import get_setting as db_get_setting
        db_value = db_get_setting(key)
        if db_value is not None:
            return db_value
    except Exception:
        pass  # Database not available yet, fall back to env

    # Fall back to environment/Settings
    return getattr(Settings(), key, default)


def get_media_paths(s: "Settings | None" = None) -> dict:
    """Return media paths from environment/infrastructure settings."""
    s = s or Settings()
    return {
        "movies_watch":  s.MOVIES_WATCH_PATH,
        "tv_watch":      s.TV_WATCH_PATH,
        "movies_output": s.MOVIES_OUTPUT_PATH,
        "tv_output":     s.TV_OUTPUT_PATH,
    }
