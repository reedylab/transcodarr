# src/transcodarr_core/pipeline.py
from __future__ import annotations
import os, logging, traceback, contextlib, re
import json
from typing import Optional
from pathlib import Path
from typing import Callable
import shutil
import time
import contextlib
from .config import Settings
#from transcodarr_core import load_unified_meta
from subliminal import scan_video, list_subtitles, download_subtitles
from subliminal.extensions import provider_manager
from subliminal.providers.opensubtitlescom import (
    OpenSubtitlesComProvider,
    OpenSubtitlesComError,
    ServiceUnavailable,
    DownloadLimitReached,
)
from transcodarr_core import *
import subprocess
from .nfo import find_nfo_for_video, find_tvshow_nfo, read_nfo_as_meta, write_tvshow_nfo_if_missing

SENTINEL_NAME = ".transcodarr-nosub"   # folder-level sentinel to stop infinite retries
VIDEO_EXTS = (".mp4", ".mkv", ".avi", ".mov")

# ---------- NEW: helpers ----------
_EP_RE = re.compile(r"S\d{1,4}E\d{1,3}(?:[-+]\d{1,3})?", re.IGNORECASE)

def get_ep_code(path: str) -> Optional[str]:
    """
    Extract SxxExx from filename; return None for movies or no match.
    """
    m = _EP_RE.search(os.path.basename(path))
    return m.group(0).upper() if m else None

def verify_output(src_path: str, tmp_out: str, chosen_srt: Optional[str], require_subs: bool = True) -> bool:
    """
    Cheap but strong validation for the transcoded file before promotion.
    - file exists and >=10% of source size
    - has at least one video and one audio stream
    - duration within sane bounds relative to source
    - (optional) has a text subtitle track if we muxed one
    """
    try:
        if not os.path.exists(tmp_out):
            logging.warning("[VERIFY] output file missing")
            return False
        out_size = os.path.getsize(tmp_out)
        src_size = os.path.getsize(src_path)
        # Sliding min-size ratio: large high-bitrate sources compress much more
        # than small ones.  10% for <2 GB, 5% for 2-10 GB, 2% for >10 GB.
        src_gb = src_size / (1024 ** 3)
        if src_gb > 10:
            min_ratio = 0.02
        elif src_gb > 2:
            min_ratio = 0.05
        else:
            min_ratio = 0.10
        if src_size > 0 and out_size < src_size * min_ratio:
            logging.warning(f"[VERIFY] output too small: {out_size / 1024 / 1024:.1f} MB is <{min_ratio*100:.0f}% of source {src_size / 1024 / 1024:.1f} MB")
            return False

        src_info = ffprobe_json(src_path)
        out_info = ffprobe_json(tmp_out)
        if not src_info or not out_info:
            logging.warning("[VERIFY] ffprobe failed")
            return False

        def _count_streams(info, codec_type):
            return sum(1 for s in info.get("streams", []) if s.get("codec_type") == codec_type)

        if _count_streams(out_info, "video") < 1 or _count_streams(out_info, "audio") < 1:
            logging.warning("[VERIFY] missing A/V streams")
            return False

        # duration check (±5%) - use VIDEO stream duration, not container
        # Container duration can be extended by subtitle tracks that run past video end
        def _video_dur(info):
            try:
                # Prefer video stream duration over format duration
                for s in info.get("streams", []):
                    if s.get("codec_type") == "video":
                        # Try stream duration first
                        dur = s.get("duration")
                        if dur:
                            return float(dur), "stream"
                        # Fallback: calculate from frames and frame rate
                        frames = s.get("nb_frames")
                        fps_str = s.get("r_frame_rate", "0/1")
                        if frames and "/" in fps_str:
                            num, den = fps_str.split("/")
                            if int(den) > 0:
                                fps = int(num) / int(den)
                                if fps > 0:
                                    return int(frames) / fps, "frames"
                # Fallback to format duration
                return float(info.get("format", {}).get("duration", "0")), "format"
            except Exception:
                return 0.0, "error"

        sd, sd_src = _video_dur(src_info)
        od, od_src = _video_dur(out_info)

        logging.info(f"[VERIFY] Duration check: src={sd:.2f}s ({sd_src}) out={od:.2f}s ({od_src})")

        if sd > 60 and od > 0:
            delta = abs(od - sd) / sd
            if delta > 0.05:
                logging.warning(f"[VERIFY] Duration mismatch: src={sd:.2f}s out={od:.2f}s delta={delta*100:.1f}%")
                return False
            else:
                logging.info(f"[VERIFY] Duration OK: delta={delta*100:.2f}%")

        if require_subs and chosen_srt:
            if _count_streams(out_info, "subtitle") < 1:
                logging.warning("[VERIFY] expected a subtitle track but none found")
                return False

        return True
    except Exception as e:
        logging.warning(f"[VERIFY] exception: {e}")
        return False

def _prune_empty_dirs(path: str, stop_at: str) -> None:
    """
    Remove `path` if empty, then walk upward removing empty parents
    until reaching `stop_at` (which is never removed).
    Safe when multiple jobs share the same tree.
    """
    try:
        p = Path(path).resolve()
        stop = Path(stop_at).resolve()
        # Bail if path isn't under stop_at
        with contextlib.suppress(ValueError):
            p.relative_to(stop)
        while p != stop:
            with contextlib.suppress(OSError):
                p.rmdir()  # only succeeds if empty
            p = p.parent
    except Exception:
        pass


def _write_initial_progress(progress_file: str, file_path: str, meta: dict, temp_dir: str) -> None:
    """
    Write initial progress file with metadata before transcoding starts.
    Also creates poster in source folder (alongside .meta.json) for UI display.
    """
    kind = (meta.get("kind") or "movie").lower()

    # Build progress data from meta
    progress_data = {
        "status": "processing",
        "progress": 0,
        "started_at": time.time(),
        "updated_at": time.time(),
        "source_file": file_path,
        "kind": kind,
    }

    if kind == "episode":
        series = meta.get("series") or {}
        episodes_list = meta.get("episodes") or []
        episode_titles = meta.get("titles") or []  # from load_unified_meta
        progress_data["show"] = meta.get("series_title") or series.get("title") or ""
        progress_data["season"] = meta.get("season")
        progress_data["episode"] = episodes_list[0] if episodes_list else None
        progress_data["episodes"] = episodes_list if len(episodes_list) > 1 else None
        # For multi-episode, join all titles; otherwise use first_title
        if len(episode_titles) > 1:
            progress_data["title"] = " + ".join(episode_titles)
        else:
            progress_data["title"] = meta.get("first_title") or ""
        poster_meta = series
    else:
        progress_data["title"] = meta.get("title") or Path(file_path).stem
        progress_data["year"] = meta.get("year")
        poster_meta = meta

    # Write progress file
    with open(progress_file, "w", encoding="utf-8") as f:
        json.dump(progress_data, f)
    logging.info("[PROGRESS] Created progress file: %s", progress_file)

    # Create poster in SOURCE folder (where video and .meta.json live)
    try:
        source_folder = Path(file_path).parent
        if kind == "episode":
            # For TV, poster goes in show folder (parent of Season X folder)
            show_folder = source_folder.parent if "season" in source_folder.name.lower() else source_folder
            poster_dir = str(show_folder)
        else:
            # For movies, poster goes in movie folder
            poster_dir = str(source_folder)

        ensure_poster(poster_dir, kind="tv" if kind == "episode" else "movie", meta=poster_meta)
    except Exception as e:
        logging.debug("[PROGRESS] Failed to create early poster: %s", e)


