# src/transcodarr_core/__init__.py
"""
Transcodarr Core Library
------------------------
Core pipeline, subtitle handling, and transcoding helpers.
"""

__version__ = "1.3.0"
from .config import Settings
from .meta import find_meta_json, _load_imdb_from_meta, find_unified_meta, load_unified_meta
from .nfo import write_nfo_from_meta, write_tvshow_nfo_if_missing, write_tvshow_nfo
from .posters import ensure_poster
from .radarr import delete_movie_by_path, update_movie_path
from .sonarr import delete_episode_by_path, update_series_path
from .jellyfin import refresh_library
from .subtitles.select import find_subtitle_file, srt_basic_sanity, list_local_subtitle_candidates, pick_working_sub
from .subtitles.sync import try_autosync_sub, ffsubsync_offset_seconds, try_autosync_until_ok
from .subtitles.fetch import fetch_extra_subs, enrich_episode_ids
from .subtitles.sanitize import sanitize_for_movtext
from .subtitles.extract import strip_image_based_subs, extract_embedded_subtitles
from .ffmpeg.probe import get_duration_seconds, file_needs_transcode, ffprobe_json
from .ffmpeg.transcode import build_ffmpeg_cmd, run_ffmpeg_with_progress, Progress, format_progress_bar, bytes_to_gb, run_ffmpeg
from .pipeline import walk_and_process as core_walk_and_process
from .pipeline import transcode_file as core_transcode_file
from .logging_setup import setup_logging, archive_and_clear_once
from .watcher import start_watchdog
from .database import (
    init_database, get_connection, get_cursor, release_connection, close_pool,
    add_transcode_history, get_transcode_history, get_transcode_history_by_source,
    get_all_transcode_history,
    upsert_movie, get_movie, get_all_movies, delete_movie,
    upsert_tv_show, get_tv_show,
    upsert_tv_episode, get_tv_episode, get_all_tv_episodes, delete_tv_episode,
    upsert_media_metadata, get_media_metadata, get_metadata_by_title,
    set_ignored, remove_ignored, is_ignored, get_all_ignored, get_ignored_paths
)
from .worker_pool import WorkerPoolManager, get_worker_pool, set_worker_pool, cleanup_stale_temp_files, JobStatus
from .metadata import (
    fetch_movie_metadata, fetch_series_metadata, fetch_episode_metadata,
    get_movie_description, get_series_description
)
from .enrich import enrich_media