def _extract_embedded_to_dir(file_path: str, dest_dir: str) -> Optional[str]:
    """
    Extract the first text-based subtitle stream to dest_dir/<stem>.srt.
    Unlike extract_embedded_subtitles(), this does NOT mutate the source file
    (no strip_image_based_subs call). Used for re-encode mode.
    """
    try:
        base_name = os.path.splitext(os.path.basename(file_path))[0]
        srt_output = os.path.join(dest_dir, base_name + ".srt")

        probe = subprocess.run([
            "ffprobe", "-v", "error",
            "-select_streams", "s:0",
            "-show_entries", "stream=codec_name",
            "-of", "csv=p=0", file_path
        ], capture_output=True, text=True)

        codec = probe.stdout.strip()
        if not codec:
            logging.info("[RE-ENCODE] No embedded subtitle stream in: %s", file_path)
            return None

        logging.info("[RE-ENCODE] Found embedded subtitle codec: %s", codec)

        if codec not in ("subrip", "ass", "ssa", "webvtt", "mov_text"):
            logging.warning("[RE-ENCODE] Image-based subs (%s), skipping extraction (source unchanged)", codec)
            return None

        process = subprocess.run([
            "ffmpeg", "-y", "-i", file_path,
            "-map", "0:s:0", srt_output
        ], capture_output=True, text=True)

        if process.returncode != 0:
            logging.error("[RE-ENCODE] FFmpeg subtitle extraction failed for %s", file_path)
            logging.error(process.stderr)
            return None

        if os.path.exists(srt_output):
            logging.info("[RE-ENCODE] Extracted embedded subtitles to: %s", srt_output)
            return srt_output

        logging.warning("[RE-ENCODE] Extraction reported success but no file created: %s", srt_output)
        return None
    except Exception as e:
        logging.error("[RE-ENCODE] Subtitle extraction crashed for %s: %s", file_path, e)
        return None


# -------------------------------------------------------------------
# Arr metadata lookup for re-encode fallback
# -------------------------------------------------------------------
_MULTI_EP_RE = re.compile(r"[Ss](\d{1,4})((?:[Ee]\d{1,3})+)", re.IGNORECASE)


def _parse_season_episodes(filename: str):
    """Parse season and ALL episode numbers from filename.
    Returns (season: int|None, episodes: list[int]).
    Handles S03E18E19E20E21, S01E05, etc.
    """
    m = _MULTI_EP_RE.search(filename)
    if m:
        season = int(m.group(1))
        ep_part = m.group(2)  # e.g. "E18E19E20E21"
        episodes = [int(x) for x in re.findall(r"[Ee](\d{1,3})", ep_part)]
        return season, episodes
    # Fallback: try "Season X" in folder name
    return None, []


def _fetch_sonarr_episodes(series_id: int, settings: Settings) -> list:
    """Fetch all episodes for a series from Sonarr API."""
    import requests
    from .config import get_setting
    url = get_setting("SONARR_URL", settings.SONARR_URL)
    api_key = get_setting("SONARR_API_KEY", settings.SONARR_API_KEY)
    if not url or not api_key:
        return []
    try:
        r = requests.get(
            f"{url}/api/v3/episode",
            params={"seriesId": series_id},
            headers={"X-Api-Key": api_key},
            timeout=15,
        )
        r.raise_for_status()
        return r.json()
    except Exception as e:
        logging.warning("[RE-ENCODE] Sonarr episode fetch failed: %s", e)
        return []


def build_meta_json_from_arr(file_path: str, settings: Settings, dest_dir: str) -> str | None:
    """
    Query Radarr/Sonarr APIs to build a full .meta.json (same structure as
    write_meta.sh / write_meta_tv.sh) and write it to dest_dir.
    Returns the path to the written .meta.json, or None on failure.
    """
    try:
        from .config import get_media_paths
        _paths = get_media_paths(settings)
        path_posix = Path(file_path).as_posix()
        is_movie = path_posix.startswith(_paths["movies_watch"]) or path_posix.startswith(_paths["movies_output"])
        is_tv = path_posix.startswith(_paths["tv_watch"]) or path_posix.startswith(_paths["tv_output"])
        base_name = os.path.splitext(os.path.basename(file_path))[0]

        if is_movie:
            from .radarr import _get_all_movies, _normalize_path, _remap_for_radarr
            movie_dir = os.path.dirname(file_path)
            remapped = _remap_for_radarr(movie_dir)
            want = _normalize_path(remapped)
            for m in _get_all_movies():
                if _normalize_path(m.get("path", "")) == want:
                    meta_json = {
                        "kind": "movie",
                        "title": m.get("title"),
                        "year": m.get("year"),
                        "imdb_id": m.get("imdbId"),
                        "tmdb_id": m.get("tmdbId"),
                        "radarr_movie_id": m.get("id"),
                        "genres": m.get("genres") or [],
                        "movie_path": m.get("path"),
                        "file_path": file_path,
                    }
                    out_path = os.path.join(dest_dir, base_name + ".meta.json")
                    with open(out_path, "w", encoding="utf-8") as f:
                        json.dump(meta_json, f, indent=2)
                    logging.info("[RE-ENCODE] Built .meta.json from Radarr: %s (%s) imdb=%s -> %s",
                                 meta_json["title"], meta_json.get("year"), meta_json.get("imdb_id"), out_path)
                    return out_path
            logging.info("[RE-ENCODE] Radarr lookup: no match for %s", movie_dir)

        elif is_tv:
            from .sonarr import _get_all_series, _normalize_path as _snorm, _remap_for_sonarr
            remapped = _remap_for_sonarr(file_path)
            normalized = _snorm(remapped)
            # Series folder name ("/output/tv/South Park/Season 21/X.mp4" -> "south park")
            series_folder_name = Path(file_path).parent.parent.name.lower()
            if Path(file_path).parent.name.lower() == series_folder_name:
                # Flat layout: no Season subfolder
                series_folder_name = Path(file_path).parent.name.lower()
            all_series = _get_all_series()
            match = None
            for s in all_series:
                if normalized.startswith(_snorm(s.get("path", ""))):
                    match = s
                    break
            if not match:
                # Fallback: match by series folder name (handles ongoing series whose
                # Sonarr path still points at the watch folder after transcode).
                for s in all_series:
                    if Path(s.get("path") or "").name.lower() == series_folder_name:
                        match = s
                        break
            if match:
                s = match
                series_id = s.get("id")
                # Parse season/episodes from filename (handles multi-ep)
                season_num, episode_nums = _parse_season_episodes(os.path.basename(file_path))
                if not season_num:
                    # Try folder name
                    parent_name = Path(file_path).parent.name
                    season_match = re.search(r"[Ss]eason\s*(\d+)", parent_name)
                    if season_match:
                        season_num = int(season_match.group(1))

                # Fetch per-episode data from Sonarr for titles and IDs
                ep_titles = []
                ep_imdb_ids = []
                ep_tvdb_ids = []
                ep_tmdb_ids = []
                first_imdb_id = None

                if series_id and season_num and episode_nums:
                    all_episodes = _fetch_sonarr_episodes(series_id, settings)
                    ep_by_num = {}
                    for ep in all_episodes:
                        if ep.get("seasonNumber") == season_num:
                            ep_by_num[ep.get("episodeNumber")] = ep

                    for ep_num in episode_nums:
                        ep_data = ep_by_num.get(ep_num, {})
                        ep_titles.append(ep_data.get("title") or "")
                        ep_imdb_ids.append(ep_data.get("imdbId") or "")
                        ep_tvdb_ids.append(ep_data.get("tvdbId") or "")
                        ep_tmdb_ids.append(ep_data.get("tmdbId") or "")
                        if not first_imdb_id and ep_data.get("imdbId"):
                            first_imdb_id = ep_data["imdbId"]

                meta_json = {
                    "kind": "episode",
                    "series": {
                        "title": s.get("title"),
                        "path": s.get("path"),
                        "tvdb_id": s.get("tvdbId"),
                        "imdb_id": s.get("imdbId"),
                        "sonarr_series_id": series_id,
                        "genres": s.get("genres") or [],
                    },
                    "episode": {
                        "season": season_num,
                        "episodes": episode_nums,
                        "titles": ep_titles,
                        "ids": {
                            "imdb": [x for x in ep_imdb_ids if x],
                            "tvdb": [x for x in ep_tvdb_ids if x],
                            "tmdb": [x for x in ep_tmdb_ids if x],
                        },
                        "first_imdb_id": first_imdb_id,
                    },
                    "file": {
                        "path": file_path,
                    },
                }
                out_path = os.path.join(dest_dir, base_name + ".meta.json")
                with open(out_path, "w", encoding="utf-8") as f:
                    json.dump(meta_json, f, indent=2)
                logging.info("[RE-ENCODE] Built .meta.json from Sonarr: %s S%sE%s (%d eps) imdb=%s -> %s",
                             s.get("title"), season_num, episode_nums,
                             len(episode_nums), s.get("imdbId"), out_path)
                return out_path
            logging.info("[RE-ENCODE] Sonarr lookup: no match for %s", file_path)

        else:
            logging.info("[RE-ENCODE] Path doesn't match configured watch/output paths, skipping arr lookup: %s", file_path)

    except Exception as e:
        logging.warning("[RE-ENCODE] Arr meta build failed for %s: %s", file_path, e)
        logging.warning(traceback.format_exc())

    return None


def _write_nfo_for_reencode(meta: dict, video_path: str) -> None:
    """
    Write an NFO file next to video_path using metadata from arr lookup.
    Works for both movies and TV episodes.
    """
    try:
        from xml.etree import ElementTree as ET
        from xml.dom import minidom

        kind = (meta.get("kind") or "movie").lower()
        nfo_path = Path(video_path).with_suffix(".nfo")

        if kind == "movie":
            root = ET.Element("movie")
            if meta.get("title"):
                el = ET.SubElement(root, "title")
                el.text = meta["title"]
            if meta.get("year"):
                el = ET.SubElement(root, "year")
                el.text = str(meta["year"])
            if meta.get("imdb_id"):
                uid = ET.SubElement(root, "uniqueid")
                uid.set("type", "imdb")
                uid.set("default", "true")
                uid.text = meta["imdb_id"]
            if meta.get("tmdb_id"):
                uid = ET.SubElement(root, "uniqueid")
                uid.set("type", "tmdb")
                uid.text = str(meta["tmdb_id"])

        elif kind == "episode":
            root = ET.Element("episodedetails")
            if meta.get("series_title"):
                el = ET.SubElement(root, "showtitle")
                el.text = meta["series_title"]
            if meta.get("season") is not None:
                el = ET.SubElement(root, "season")
                el.text = str(meta["season"])
            episodes = meta.get("episodes") or []
            if episodes:
                el = ET.SubElement(root, "episode")
                el.text = str(episodes[0])
            if meta.get("series_imdb_id"):
                uid = ET.SubElement(root, "uniqueid")
                uid.set("type", "imdb:series")
                uid.text = meta["series_imdb_id"]
            if meta.get("series_tvdb_id"):
                uid = ET.SubElement(root, "uniqueid")
                uid.set("type", "tvdb:series")
                uid.text = str(meta["series_tvdb_id"])
        else:
            return

        rough = ET.tostring(root, encoding="utf-8")
        pretty = minidom.parseString(rough).toprettyxml(indent="  ", encoding="utf-8")
        with open(nfo_path, "wb") as f:
            f.write(pretty)
        logging.info("[RE-ENCODE] Wrote NFO: %s", nfo_path)
    except Exception as e:
        logging.warning("[RE-ENCODE] Failed to write NFO for %s: %s", video_path, e)


# -------------------------------------------------------------------
# Core transcode path
# -------------------------------------------------------------------
def transcode_file(file_path: str, settings: Settings):
    try:
        s = settings or Settings()
        from .config import get_media_paths
        _mpaths = get_media_paths(s)
        base_name   = os.path.splitext(os.path.basename(file_path))[0]
        src_dir   = os.path.dirname(file_path)

        # ---------- detect re-encode (source lives inside any output path) ----------
        _src_resolved = os.path.realpath(file_path)
        is_reencode = any(
            _src_resolved.startswith(os.path.realpath(p) + os.sep)
            for p in [_mpaths["movies_output"], _mpaths["tv_output"]]
        )

        if is_reencode:
            logging.info("[RE-ENCODE] Detected re-encode for: %s", file_path)

        # NEW: run enrichment for TV episodes before we pick subtitles
        if not is_reencode:
            try:
                meta = load_unified_meta(file_path) or {}
                if (meta.get("kind") or "").lower() == "episode":
                    enrich_episode_ids(file_path)  # writes back to .meta.json if needed
            except Exception as e:
                logging.debug("[ENRICH] skipped: %s", e)
        else:
            meta = {}

        ep_src = get_ep_code(file_path)  # NEW: remember source ep (None for movies)

        # ---------- path calculation (branched for re-encode) ----------
        temp_root = s.MEDIA_TEMP_FOLDER or "/temp"
        if is_reencode:
            # Determine which output path this file is under
            _media_type = "movies"
            for _okey, _mtype in [("movies_output", "movies"), ("tv_output", "tv")]:
                _oresolved = os.path.realpath(_mpaths[_okey])
                if _src_resolved.startswith(_oresolved + os.sep):
                    relative_dir = os.path.relpath(src_dir, _mpaths[_okey])
                    _media_type = _mtype
                    break
            else:
                relative_dir = os.path.basename(src_dir)
            output_dir = src_dir
            temp_dir = os.path.join(temp_root, "_reencode", _media_type, relative_dir)
        else:
            # Determine output dir from configured media paths
            _fp = file_path.replace(os.sep, "/")
            if _fp.startswith(_mpaths["movies_watch"]):
                relative_dir = os.path.relpath(src_dir, _mpaths["movies_watch"])
                output_dir = os.path.join(_mpaths["movies_output"], relative_dir)
                _media_type = "movies"
            elif _fp.startswith(_mpaths["tv_watch"]):
                relative_dir = os.path.relpath(src_dir, _mpaths["tv_watch"])
                output_dir = os.path.join(_mpaths["tv_output"], relative_dir)
                _media_type = "tv"
            else:
                relative_dir = os.path.basename(src_dir)
                output_dir = os.path.join(_mpaths["movies_output"], relative_dir)
                _media_type = "movies"
                logging.warning("[TRANSCODE] File not under any configured watch path, defaulting to movies output: %s", file_path)
            temp_dir = os.path.join(temp_root, _media_type, relative_dir)

        os.makedirs(temp_dir, exist_ok=True)

        # Copy source video to temp for re-encode so OUTPUT stays clean
        if is_reencode:
            temp_source = os.path.join(temp_dir, os.path.basename(file_path))
            if not os.path.exists(temp_source):
                logging.info("[RE-ENCODE] Copying source to temp: %s -> %s", file_path, temp_source)
                shutil.copy2(file_path, temp_source)
        else:
            temp_source = None

        container     = s.TARGET_CONTAINER or ".mp4"
        tmp_path      = os.path.join(temp_dir, base_name + ".tmp" + container)
        progress_file = os.path.join(temp_dir, base_name + ".progress.json")
        final_path    = os.path.join(output_dir, base_name + container)
        ok_marker     = os.path.join(output_dir, f".{base_name}.ok")

        # ---------- sentinel handling (skip for re-encode) ----------
        sentinel_path = os.path.join(src_dir, SENTINEL_NAME)
        if not is_reencode and os.path.exists(sentinel_path):
            logging.info(f"[SENTINEL] Removing stale sentinel: {sentinel_path}")
            with contextlib.suppress(Exception):
                os.remove(sentinel_path)

        # ---------- thresholds (unchanged) ----------
        try:
            FFSUBSYNC_MAX_OFFSET = float(os.getenv("FFSUBSYNC_MAX_OFFSET", "0.5"))
        except Exception:
            FFSUBSYNC_MAX_OFFSET = 0.5
        MAX_RETRIES     = int(os.getenv("FFSUBSYNC_MAX_RETRIES", "5"))
        MIN_IMPROVEMENT = float(os.getenv("FFSUBSYNC_MIN_IMPROVEMENT", "0.5"))

        # ---------- NFO metadata fallback for re-encode ----------
        # Track whether we have a real .meta.json file (not just NFO-derived dict)
        _reencode_has_meta_json = False
        if is_reencode and not meta:
            # Step 1: Try per-file NFO
            nfo = find_nfo_for_video(file_path)
            if nfo:
                meta = read_nfo_as_meta(nfo)
                logging.info("[RE-ENCODE] Loaded per-file meta from NFO: %s -> %s", nfo, meta.get("kind"))
            # Step 2: Merge series-level IDs from tvshow.nfo
            tvshow_nfo = find_tvshow_nfo(file_path)
            if tvshow_nfo:
                tvshow_meta = read_nfo_as_meta(tvshow_nfo)
                if tvshow_meta:
                    if not meta.get("series_imdb_id") and tvshow_meta.get("series_imdb_id"):
                        meta["series_imdb_id"] = tvshow_meta["series_imdb_id"]
                    if not meta.get("best_imdb_id") and tvshow_meta.get("best_imdb_id"):
                        meta["best_imdb_id"] = tvshow_meta["best_imdb_id"]
                    if not meta.get("series_tvdb_id") and tvshow_meta.get("series_tvdb_id"):
                        meta["series_tvdb_id"] = tvshow_meta["series_tvdb_id"]
                    if not meta.get("series_title") and tvshow_meta.get("series_title"):
                        meta["series_title"] = tvshow_meta["series_title"]
                    logging.info("[RE-ENCODE] Merged tvshow.nfo series IDs: imdb=%s tvdb=%s",
                                 meta.get("series_imdb_id"), meta.get("series_tvdb_id"))
            # Step 3: If meta is still empty, build full .meta.json from Radarr/Sonarr
            if not meta:
                logging.info("[RE-ENCODE] No NFO metadata found, building .meta.json from Radarr/Sonarr...")
                arr_meta_path = build_meta_json_from_arr(file_path, s, temp_dir)
                if arr_meta_path:
                    logging.info("[RE-ENCODE] Built .meta.json in temp: %s", arr_meta_path)
                    meta = load_unified_meta(temp_source) or {}
                    _reencode_has_meta_json = True

        # ---------- pick subtitles ----------
        if is_reencode:
            extracted_srt = _extract_embedded_to_dir(temp_source, temp_dir)
        else:
            extracted_srt = extract_embedded_subtitles(file_path)

        # For re-encode with NFO-only meta (no .meta.json), pass meta_override
        # so fetch_extra_subs can still find IMDB IDs. When we built a real
        # .meta.json (step 3), let the normal file-based path handle it —
        # this preserves full multi-episode logic (titles, per-ep IDs, etc.)
        _meta_override = None
        if is_reencode and meta and not _reencode_has_meta_json:
            _meta_override = meta

        working_video = temp_source if is_reencode else file_path

        # ---------- resolve Auto preset rules ----------
        from .auto_preset import resolve_auto_preset
        settings_override = resolve_auto_preset(working_video, meta)

        chosen_srt = pick_working_sub(
            video_path=working_video,
            initial_srt=extracted_srt or find_subtitle_file(working_video),
            max_offset=FFSUBSYNC_MAX_OFFSET,
            max_retries=MAX_RETRIES,
            min_improvement=MIN_IMPROVEMENT,
            meta_override=_meta_override,
        )
        if not chosen_srt:
            from .config import get_setting
            _req_val = (settings_override or {}).get("REQUIRE_SUBTITLES") or get_setting("REQUIRE_SUBTITLES", "true")
            require_subs = str(_req_val).lower() == "true"
            if require_subs:
                if not is_reencode:
                    with contextlib.suppress(Exception):
                        with open(sentinel_path, "w") as f:
                            f.write("no aligned/sane subs available\n")
                    logging.warning(f"[SENTINEL] Created: {sentinel_path}")
                logging.warning("[SUBPICK] No aligned/sane subtitles available — skipping this title for now.")
                return
            else:
                logging.warning("[SUBPICK] No subtitles available — proceeding without subs (REQUIRE_SUBTITLES=false).")

        # ---------- NEW: ep-code sanity between src & chosen_srt ----------
        ep_sub = get_ep_code(chosen_srt)
        if ep_src and ep_sub and ep_src != ep_sub:
            logging.warning(f"[SANITY] ep mismatch: video={ep_src} sub={ep_sub} -> aborting transcode for safety")
            return

        logging.info(f"[SUBPICK] Final subtitle used: {os.path.basename(chosen_srt)}")

        # ---------- create progress file and poster BEFORE transcoding ----------
        try:
            _write_initial_progress(progress_file, file_path, meta, temp_dir)
        except Exception as e:
            logging.debug("[PROGRESS] Failed to write initial progress file: %s", e)

        # ---------- transcode to temp (NEVER to final) ----------
        # remove any stale tmp
        with contextlib.suppress(Exception):
            if os.path.exists(tmp_path): os.remove(tmp_path)

        ffmpeg_input = temp_source if is_reencode else file_path
        run_ffmpeg(ffmpeg_input, chosen_srt, tmp_path, base_name, s, progress_file,
                   register_path=file_path if is_reencode else "",
                   settings_override=settings_override)

        # ---------- verify tmp BEFORE promotion ----------
        if not verify_output(ffmpeg_input, tmp_path, chosen_srt, require_subs=bool(chosen_srt)):
            logging.warning("[FINALIZE] Verification failed; keeping source and leaving tmp for inspection.")
            return

        # ---------- promote to output ----------
        # Copy to staging file on destination FS, then atomic rename in-place
        logging.info(f"[FINALIZE] Moving to final: {final_path}")
        os.makedirs(output_dir, exist_ok=True)
        staging_path = final_path + ".staging"
        shutil.copy2(tmp_path, staging_path)
        os.replace(staging_path, final_path)
        os.remove(tmp_path)
        logging.info(f"[FINALIZE] Transcoded and moved to: {final_path}")

        # ---------- save transcode metadata to database ----------
        try:
            processing_duration = None
            if os.path.exists(progress_file):
                with open(progress_file, "r", encoding="utf-8") as f:
                    prog_data = json.load(f)
                started_at = prog_data.get("started_at")
                if started_at:
                    processing_duration = time.time() - started_at

            source_size = os.path.getsize(file_path) if os.path.exists(file_path) else None
            add_transcode_history(final_path, file_path, source_size, processing_duration, copied=False)
            logging.info("[HISTORY] Recorded transcode: %s → %s", file_path, final_path)
        except Exception as e:
            logging.warning("[TRANSCODE-META] Failed to write history for %s: %s: %s",
                            file_path, type(e).__name__, e)

        # ---------- cleanup progress file ----------
        with contextlib.suppress(Exception):
            if os.path.exists(progress_file):
                os.remove(progress_file)

        # ---------- post-transcode: re-encode vs normal ----------
        if is_reencode:
            logging.info("[RE-ENCODE] Complete: %s", final_path)
            # Write NFO if missing
            if not find_nfo_for_video(final_path) and meta:
                _write_nfo_for_reencode(meta, final_path)
            # Write tvshow.nfo if TV and missing
            if meta.get("kind") == "episode" and not find_tvshow_nfo(final_path):
                series_dir = Path(final_path).parent.parent if "season" in Path(final_path).parent.name.lower() else Path(final_path).parent
                write_tvshow_nfo_if_missing(
                    str(series_dir),
                    title=meta.get("series_title"),
                    imdb_id=meta.get("series_imdb_id"),
                    tvdb_id=meta.get("series_tvdb_id"),
                    genres=meta.get("genres") or None,
                )
            # Generate poster if missing
            poster_dir = str(Path(final_path).parent)
            if meta.get("kind") == "episode":
                show_dir = Path(final_path).parent.parent if "season" in Path(final_path).parent.name.lower() else Path(final_path).parent
                poster_dir = str(show_dir)
            if not (Path(poster_dir) / "poster.jpg").exists() and meta:
                poster_meta = {}
                if meta.get("kind") == "episode":
                    poster_meta = {"imdb_id": meta.get("series_imdb_id"), "tvdb_id": meta.get("series_tvdb_id")}
                    ensure_poster(poster_dir, kind="tv", meta=poster_meta)
                else:
                    poster_meta = {"imdb_id": meta.get("imdb_id"), "tmdb_id": meta.get("tmdb_id")}
                    ensure_poster(poster_dir, kind="movie", meta=poster_meta)
            # Delete old source file if extension changed (e.g. .mkv → .mp4)
            if os.path.realpath(file_path) != os.path.realpath(final_path) and os.path.exists(file_path):
                logging.info("[RE-ENCODE] Removing old source: %s", file_path)
                with contextlib.suppress(Exception):
                    os.remove(file_path)
            # Clean up temp copy
            if temp_source and os.path.exists(temp_source):
                with contextlib.suppress(Exception):
                    os.remove(temp_source)
            refresh_library()
        else:
            # ---------- write compact NFO next to final ----------
            meta_path = find_meta_json(file_path)
            if meta_path:
                write_nfo_from_meta(meta_path, final_path)

            # ---------- write series root NFO under OUTPUT ----------
            meta_path = find_meta_json(file_path)
            series_root = None
            series_meta = {}

            if meta_path and os.path.exists(meta_path):
                with open(meta_path, "r", encoding="utf-8", errors="replace") as f:
                    raw = json.load(f)
                series_meta = (raw.get("series") or {})
                series_root = Path(final_path).parent.parent
                logging.info("[TVROOT] series_root=%s", series_root)
            else:
                logging.warning("[META] no meta found next to %s for tvshow.nfo/poster", file_path)

            if (meta.get("kind", "").lower() == "episode") and series_root:
                write_tvshow_nfo(str(series_root), series_meta)
                ensure_poster(str(series_root), kind="tv", meta=series_meta)
            else:  # Movie
                movie_dir = os.path.dirname(final_path)
                m_meta = {}
                try:
                    if meta_path and os.path.exists(meta_path):
                        with open(meta_path, "r", encoding="utf-8") as f:
                            raw = json.load(f)
                        m_meta = raw.get("movie") or {
                            "imdb_id": raw.get("imdb_id"),
                            "tmdb_id": (raw.get("tmdb_id") or None),
                            "radarr_movie_id": (raw.get("radarr_movie_id") or None),
                        }
                except Exception:
                    pass
                ensure_poster(movie_dir, kind="movie", meta=m_meta)

            # optional: library refresh
            ok = refresh_library()
            if ok:
                logging.info("[JELLYFIN] Jellyfin refresh successful.")

            # ---------- SAFE cleanup via Radarr/Sonarr ----------
            ep_final = get_ep_code(final_path)
            if os.path.exists(final_path):
                if (ep_src is None) or (ep_final == ep_src):
                    # remove folder-level nosub sentinel if present
                    if os.path.exists(sentinel_path):
                        with contextlib.suppress(Exception):
                            os.remove(sentinel_path)

                    meta = load_unified_meta(file_path)
                    kind = (meta.get("kind") or "").lower()

                    if kind == "movie":
                        logging.info(f"[RADARR] Updating movie path to output: {final_path}")
                        ok = update_movie_path(src_dir, final_path)
                        if ok:
                            logging.info("[RADARR] Path updated successfully. Cleaning up source folder.")
                            with contextlib.suppress(Exception):
                                shutil.rmtree(src_dir)
                                logging.info(f"[CLEANUP] Removed source folder: {src_dir}")
                        else:
                            logging.warning("[RADARR] Path update failed; keeping source.")
                    elif kind == "episode":
                        from .sonarr import get_series_status, delete_episode_by_path

                        status = get_series_status(file_path)
                        if status:
                            series_title = status["title"]
                            is_last_episode = status["ended"] and status["episode_file_count"] == 1

                            logging.info(f"[SONARR] Series '{series_title}': ended={status['ended']}, episode_file_count={status['episode_file_count']} (before delete)")

                            delete_ok = delete_episode_by_path(file_path, delete_files=False)
                            if delete_ok:
                                logging.info(f"[SONARR] Deleted episode file record for: {file_path}")
                            else:
                                logging.warning(f"[SONARR] Failed to delete episode file record: {file_path}")

                            if is_last_episode:
                                logging.info(f"[SONARR] Last episode of ended series - updating path to output")
                                path_ok = update_series_path(file_path, final_path)
                                if path_ok:
                                    logging.info(f"[SONARR] Series path updated to output: {final_path}")
                                else:
                                    logging.warning(f"[SONARR] Failed to update series path")
                        else:
                            logging.warning(f"[SONARR] Could not get series status for: {file_path}")

                        logging.info("[CLEANUP] Removing source episode file.")
                        with contextlib.suppress(Exception):
                            os.remove(file_path)
                            logging.info(f"[CLEANUP] Removed source file: {file_path}")
                        meta_file = Path(file_path).with_suffix(".meta.json")
                        if meta_file.exists():
                            with contextlib.suppress(Exception):
                                os.remove(meta_file)
                        meta_pattern = Path(file_path).parent.glob(f"*{Path(file_path).stem}*.meta.json")
                        for mf in meta_pattern:
                            with contextlib.suppress(Exception):
                                os.remove(mf)
                    else:
                        # Output was produced successfully but we can't classify the
                        # source (no metadata → kind unknown), so cleanup can't safely
                        # remove it. Ignore the source to prevent an infinite
                        # re-transcode loop on every scan. See circuit breaker in
                        # walk_and_process for the same self-healing intent.
                        logging.warning(
                            "[CLEANUP] Unknown kind for %s; output exists at %s. "
                            "Adding source to ignore list to prevent re-transcode loop "
                            "(investigate why metadata enrichment produced no kind).",
                            file_path, final_path,
                        )
                        with contextlib.suppress(Exception):
                            set_ignored(file_path, reason="cleanup: unknown kind (missing metadata); output already produced")
                else:
                    logging.warning(f"[CLEANUP] ep mismatch at finalize (src={ep_src}, final={ep_final}); keeping source.")
            else:
                logging.warning("[CLEANUP] Final file or .ok marker missing; keeping source.")

    except Exception as e:
        logging.error(f"Failed to transcode {file_path}: {e}")
        logging.error(traceback.format_exc())
    finally:
        # Best-effort cleanup of temp artifacts
        with contextlib.suppress(Exception):
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        # Clean up progress file (may have been removed already in happy path)
        with contextlib.suppress(Exception):
            if os.path.exists(progress_file):
                os.remove(progress_file)
        # Clean up extracted SRT and synced variants from temp_dir (re-encode mode)
        with contextlib.suppress(Exception):
            for f in Path(temp_dir).glob(f"{base_name}*.srt"):
                os.remove(f)
        # Clean up temp source copy for re-encode
        if is_reencode and temp_source:
            with contextlib.suppress(Exception):
                if os.path.exists(temp_source):
                    os.remove(temp_source)
                    logging.debug("[RE-ENCODE] Cleaned up temp source copy: %s", temp_source)
        _prune_empty_dirs(temp_dir, temp_root)


# -------------------------------------------------------------------
# Copy compatible file (no transcode needed)
# -------------------------------------------------------------------
def copy_compatible_file(file_path: str, settings: Settings):
    """Copy already-compatible file to output without transcoding."""
    try:
        s = settings or Settings()
        from .config import get_media_paths
        _mpaths = get_media_paths(s)
        base_name = os.path.splitext(os.path.basename(file_path))[0]
        src_dir = os.path.dirname(file_path)

        # Determine output dir from configured media paths
        _fp = file_path.replace(os.sep, "/")
        if _fp.startswith(_mpaths["movies_watch"]):
            relative_dir = os.path.relpath(src_dir, _mpaths["movies_watch"])
            output_dir = os.path.join(_mpaths["movies_output"], relative_dir)
        elif _fp.startswith(_mpaths["tv_watch"]):
            relative_dir = os.path.relpath(src_dir, _mpaths["tv_watch"])
            output_dir = os.path.join(_mpaths["tv_output"], relative_dir)
        else:
            relative_dir = os.path.basename(src_dir)
            output_dir = os.path.join(_mpaths["movies_output"], relative_dir)

        # Load meta for kind detection
        meta = load_unified_meta(file_path) or {}
        kind = (meta.get("kind") or "").lower()
        ep_src = get_ep_code(file_path)
        container = s.TARGET_CONTAINER or ".mp4"
        final_path = os.path.join(output_dir, base_name + container)

        # Skip if already exists in output
        if os.path.exists(final_path):
            logging.info(f"[COPY] Already exists in output: {final_path}")
            return

        logging.info(f"[COPY] Copying compatible file to output: {file_path}")
        os.makedirs(output_dir, exist_ok=True)

        # Copy the file (use shutil.copy2 to preserve metadata)
        source_size = os.path.getsize(file_path) if os.path.exists(file_path) else None
        shutil.copy2(file_path, final_path)
        logging.info(f"[COPY] Copied to: {final_path}")

        # ---------- save to database ----------
        try:
            add_transcode_history(final_path, file_path, source_size, processing_duration=0, copied=True)
            logging.info("[HISTORY] Recorded copy: %s → %s", file_path, final_path)
        except Exception as e:
            logging.warning("[COPY] Failed to write history for %s: %s: %s",
                            file_path, type(e).__name__, e)

        # ---------- write NFO next to final ----------
        meta_path = find_meta_json(file_path)
        if meta_path:
            write_nfo_from_meta(meta_path, final_path)

        # ---------- write series/movie NFO and poster ----------
        series_root = None
        series_meta = {}

        if meta_path and os.path.exists(meta_path):
            with open(meta_path, "r", encoding="utf-8", errors="replace") as f:
                raw = json.load(f)
            series_meta = (raw.get("series") or {})
            series_root = Path(final_path).parent.parent

        if kind == "episode" and series_root:
            write_tvshow_nfo(str(series_root), series_meta)
            ensure_poster(str(series_root), kind="tv", meta=series_meta)
        else:  # Movie
            movie_dir = os.path.dirname(final_path)
            m_meta = {}
            try:
                if meta_path and os.path.exists(meta_path):
                    with open(meta_path, "r", encoding="utf-8") as f:
                        raw = json.load(f)
                    m_meta = raw.get("movie") or {
                        "imdb_id": raw.get("imdb_id"),
                        "tmdb_id": raw.get("tmdb_id"),
                        "radarr_movie_id": raw.get("radarr_movie_id"),
                    }
            except Exception:
                pass
            ensure_poster(movie_dir, kind="movie", meta=m_meta)

        # ---------- library refresh ----------
        ok = refresh_library()
        if ok:
            logging.info("[JELLYFIN] Jellyfin refresh successful.")

        # ---------- cleanup source via Radarr/Sonarr ----------
        ep_final = get_ep_code(final_path)
        if os.path.exists(final_path):
            if (ep_src is None) or (ep_final == ep_src):
                if kind == "movie":
                    logging.info(f"[RADARR] Updating movie path to output: {final_path}")
                    ok = update_movie_path(src_dir, final_path)
                    if ok:
                        logging.info("[RADARR] Path updated successfully. Cleaning up source folder.")
                        with contextlib.suppress(Exception):
                            shutil.rmtree(src_dir)
                            logging.info(f"[CLEANUP] Removed source folder: {src_dir}")
                    else:
                        logging.warning("[RADARR] Path update failed; keeping source.")
                elif kind == "episode":
                    # Get series status before any modifications
                    from .sonarr import get_series_status, delete_episode_by_path

                    status = get_series_status(file_path)
                    if status:
                        series_title = status["title"]
                        is_last_episode = status["ended"] and status["episode_file_count"] == 1

                        logging.info(f"[SONARR] Series '{series_title}': ended={status['ended']}, episode_file_count={status['episode_file_count']} (before delete)")

                        # Delete episode file record from Sonarr (prevents re-download)
                        delete_ok = delete_episode_by_path(file_path, delete_files=False)
                        if delete_ok:
                            logging.info(f"[SONARR] Deleted episode file record for: {file_path}")
                        else:
                            logging.warning(f"[SONARR] Failed to delete episode file record: {file_path}")

                        # Update series path only if this was the last episode of an ended series
                        if is_last_episode:
                            logging.info(f"[SONARR] Last episode of ended series - updating path to output")
                            path_ok = update_series_path(file_path, final_path)
                            if path_ok:
                                logging.info(f"[SONARR] Series path updated to output: {final_path}")
                            else:
                                logging.warning(f"[SONARR] Failed to update series path")
                    else:
                        logging.warning(f"[SONARR] Could not get series status for: {file_path}")

                    # Always clean up source files (Sonarr tracking handled above)
                    logging.info("[CLEANUP] Removing source episode file.")
                    with contextlib.suppress(Exception):
                        os.remove(file_path)
                        logging.info(f"[CLEANUP] Removed source file: {file_path}")
                    # Also remove meta.json sidecar if present
                    meta_file = Path(file_path).with_suffix(".meta.json")
                    if meta_file.exists():
                        with contextlib.suppress(Exception):
                            os.remove(meta_file)
                    # Try cleanup of parent folder with episode code pattern in name
                    meta_pattern = Path(file_path).parent.glob(f"*{Path(file_path).stem}*.meta.json")
                    for mf in meta_pattern:
                        with contextlib.suppress(Exception):
                            os.remove(mf)
                else:
                    # Output produced but source unclassifiable (missing metadata);
                    # ignore it so we don't re-copy on every scan (see finalize path).
                    logging.warning(
                        "[CLEANUP] Unknown kind for %s; output exists at %s. "
                        "Adding source to ignore list to prevent re-process loop "
                        "(investigate why metadata enrichment produced no kind).",
                        file_path, final_path,
                    )
                    with contextlib.suppress(Exception):
                        set_ignored(file_path, reason="cleanup: unknown kind (missing metadata); output already produced")
            else:
                logging.warning(f"[CLEANUP] ep mismatch (src={ep_src}, final={ep_final}); keeping source.")

    except Exception as e:
        logging.error(f"[COPY] Failed to copy {file_path}: {e}")
        logging.error(traceback.format_exc())


# -------------------------------------------------------------------
# Walker (with sentinel skip)
# -------------------------------------------------------------------
def walk_and_process(
    transcode_file_fn: Callable[[str, Settings], None],
    settings: Settings | None = None,
    stop_flag_fn: Callable[[], bool] | None = None
) -> None:
    s = settings or Settings()
    logging.info("Starting walk_and_process()...")

    # Import here to avoid circular imports
    from transcodarr_core.worker_pool import get_worker_pool
    from transcodarr_core.database import is_ignored, get_transcode_history_by_source, set_ignored
    from transcodarr_core.config import get_media_paths, get_setting

    worker_pool = get_worker_pool()
    use_auto_pool = worker_pool and worker_pool.auto_workers > 0

    pending_futures = []

    # Walk configured watch paths (deduplicated, skip missing)
    _mpaths = get_media_paths(s)
    watch_dirs = []
    for p in [_mpaths["movies_watch"], _mpaths["tv_watch"]]:
        if p and os.path.isdir(p) and p not in watch_dirs:
            watch_dirs.append(p)
    if not watch_dirs:
        logging.warning("[WALK] No configured watch paths found. Check MOVIES_WATCH_PATH and TV_WATCH_PATH.")

    for _watch_dir in watch_dirs:
        for root, _, files in os.walk(_watch_dir):
            if stop_flag_fn and stop_flag_fn():
                logging.info("Stop flag detected before directory scan. Exiting...")
                break

            # Skip folder if sentinel exists
            sentinel_path = os.path.join(root, SENTINEL_NAME)
            if os.path.exists(sentinel_path):
                logging.info(f"Skipping folder {root} — sentinel present ({SENTINEL_NAME}).")
                continue

            for name in files:
                if stop_flag_fn and stop_flag_fn():
                    logging.info("Stop flag detected mid-run. Exiting...")
                    break

                if not name.lower().endswith(VIDEO_EXTS):
                    continue

                file_path = os.path.join(root, name)

                # Skip if file is being processed by any pool
                if worker_pool and worker_pool.is_file_processing(file_path):
                    logging.info(f"Skipping {file_path} — already being processed.")
                    continue

                # Skip if file is on the ignore list
                try:
                    if is_ignored(file_path):
                        logging.debug(f"Skipping {file_path} — on ignore list.")
                        continue
                except Exception:
                    pass  # Database might not be available, continue anyway

                # If a final .mp4 already exists next to a non-mp4, skip
                base_name = os.path.splitext(name)[0]
                final_mp4_path = os.path.join(root, base_name + ".mp4")
                if file_path.lower() != final_mp4_path.lower() and os.path.exists(final_mp4_path):
                    logging.info(f"Skipping {file_path} — final .mp4 already exists in this folder.")
                    continue

                # Circuit breaker: if we have a successful transcode history record for this
                # exact source (matching size) and the output still exists, the source should
                # have been cleaned up but wasn't. Add to ignore list to break the loop.
                # Kill-switch: set DEDUP_BY_TRANSCODE_HISTORY=false to disable.
                if get_setting("DEDUP_BY_TRANSCODE_HISTORY", "true") != "false":
                    try:
                        existing = get_transcode_history_by_source(file_path)
                    except Exception:
                        existing = None
                    if existing and existing.get("output_path") and os.path.exists(existing["output_path"]):
                        try:
                            current_size = os.path.getsize(file_path)
                        except OSError:
                            current_size = None
                        if existing.get("source_size") == current_size:
                            logging.error(
                                "[CIRCUIT-BREAKER] Source %s was already transcoded to %s on %s "
                                "but is still in the watch tree. Adding to ignore list to prevent "
                                "re-transcode loop. Investigate why cleanup failed.",
                                file_path, existing["output_path"], existing.get("processed_at"),
                            )
                            try:
                                set_ignored(file_path, reason="circuit-breaker: source not cleaned after successful transcode")
                            except Exception as e:
                                logging.warning("[CIRCUIT-BREAKER] Failed to add to ignore list: %s", e)
                            continue

                # Always transcode to ensure subs + audio normalization
                fn = transcode_file_fn
                fn_args = (file_path, s)
                if file_needs_transcode(file_path):
                    logging.info(f"Needs transcode: {file_path}")
                else:
                    logging.info(f"Already compatible but force-encoding for subs/audio: {file_path}")

                if use_auto_pool:
                    # Wait for auto pool capacity (spin-wait with stop flag check)
                    while not worker_pool.can_accept_auto_job():
                        if stop_flag_fn and stop_flag_fn():
                            logging.info("Stop flag detected while waiting for auto pool capacity.")
                            break
                        time.sleep(1)

                    if stop_flag_fn and stop_flag_fn():
                        break

                    future = worker_pool.submit_auto_job(file_path, fn, *fn_args)
                    if future:
                        pending_futures.append(future)
                    else:
                        # Fallback: run inline if submit failed
                        try:
                            fn(*fn_args)
                        except Exception as e:
                            logging.error(f"[WALK] Unhandled error processing {file_path}: {e}")
                            logging.error(traceback.format_exc())
                            logging.info("[WALK] Continuing to next file...")
                else:
                    # No auto pool — run inline (sequential)
                    try:
                        fn(*fn_args)
                    except Exception as e:
                        logging.error(f"[WALK] Unhandled error processing {file_path}: {e}")
                        logging.error(traceback.format_exc())
                        logging.info("[WALK] Continuing to next file...")

    # Wait for all outstanding auto-pool futures to complete
    if pending_futures:
        logging.info("[WALK] Waiting for %d auto-pool jobs to finish...", len(pending_futures))
        for future in pending_futures:
            try:
                future.result()  # blocks until done
            except Exception as e:
                logging.error("[WALK] Auto-pool job raised: %s", e)

    logging.info("walk_and_process() completed or stopped.")