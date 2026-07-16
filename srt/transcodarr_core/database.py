# srt/transcodarr_core/database.py
"""
PostgreSQL database for persistent storage of transcode history, media cache, and metadata.
Designed for concurrent multi-process access (Gunicorn workers, background tasks, etc.)
"""
import logging
import threading
import time
from typing import Optional, Dict, List
from contextlib import contextmanager

import json
import psycopg2
from psycopg2 import pool
from psycopg2.extras import RealDictCursor, Json

from .config import Settings

# Connection pool (thread-safe)
_pool: Optional[pool.ThreadedConnectionPool] = None
_pool_lock = threading.Lock()
_schema_initialized = False


def _get_settings():
    """Get database settings."""
    return Settings()


def _get_pool() -> pool.ThreadedConnectionPool:
    """Get or create the connection pool."""
    global _pool

    if _pool is not None:
        return _pool

    with _pool_lock:
        if _pool is not None:
            return _pool

        settings = _get_settings()

        if not settings.POSTGRES_PASSWORD:
            raise RuntimeError(
                "POSTGRES_PASSWORD not set. Please configure PostgreSQL connection in .env:\n"
                "  POSTGRES_HOST=your-postgres-host\n"
                "  POSTGRES_PORT=5432\n"
                "  POSTGRES_DB=transcodarr\n"
                "  POSTGRES_USER=transcodarr\n"
                "  POSTGRES_PASSWORD=your-password"
            )

        logging.info("[DATABASE] Connecting to PostgreSQL at %s:%s/%s",
                     settings.POSTGRES_HOST, settings.POSTGRES_PORT, settings.POSTGRES_DB)

        _pool = pool.ThreadedConnectionPool(
            minconn=2,
            maxconn=20,
            host=settings.POSTGRES_HOST,
            port=settings.POSTGRES_PORT,
            dbname=settings.POSTGRES_DB,
            user=settings.POSTGRES_USER,
            password=settings.POSTGRES_PASSWORD,
            connect_timeout=30,
        )

        logging.info("[DATABASE] PostgreSQL connection pool created")
        return _pool


def get_connection():
    """Get a connection from the pool."""
    _ensure_initialized()
    return _get_pool().getconn()


def release_connection(conn):
    """Return a connection to the pool."""
    if _pool is not None and conn is not None:
        _get_pool().putconn(conn)


@contextmanager
def get_cursor():
    """Context manager for database cursor with auto-commit and connection release."""
    conn = get_connection()
    try:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        try:
            yield cursor
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            cursor.close()
    finally:
        release_connection(conn)


def _ensure_initialized():
    """Ensure database schema is created."""
    global _schema_initialized

    if _schema_initialized:
        return

    with _pool_lock:
        if _schema_initialized:
            return

        # Get a direct connection for schema creation
        settings = _get_settings()

        if not settings.POSTGRES_PASSWORD:
            raise RuntimeError("POSTGRES_PASSWORD not configured")

        conn = psycopg2.connect(
            host=settings.POSTGRES_HOST,
            port=settings.POSTGRES_PORT,
            dbname=settings.POSTGRES_DB,
            user=settings.POSTGRES_USER,
            password=settings.POSTGRES_PASSWORD,
            connect_timeout=30,
        )

        try:
            cursor = conn.cursor()

            # Check if schema exists
            cursor.execute("""
                SELECT EXISTS (
                    SELECT FROM information_schema.tables
                    WHERE table_name = 'transcode_history'
                )
            """)
            exists = cursor.fetchone()[0]

            if not exists:
                logging.info("[DATABASE] Creating schema...")
                _create_schema(conn)
                logging.info("[DATABASE] Schema created successfully")
            else:
                logging.info("[DATABASE] Schema already exists, checking for migrations...")
                _run_migrations(conn)

            cursor.close()
            conn.commit()
        finally:
            conn.close()

        _schema_initialized = True


def init_database():
    """Public function to initialize database. Safe to call multiple times."""
    _ensure_initialized()


def _run_migrations(conn):
    """Run any necessary migrations for existing databases."""
    cursor = conn.cursor()

    # Migration: Add media_ignore table if it doesn't exist
    cursor.execute("""
        SELECT EXISTS (
            SELECT FROM information_schema.tables
            WHERE table_name = 'media_ignore'
        )
    """)
    if not cursor.fetchone()[0]:
        logging.info("[DATABASE] Migration: Creating media_ignore table...")
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS media_ignore (
                id SERIAL PRIMARY KEY,
                file_path TEXT UNIQUE NOT NULL,
                ignored_at DOUBLE PRECISION DEFAULT EXTRACT(EPOCH FROM NOW()),
                reason TEXT
            )
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_ignore_path ON media_ignore(file_path)")
        logging.info("[DATABASE] Migration: media_ignore table created")

    # Migration: Add episode_count column to tv_episodes if it doesn't exist
    cursor.execute("""
        SELECT EXISTS (
            SELECT FROM information_schema.columns
            WHERE table_name = 'tv_episodes' AND column_name = 'episode_count'
        )
    """)
    if not cursor.fetchone()[0]:
        logging.info("[DATABASE] Migration: Adding episode_count column to tv_episodes...")
        cursor.execute("ALTER TABLE tv_episodes ADD COLUMN episode_count INTEGER DEFAULT 1")
        logging.info("[DATABASE] Migration: episode_count column added")

    # Migration: Add settings table if it doesn't exist
    cursor.execute("""
        SELECT EXISTS (
            SELECT FROM information_schema.tables
            WHERE table_name = 'settings'
        )
    """)
    if not cursor.fetchone()[0]:
        logging.info("[DATABASE] Migration: Creating settings table...")
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                id SERIAL PRIMARY KEY,
                key TEXT UNIQUE NOT NULL,
                value TEXT,
                description TEXT,
                created_at DOUBLE PRECISION DEFAULT EXTRACT(EPOCH FROM NOW()),
                updated_at DOUBLE PRECISION DEFAULT EXTRACT(EPOCH FROM NOW())
            )
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_settings_key ON settings(key)")
        logging.info("[DATABASE] Migration: settings table created")

    # Migration: Add storage_history table if it doesn't exist
    cursor.execute("""
        SELECT EXISTS (
            SELECT FROM information_schema.tables
            WHERE table_name = 'storage_history'
        )
    """)
    if not cursor.fetchone()[0]:
        logging.info("[DATABASE] Migration: Creating storage_history table...")
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS storage_history (
                id SERIAL PRIMARY KEY,
                recorded_at DOUBLE PRECISION DEFAULT EXTRACT(EPOCH FROM NOW()),
                total_bytes BIGINT NOT NULL,
                used_bytes BIGINT NOT NULL,
                free_bytes BIGINT NOT NULL
            )
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_storage_history_time ON storage_history(recorded_at)")
        logging.info("[DATABASE] Migration: storage_history table created")

    # Migration: Add encoding_presets table if it doesn't exist
    cursor.execute("""
        SELECT EXISTS (
            SELECT FROM information_schema.tables
            WHERE table_name = 'encoding_presets'
        )
    """)
    if not cursor.fetchone()[0]:
        logging.info("[DATABASE] Migration: Creating encoding_presets table...")
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS encoding_presets (
                id SERIAL PRIMARY KEY,
                name TEXT UNIQUE NOT NULL,
                is_default BOOLEAN DEFAULT FALSE,
                settings JSONB NOT NULL,
                created_at DOUBLE PRECISION DEFAULT EXTRACT(EPOCH FROM NOW()),
                updated_at DOUBLE PRECISION DEFAULT EXTRACT(EPOCH FROM NOW())
            )
        """)
        logging.info("[DATABASE] Migration: encoding_presets table created")
        _seed_default_presets(cursor)

    # Migration: Add auto_rules column to encoding_presets if missing
    cursor.execute("""
        SELECT EXISTS (
            SELECT FROM information_schema.columns
            WHERE table_name = 'encoding_presets' AND column_name = 'auto_rules'
        )
    """)
    if not cursor.fetchone()[0]:
        logging.info("[DATABASE] Migration: Adding auto_rules column to encoding_presets...")
        cursor.execute("ALTER TABLE encoding_presets ADD COLUMN auto_rules JSONB DEFAULT NULL")
        logging.info("[DATABASE] Migration: auto_rules column added")

    # Migration: Seed Auto preset if missing
    cursor.execute("SELECT id FROM encoding_presets WHERE name = 'Auto'")
    if not cursor.fetchone():
        logging.info("[DATABASE] Migration: Seeding Auto preset...")
        _seed_auto_preset(cursor)

    # Migration: Rename X264_THREADS → ENCODER_THREADS (threads now apply to all codecs).
    # Idempotent: only runs if the old row exists and the new one doesn't.
    cursor.execute("""
        INSERT INTO settings (key, value)
        SELECT 'ENCODER_THREADS', value FROM settings WHERE key = 'X264_THREADS'
        ON CONFLICT (key) DO NOTHING
    """)
    if cursor.rowcount:
        logging.info("[DATABASE] Migration: Copied X264_THREADS → ENCODER_THREADS")
    # Rewrite the same key inside each preset's settings JSON. PostgreSQL JSONB
    # functions handle this cleanly; only touch rows that actually need it.
    cursor.execute("""
        UPDATE encoding_presets
        SET settings = (settings - 'X264_THREADS')
                       || jsonb_build_object('ENCODER_THREADS', settings->'X264_THREADS')
        WHERE settings ? 'X264_THREADS' AND NOT (settings ? 'ENCODER_THREADS')
    """)
    if cursor.rowcount:
        logging.info("[DATABASE] Migration: Renamed X264_THREADS in %d preset(s)", cursor.rowcount)

    # Migration: Index transcode_history.source_path for fast circuit-breaker lookups.
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_transcode_source ON transcode_history(source_path)")

    # Migration: Backfill HW_BACKEND on presets that predate the key.
    # Without it those presets carry no backend, so the encoder falls through to
    # the global setting and the UI leaves whatever the previously-selected preset
    # had in the field — a software preset can silently inherit hardware. Every
    # preset must state its own backend.
    cursor.execute("""
        UPDATE encoding_presets
           SET settings = settings || '{"HW_BACKEND": "software"}'::jsonb
         WHERE settings IS NOT NULL AND NOT (settings ? 'HW_BACKEND')
    """)
    if cursor.rowcount:
        logging.info("[DATABASE] Migration: Backfilled HW_BACKEND=software on %d preset(s)", cursor.rowcount)

    # Migration: Rename the earlier single generic "4K Downscale (Hardware)" preset
    # to its backend-specific name, so it matches the per-backend scheme. Keyed off
    # the backend it was pinned to; renaming preserves any Auto-rule references by id.
    cursor.execute("SELECT id, settings->>'HW_BACKEND' FROM encoding_presets WHERE name = %s",
                   ("4K Downscale (Hardware)",))
    _legacy = cursor.fetchone()
    if _legacy:
        _lid, _lbe = _legacy
        _new = _hardware_preset(_lbe)["name"] if _lbe and _lbe != "software" else None
        cursor.execute("SELECT 1 FROM encoding_presets WHERE name = %s", (_new,)) if _new else None
        if _new and not cursor.fetchone():
            cursor.execute("UPDATE encoding_presets SET name = %s WHERE id = %s", (_new, _lid))
            logging.info("[DATABASE] Migration: Renamed '4K Downscale (Hardware)' -> %r", _new)
        else:
            # backend-specific one already exists (or no backend) — drop the legacy dup
            cursor.execute("DELETE FROM encoding_presets WHERE id = %s AND is_default = TRUE", (_lid,))
            logging.info("[DATABASE] Migration: Removed duplicate legacy hardware preset")

    # Migration: Ensure a hardware preset exists for every detected backend. Seeding
    # only runs on an empty table, so existing installs get their hardware presets
    # here. Additive; skipped on GPU-less hosts (a dead vendor preset is worse than
    # none).
    _added = _ensure_hardware_presets(cursor)
    if _added:
        logging.info("[DATABASE] Migration: Added %d hardware preset(s)", _added)

    cursor.close()
    conn.commit()


def _create_schema(conn):
    """Create all tables."""
    cursor = conn.cursor()

    # Transcode history
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS transcode_history (
            id SERIAL PRIMARY KEY,
            output_path TEXT UNIQUE NOT NULL,
            source_path TEXT,
            source_size BIGINT,
            processed_at DOUBLE PRECISION,
            processing_duration DOUBLE PRECISION,
            copied BOOLEAN DEFAULT FALSE,
            created_at DOUBLE PRECISION DEFAULT EXTRACT(EPOCH FROM NOW())
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_transcode_output ON transcode_history(output_path)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_transcode_source ON transcode_history(source_path)")

    # Movies cache
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS movies (
            id SERIAL PRIMARY KEY,
            path TEXT UNIQUE NOT NULL,
            title TEXT,
            year INTEGER,
            imdb_id TEXT,
            tmdb_id TEXT,
            size_gb DOUBLE PRECISION,
            runtime_min DOUBLE PRECISION,
            container TEXT,
            vcodec TEXT,
            acodec TEXT,
            resolution TEXT,
            video_bitrate INTEGER,
            audio_bitrate INTEGER,
            total_bitrate INTEGER,
            frame_rate DOUBLE PRECISION,
            audio_channels INTEGER,
            audio_sample_rate INTEGER,
            mtime BIGINT,
            status TEXT DEFAULT 'ready',
            processed_at DOUBLE PRECISION,
            processing_duration DOUBLE PRECISION,
            source_size BIGINT,
            compression_ratio DOUBLE PRECISION,
            last_scanned DOUBLE PRECISION DEFAULT EXTRACT(EPOCH FROM NOW())
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_movies_path ON movies(path)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_movies_imdb ON movies(imdb_id)")

    # TV Shows (series-level)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS tv_shows (
            id SERIAL PRIMARY KEY,
            path TEXT UNIQUE NOT NULL,
            title TEXT,
            imdb_id TEXT,
            tvdb_id TEXT,
            tmdb_id TEXT,
            status TEXT,
            last_scanned DOUBLE PRECISION DEFAULT EXTRACT(EPOCH FROM NOW())
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_shows_path ON tv_shows(path)")

    # TV Episodes cache
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS tv_episodes (
            id SERIAL PRIMARY KEY,
            path TEXT UNIQUE NOT NULL,
            show_path TEXT,
            show TEXT,
            season INTEGER,
            episode INTEGER,
            episode_count INTEGER DEFAULT 1,
            title TEXT,
            size_gb DOUBLE PRECISION,
            runtime_min DOUBLE PRECISION,
            container TEXT,
            vcodec TEXT,
            acodec TEXT,
            resolution TEXT,
            video_bitrate INTEGER,
            audio_bitrate INTEGER,
            total_bitrate INTEGER,
            frame_rate DOUBLE PRECISION,
            audio_channels INTEGER,
            audio_sample_rate INTEGER,
            mtime BIGINT,
            status TEXT DEFAULT 'ready',
            processed_at DOUBLE PRECISION,
            processing_duration DOUBLE PRECISION,
            source_size BIGINT,
            compression_ratio DOUBLE PRECISION,
            last_scanned DOUBLE PRECISION DEFAULT EXTRACT(EPOCH FROM NOW()),
            FOREIGN KEY (show_path) REFERENCES tv_shows(path) ON DELETE SET NULL
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_episodes_path ON tv_episodes(path)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_episodes_show ON tv_episodes(show_path)")

    # Media metadata cache
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS media_metadata (
            id SERIAL PRIMARY KEY,
            media_type TEXT NOT NULL,
            imdb_id TEXT,
            tmdb_id TEXT,
            tvdb_id TEXT,
            title TEXT,
            year INTEGER,
            description TEXT,
            genres TEXT,
            rating DOUBLE PRECISION,
            poster_url TEXT,
            backdrop_url TEXT,
            runtime INTEGER,
            status TEXT,
            network TEXT,
            fetched_at DOUBLE PRECISION DEFAULT EXTRACT(EPOCH FROM NOW()),
            source TEXT,
            UNIQUE(media_type, imdb_id),
            UNIQUE(media_type, tmdb_id)
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_metadata_imdb ON media_metadata(imdb_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_metadata_tmdb ON media_metadata(tmdb_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_metadata_type ON media_metadata(media_type)")

    # Media ignore list (for main loop to skip)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS media_ignore (
            id SERIAL PRIMARY KEY,
            file_path TEXT UNIQUE NOT NULL,
            ignored_at DOUBLE PRECISION DEFAULT EXTRACT(EPOCH FROM NOW()),
            reason TEXT
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_ignore_path ON media_ignore(file_path)")

    # Runtime settings (replaces .env for configurable settings)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            id SERIAL PRIMARY KEY,
            key TEXT UNIQUE NOT NULL,
            value TEXT,
            description TEXT,
            created_at DOUBLE PRECISION DEFAULT EXTRACT(EPOCH FROM NOW()),
            updated_at DOUBLE PRECISION DEFAULT EXTRACT(EPOCH FROM NOW())
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_settings_key ON settings(key)")

    # Storage history (system stats)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS storage_history (
            id SERIAL PRIMARY KEY,
            recorded_at DOUBLE PRECISION DEFAULT EXTRACT(EPOCH FROM NOW()),
            total_bytes BIGINT NOT NULL,
            used_bytes BIGINT NOT NULL,
            free_bytes BIGINT NOT NULL
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_storage_history_time ON storage_history(recorded_at)")

    cursor.close()
    conn.commit()


# ============================================================
# Transcode History Functions
# ============================================================

def add_transcode_history(output_path: str, source_path: str, source_size: int,
                          processing_duration: float = None, copied: bool = False) -> None:
    """Add or update transcode history entry."""
    with get_cursor() as cursor:
        cursor.execute("""
            INSERT INTO transcode_history (output_path, source_path, source_size,
                                           processed_at, processing_duration, copied)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (output_path) DO UPDATE SET
                source_path = EXCLUDED.source_path,
                source_size = EXCLUDED.source_size,
                processed_at = EXCLUDED.processed_at,
                processing_duration = EXCLUDED.processing_duration,
                copied = EXCLUDED.copied
        """, (output_path, source_path, source_size, time.time(), processing_duration, copied))
    logging.debug("[DATABASE] Added transcode history: %s", output_path)


def get_transcode_history(output_path: str) -> Optional[Dict]:
    """Get transcode history for a specific output path."""
    with get_cursor() as cursor:
        cursor.execute("SELECT * FROM transcode_history WHERE output_path = %s", (output_path,))
        row = cursor.fetchone()
        return dict(row) if row else None


def get_transcode_history_by_source(source_path: str) -> Optional[Dict]:
    """Return most-recent transcode-history row for this source path, if any.

    Used by the walk-loop circuit breaker to detect sources that were already
    successfully transcoded but never cleaned up — preventing re-transcode loops.
    """
    with get_cursor() as cursor:
        cursor.execute(
            "SELECT output_path, source_path, source_size, processed_at "
            "FROM transcode_history WHERE source_path = %s "
            "ORDER BY processed_at DESC NULLS LAST LIMIT 1",
            (source_path,),
        )
        row = cursor.fetchone()
        return dict(row) if row else None


def get_all_transcode_history() -> Dict[str, Dict]:
    """Get all transcode history as a dict keyed by output_path."""
    with get_cursor() as cursor:
        cursor.execute("SELECT * FROM transcode_history")
        return {row["output_path"]: dict(row) for row in cursor.fetchall()}


# ============================================================
# Movies Cache Functions
# ============================================================

def upsert_movie(path: str, data: Dict) -> None:
    """Insert or update a movie in the cache."""
    with get_cursor() as cursor:
        cursor.execute("""
            INSERT INTO movies (path, title, year, imdb_id, tmdb_id, size_gb, runtime_min,
                               container, vcodec, acodec, resolution, video_bitrate, audio_bitrate,
                               total_bitrate, frame_rate, audio_channels, audio_sample_rate,
                               mtime, status, processed_at, processing_duration, source_size,
                               compression_ratio, last_scanned)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (path) DO UPDATE SET
                title = EXCLUDED.title,
                year = EXCLUDED.year,
                imdb_id = EXCLUDED.imdb_id,
                tmdb_id = EXCLUDED.tmdb_id,
                size_gb = EXCLUDED.size_gb,
                runtime_min = EXCLUDED.runtime_min,
                container = EXCLUDED.container,
                vcodec = EXCLUDED.vcodec,
                acodec = EXCLUDED.acodec,
                resolution = EXCLUDED.resolution,
                video_bitrate = EXCLUDED.video_bitrate,
                audio_bitrate = EXCLUDED.audio_bitrate,
                total_bitrate = EXCLUDED.total_bitrate,
                frame_rate = EXCLUDED.frame_rate,
                audio_channels = EXCLUDED.audio_channels,
                audio_sample_rate = EXCLUDED.audio_sample_rate,
                mtime = EXCLUDED.mtime,
                status = EXCLUDED.status,
                processed_at = EXCLUDED.processed_at,
                processing_duration = EXCLUDED.processing_duration,
                source_size = EXCLUDED.source_size,
                compression_ratio = EXCLUDED.compression_ratio,
                last_scanned = EXCLUDED.last_scanned
        """, (
            path, data.get("title"), data.get("year"), data.get("imdb_id"), data.get("tmdb_id"),
            data.get("size_gb"), data.get("runtime_min"), data.get("container"),
            data.get("vcodec"), data.get("acodec"), data.get("resolution"),
            data.get("video_bitrate"), data.get("audio_bitrate"), data.get("total_bitrate"),
            data.get("frame_rate"), data.get("audio_channels"), data.get("audio_sample_rate"),
            data.get("mtime"), data.get("status", "ready"), data.get("processed_at"),
            data.get("processing_duration"), data.get("source_size"), data.get("compression_ratio"),
            time.time()
        ))


def get_movie(path: str) -> Optional[Dict]:
    """Get a movie by path."""
    with get_cursor() as cursor:
        cursor.execute("SELECT * FROM movies WHERE path = %s", (path,))
        row = cursor.fetchone()
        return dict(row) if row else None


def get_all_movies() -> List[Dict]:
    """Get all movies from cache."""
    with get_cursor() as cursor:
        cursor.execute("SELECT * FROM movies ORDER BY mtime DESC")
        return [dict(row) for row in cursor.fetchall()]


def delete_movie(path: str) -> None:
    """Delete a movie from cache."""
    with get_cursor() as cursor:
        cursor.execute("DELETE FROM movies WHERE path = %s", (path,))


# ============================================================
# TV Shows Cache Functions
# ============================================================

def upsert_tv_show(path: str, data: Dict) -> None:
    """Insert or update a TV show."""
    with get_cursor() as cursor:
        cursor.execute("""
            INSERT INTO tv_shows (path, title, imdb_id, tvdb_id, tmdb_id, status, last_scanned)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (path) DO UPDATE SET
                title = EXCLUDED.title,
                imdb_id = EXCLUDED.imdb_id,
                tvdb_id = EXCLUDED.tvdb_id,
                tmdb_id = EXCLUDED.tmdb_id,
                status = EXCLUDED.status,
                last_scanned = EXCLUDED.last_scanned
        """, (path, data.get("title"), data.get("imdb_id"), data.get("tvdb_id"),
              data.get("tmdb_id"), data.get("status"), time.time()))


def get_tv_show(path: str) -> Optional[Dict]:
    """Get a TV show by path."""
    with get_cursor() as cursor:
        cursor.execute("SELECT * FROM tv_shows WHERE path = %s", (path,))
        row = cursor.fetchone()
        return dict(row) if row else None


# ============================================================
# TV Episodes Cache Functions
# ============================================================

def upsert_tv_episode(path: str, data: Dict) -> None:
    """Insert or update a TV episode in the cache."""
    with get_cursor() as cursor:
        cursor.execute("""
            INSERT INTO tv_episodes (path, show_path, show, season, episode, title, size_gb,
                                    runtime_min, container, vcodec, acodec, resolution,
                                    video_bitrate, audio_bitrate, total_bitrate, frame_rate,
                                    audio_channels, audio_sample_rate, mtime, status,
                                    processed_at, processing_duration, source_size,
                                    compression_ratio, last_scanned)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (path) DO UPDATE SET
                show_path = EXCLUDED.show_path,
                show = EXCLUDED.show,
                season = EXCLUDED.season,
                episode = EXCLUDED.episode,
                title = EXCLUDED.title,
                size_gb = EXCLUDED.size_gb,
                runtime_min = EXCLUDED.runtime_min,
                container = EXCLUDED.container,
                vcodec = EXCLUDED.vcodec,
                acodec = EXCLUDED.acodec,
                resolution = EXCLUDED.resolution,
                video_bitrate = EXCLUDED.video_bitrate,
                audio_bitrate = EXCLUDED.audio_bitrate,
                total_bitrate = EXCLUDED.total_bitrate,
                frame_rate = EXCLUDED.frame_rate,
                audio_channels = EXCLUDED.audio_channels,
                audio_sample_rate = EXCLUDED.audio_sample_rate,
                mtime = EXCLUDED.mtime,
                status = EXCLUDED.status,
                processed_at = EXCLUDED.processed_at,
                processing_duration = EXCLUDED.processing_duration,
                source_size = EXCLUDED.source_size,
                compression_ratio = EXCLUDED.compression_ratio,
                last_scanned = EXCLUDED.last_scanned
        """, (
            path, data.get("show_path"), data.get("show"), data.get("season"), data.get("episode"),
            data.get("title"), data.get("size_gb"), data.get("runtime_min"), data.get("container"),
            data.get("vcodec"), data.get("acodec"), data.get("resolution"),
            data.get("video_bitrate"), data.get("audio_bitrate"), data.get("total_bitrate"),
            data.get("frame_rate"), data.get("audio_channels"), data.get("audio_sample_rate"),
            data.get("mtime"), data.get("status", "ready"), data.get("processed_at"),
            data.get("processing_duration"), data.get("source_size"), data.get("compression_ratio"),
            time.time()
        ))


def get_tv_episode(path: str) -> Optional[Dict]:
    """Get a TV episode by path."""
    with get_cursor() as cursor:
        cursor.execute("SELECT * FROM tv_episodes WHERE path = %s", (path,))
        row = cursor.fetchone()
        return dict(row) if row else None


def get_all_tv_episodes() -> List[Dict]:
    """Get all TV episodes from cache."""
    with get_cursor() as cursor:
        cursor.execute("SELECT * FROM tv_episodes ORDER BY mtime DESC")
        return [dict(row) for row in cursor.fetchall()]


def delete_tv_episode(path: str) -> None:
    """Delete a TV episode from cache."""
    with get_cursor() as cursor:
        cursor.execute("DELETE FROM tv_episodes WHERE path = %s", (path,))


# ============================================================
# Media Metadata Functions (descriptions, etc.)
# ============================================================

def upsert_media_metadata(media_type: str, data: Dict) -> None:
    """Insert or update media metadata (descriptions, etc.)."""
    with get_cursor() as cursor:
        cursor.execute("""
            INSERT INTO media_metadata (media_type, imdb_id, tmdb_id, tvdb_id, title, year,
                                       description, genres, rating, poster_url, backdrop_url,
                                       runtime, status, network, source, fetched_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (media_type, imdb_id) DO UPDATE SET
                tmdb_id = EXCLUDED.tmdb_id,
                tvdb_id = EXCLUDED.tvdb_id,
                title = EXCLUDED.title,
                year = EXCLUDED.year,
                description = EXCLUDED.description,
                genres = EXCLUDED.genres,
                rating = EXCLUDED.rating,
                poster_url = EXCLUDED.poster_url,
                backdrop_url = EXCLUDED.backdrop_url,
                runtime = EXCLUDED.runtime,
                status = EXCLUDED.status,
                network = EXCLUDED.network,
                source = EXCLUDED.source,
                fetched_at = EXCLUDED.fetched_at
        """, (
            media_type, data.get("imdb_id"), data.get("tmdb_id"), data.get("tvdb_id"),
            data.get("title"), data.get("year"), data.get("description"), data.get("genres"),
            data.get("rating"), data.get("poster_url"), data.get("backdrop_url"),
            data.get("runtime"), data.get("status"), data.get("network"),
            data.get("source", "unknown"), time.time()
        ))


def get_media_metadata(media_type: str, imdb_id: str = None, tmdb_id: str = None) -> Optional[Dict]:
    """Get media metadata by IMDB or TMDB ID."""
    with get_cursor() as cursor:
        if imdb_id:
            cursor.execute(
                "SELECT * FROM media_metadata WHERE media_type = %s AND imdb_id = %s",
                (media_type, imdb_id)
            )
        elif tmdb_id:
            cursor.execute(
                "SELECT * FROM media_metadata WHERE media_type = %s AND tmdb_id = %s",
                (media_type, str(tmdb_id))
            )
        else:
            return None
        row = cursor.fetchone()
        return dict(row) if row else None


def get_metadata_by_title(media_type: str, title: str, year: int = None) -> Optional[Dict]:
    """Get media metadata by title (and optionally year)."""
    with get_cursor() as cursor:
        if year:
            cursor.execute(
                "SELECT * FROM media_metadata WHERE media_type = %s AND title = %s AND year = %s",
                (media_type, title, year)
            )
        else:
            cursor.execute(
                "SELECT * FROM media_metadata WHERE media_type = %s AND title = %s",
                (media_type, title)
            )
        row = cursor.fetchone()
        return dict(row) if row else None


def close_pool():
    """Close the connection pool (for graceful shutdown)."""
    global _pool
    if _pool is not None:
        _pool.closeall()
        _pool = None
        logging.info("[DATABASE] Connection pool closed")


# ============================================================
# Media Ignore Functions
# ============================================================

def set_ignored(file_path: str, reason: str = None) -> None:
    """Mark a file as ignored (main loop will skip it)."""
    with get_cursor() as cursor:
        cursor.execute("""
            INSERT INTO media_ignore (file_path, ignored_at, reason)
            VALUES (%s, %s, %s)
            ON CONFLICT (file_path) DO UPDATE SET
                ignored_at = EXCLUDED.ignored_at,
                reason = EXCLUDED.reason
        """, (file_path, time.time(), reason))
    logging.info("[DATABASE] Marked as ignored: %s", file_path)


def remove_ignored(file_path: str) -> bool:
    """Remove a file from the ignore list. Returns True if removed."""
    with get_cursor() as cursor:
        cursor.execute("DELETE FROM media_ignore WHERE file_path = %s", (file_path,))
        removed = cursor.rowcount > 0
    if removed:
        logging.info("[DATABASE] Removed from ignore list: %s", file_path)
    return removed


def is_ignored(file_path: str) -> bool:
    """Check if a file is on the ignore list."""
    with get_cursor() as cursor:
        cursor.execute("SELECT 1 FROM media_ignore WHERE file_path = %s", (file_path,))
        return cursor.fetchone() is not None


def get_all_ignored() -> List[Dict]:
    """Get all ignored files."""
    with get_cursor() as cursor:
        cursor.execute("SELECT * FROM media_ignore ORDER BY ignored_at DESC")
        return [dict(row) for row in cursor.fetchall()]


def get_ignored_paths() -> set:
    """Get all ignored file paths as a set (for fast lookup)."""
    with get_cursor() as cursor:
        cursor.execute("SELECT file_path FROM media_ignore")
        return {row["file_path"] for row in cursor.fetchall()}


# ============================================================
# Settings Functions (runtime configuration stored in database)
# ============================================================

def get_setting(key: str, default: str = None) -> Optional[str]:
    """
    Get a setting value from the database.
    Returns default if not found.
    """
    try:
        with get_cursor() as cursor:
            cursor.execute("SELECT value FROM settings WHERE key = %s", (key,))
            row = cursor.fetchone()
            if row:
                return row["value"]
            return default
    except Exception as e:
        logging.warning("[DATABASE] Failed to get setting %s: %s", key, e)
        return default


def set_setting(key: str, value: str, description: str = None) -> bool:
    """
    Set a setting value in the database.
    Creates the setting if it doesn't exist, updates if it does.
    Returns True on success.
    """
    try:
        with get_cursor() as cursor:
            cursor.execute("""
                INSERT INTO settings (key, value, description, updated_at)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (key) DO UPDATE SET
                    value = EXCLUDED.value,
                    description = COALESCE(EXCLUDED.description, settings.description),
                    updated_at = EXCLUDED.updated_at
            """, (key, value, description, time.time()))
        logging.debug("[DATABASE] Set setting %s = %s", key, value[:50] if value else None)
        return True
    except Exception as e:
        logging.error("[DATABASE] Failed to set setting %s: %s", key, e)
        return False


def get_all_settings() -> Dict[str, str]:
    """Get all settings as a dict."""
    try:
        with get_cursor() as cursor:
            cursor.execute("SELECT key, value FROM settings")
            return {row["key"]: row["value"] for row in cursor.fetchall()}
    except Exception as e:
        logging.warning("[DATABASE] Failed to get all settings: %s", e)
        return {}


def delete_setting(key: str) -> bool:
    """Delete a setting from the database."""
    try:
        with get_cursor() as cursor:
            cursor.execute("DELETE FROM settings WHERE key = %s", (key,))
            return cursor.rowcount > 0
    except Exception as e:
        logging.error("[DATABASE] Failed to delete setting %s: %s", key, e)
        return False


def bulk_set_settings(settings_dict: Dict[str, str]) -> int:
    """
    Set multiple settings at once.
    Returns number of settings successfully set.
    """
    count = 0
    for key, value in settings_dict.items():
        if set_setting(key, value if value is not None else ""):
            count += 1
    return count


# ============================================================
# Storage History Functions (system stats)
# ============================================================

def insert_storage_snapshot(total_bytes: int, used_bytes: int, free_bytes: int) -> None:
    """Insert a storage usage snapshot."""
    with get_cursor() as cursor:
        cursor.execute("""
            INSERT INTO storage_history (recorded_at, total_bytes, used_bytes, free_bytes)
            VALUES (%s, %s, %s, %s)
        """, (time.time(), total_bytes, used_bytes, free_bytes))


def get_storage_history(since_epoch: float = None) -> List[Dict]:
    """Get storage history, optionally filtered by time. Returns list of dicts ordered by time ASC."""
    with get_cursor() as cursor:
        if since_epoch:
            cursor.execute(
                "SELECT recorded_at, total_bytes, used_bytes, free_bytes FROM storage_history WHERE recorded_at >= %s ORDER BY recorded_at ASC",
                (since_epoch,)
            )
        else:
            cursor.execute(
                "SELECT recorded_at, total_bytes, used_bytes, free_bytes FROM storage_history ORDER BY recorded_at ASC"
            )
        return [dict(row) for row in cursor.fetchall()]


def prune_storage_history(keep_days: int = 90) -> int:
    """Delete storage history rows older than keep_days. Returns number of rows deleted."""
    cutoff = time.time() - (keep_days * 86400)
    with get_cursor() as cursor:
        cursor.execute("DELETE FROM storage_history WHERE recorded_at < %s", (cutoff,))
        return cursor.rowcount


# ============================================================
# Encoding Presets
# ============================================================

_BASE_PRESET_SETTINGS = {
    "VIDEO_STREAM_MODE": "encode",
    "AUDIO_STREAM_MODE": "encode",
    "TARGET_VIDEO_CODEC": "h264",
    "TARGET_AUDIO_CODEC": "aac",
    "TARGET_CONTAINER": ".mp4",
    "TARGET_RESOLUTION": "1920x1080",
    "TARGET_PRESET": "fast",
    "TARGET_PROFILE": "high",
    "TARGET_AUDIO_BITRATE": "448k",
    "TARGET_AUDIO_CHANNELS": "6",
    "TARGET_CRF": "",
    "TARGET_AUDIO_NORMALIZE": "true",
    "TARGET_HDR_MODE": "auto",
    # Encode backend is a property of the quality profile: a "fast" preset can run
    # on the GPU while a "quality" preset stays on x264. Unavailable hardware falls
    # back to software per-job, so this is always safe to set.
    "HW_BACKEND": "software",
    "TARGET_TONEMAP": "auto",
    "FFMPEG_THREADS": "1",
    "ENCODER_THREADS": "4",
    "REQUIRE_SUBTITLES": "true",
}

# Portable software presets — safe to ship to every host. Hardware presets are
# generated per detected backend instead (see _hardware_preset), so they only
# ever appear for hardware the user actually has.
DEFAULT_PRESETS = [
    {"name": "Audio Only", "settings": {**_BASE_PRESET_SETTINGS, "VIDEO_STREAM_MODE": "copy"}},
    {"name": "Remux + Subs", "settings": {**_BASE_PRESET_SETTINGS, "VIDEO_STREAM_MODE": "copy", "AUDIO_STREAM_MODE": "copy"}},
    {"name": "4K Downscale", "settings": {**_BASE_PRESET_SETTINGS, "TARGET_RESOLUTION": "1080p_max"}},
    {"name": "High Quality", "settings": {**_BASE_PRESET_SETTINGS, "TARGET_PRESET": "slow", "TARGET_CRF": "19"}},
]

# Short display names for backend-specific presets.
_HW_PRESET_LABELS = {"qsv": "QSV", "vaapi": "VA-API", "nvenc": "NVENC"}


def _hardware_preset(backend: str) -> dict:
    """
    A "4K Downscale" preset for one hardware backend.

    One per detected backend is seeded, so an Intel host gets both QSV and VA-API,
    while AMD gets VA-API and NVIDIA gets NVENC — every preset that appears is real
    for that machine, which is the whole point (a dead vendor preset is worse than
    no preset).

    TARGET_CRF is explicit ON PURPOSE: "" means CRF 23 to libx264, but hardware
    encoders read it as "no quality target" and drop into a default bitrate mode.
    Measured on a UHD 630, h264_qsv with no quality flag scored SSIM 0.966 in a
    0.11 MB file vs 0.983 / 0.75 MB at global_quality 23 — a real quality loss.
    """
    return {
        "name": f"4K Downscale ({_HW_PRESET_LABELS.get(backend, backend.upper())})",
        "settings": {
            **_BASE_PRESET_SETTINGS,
            "TARGET_RESOLUTION": "1080p_max",
            "HW_BACKEND": backend,
            "TARGET_CRF": "23",
        },
    }


_DEFAULT_AUTO_RULES = {
    "fallback_preset_id": None,  # resolved at seed time to "Audio Only"
    "rules": [
        {
            "name": "4K Content",
            "conditions": {"resolution": "above_1080p", "video_codec": None, "media_type": None},
            "target_preset_id": None,  # resolved to "4K Downscale"
        },
        {
            "name": "Legacy Codecs",
            "conditions": {"resolution": None, "video_codec": ["mpeg2video", "mpeg4", "wmv3", "vc1"], "media_type": None},
            "target_preset_id": None,  # resolved to "4K Downscale"
        },
    ],
}

_AUTO_RULE_PRESET_MAP = {
    "4K Content": "4K Downscale",
    "Legacy Codecs": "4K Downscale",
}

_AUTO_FALLBACK_PRESET = "Audio Only"


def _detected_hw_backends() -> list[str]:
    """Hardware backends this host actually has, best first (empty if none)."""
    try:
        from .ffmpeg.capabilities import available_backends
        return [b for b in available_backends() if b != "software"]
    except Exception as e:
        logging.debug("[DATABASE] hardware probe failed: %s", e)
        return []


def _ensure_hardware_presets(cursor) -> int:
    """
    Create a "4K Downscale (<backend>)" preset for each detected hardware backend
    that doesn't already have one. Idempotent; safe to call from seed, migration
    and Restore Defaults. Returns how many were added.
    """
    added = 0
    for backend in _detected_hw_backends():
        preset = _hardware_preset(backend)
        cursor.execute("SELECT 1 FROM encoding_presets WHERE name = %s", (preset["name"],))
        if cursor.fetchone():
            continue
        cursor.execute(
            "INSERT INTO encoding_presets (name, is_default, settings) VALUES (%s, TRUE, %s)",
            (preset["name"], Json(preset["settings"])),
        )
        added += 1
        logging.info("[DATABASE] Added hardware preset %r", preset["name"])
    return added


def _seed_default_presets(cursor):
    """Insert default presets if table is empty."""
    cursor.execute("SELECT COUNT(*) FROM encoding_presets")
    if cursor.fetchone()[0] > 0:
        return
    for p in DEFAULT_PRESETS:
        cursor.execute(
            "INSERT INTO encoding_presets (name, is_default, settings) VALUES (%s, TRUE, %s)",
            (p["name"], Json(p["settings"])),
        )
    # One hardware preset per detected backend — none on a GPU-less host.
    _ensure_hardware_presets(cursor)
    logging.info("[DATABASE] Seeded %d default presets", len(DEFAULT_PRESETS))
    _seed_auto_preset(cursor)


def _seed_auto_preset(cursor):
    """Insert Auto preset with rules referencing other presets by name."""
    # Look up preset IDs by name
    cursor.execute("SELECT id, name FROM encoding_presets WHERE name IN %s",
                   (tuple(list(_AUTO_RULE_PRESET_MAP.values()) + ["4K Downscale"]),))
    name_to_id = {row[0]: row[1] for row in cursor.fetchall()}
    # Flip: name -> id
    name_to_id = {}
    cursor.execute("SELECT id, name FROM encoding_presets")
    for row in cursor.fetchall():
        name_to_id[row[1]] = row[0]

    import copy
    rules_data = copy.deepcopy(_DEFAULT_AUTO_RULES)
    for rule in rules_data["rules"]:
        target_name = _AUTO_RULE_PRESET_MAP.get(rule["name"])
        if target_name and target_name in name_to_id:
            rule["target_preset_id"] = name_to_id[target_name]
    rules_data["fallback_preset_id"] = name_to_id.get(_AUTO_FALLBACK_PRESET)

    cursor.execute(
        "INSERT INTO encoding_presets (name, is_default, settings, auto_rules) VALUES (%s, TRUE, %s, %s)",
        ("Auto", Json({**_BASE_PRESET_SETTINGS}), Json(rules_data)),
    )
    auto_id = None
    cursor.execute("SELECT id FROM encoding_presets WHERE name = 'Auto'")
    row = cursor.fetchone()
    if row:
        auto_id = row[0]

    # Set Auto as active preset if no active preset is set
    if auto_id:
        cursor.execute("SELECT value FROM settings WHERE key = 'ACTIVE_PRESET_ID'")
        if not cursor.fetchone():
            cursor.execute(
                "INSERT INTO settings (key, value, description) VALUES (%s, %s, %s)",
                ("ACTIVE_PRESET_ID", str(auto_id), "Currently active encoding preset"),
            )
    logging.info("[DATABASE] Seeded Auto preset with default rules")


def get_encoding_presets() -> List[Dict]:
    """Get all encoding presets, defaults first then by name."""
    with get_cursor() as cursor:
        cursor.execute(
            "SELECT id, name, is_default, settings, auto_rules, created_at, updated_at "
            "FROM encoding_presets ORDER BY is_default DESC, name ASC"
        )
        return [dict(row) for row in cursor.fetchall()]


def get_encoding_preset(preset_id: int) -> Optional[Dict]:
    """Get a single preset by ID."""
    with get_cursor() as cursor:
        cursor.execute(
            "SELECT id, name, is_default, settings, auto_rules, created_at, updated_at "
            "FROM encoding_presets WHERE id = %s", (preset_id,)
        )
        row = cursor.fetchone()
        return dict(row) if row else None


def get_auto_preset() -> Optional[Dict]:
    """Get the Auto preset (the one with auto_rules set)."""
    with get_cursor() as cursor:
        cursor.execute(
            "SELECT id, name, is_default, settings, auto_rules, created_at, updated_at "
            "FROM encoding_presets WHERE auto_rules IS NOT NULL LIMIT 1"
        )
        row = cursor.fetchone()
        return dict(row) if row else None


def save_auto_rules(rules_data: dict) -> bool:
    """Update the Auto preset's rules."""
    try:
        with get_cursor() as cursor:
            cursor.execute(
                "UPDATE encoding_presets SET auto_rules = %s, updated_at = %s "
                "WHERE name = 'Auto' AND is_default = TRUE",
                (Json(rules_data), time.time()),
            )
            return cursor.rowcount > 0
    except Exception as e:
        logging.error("[DATABASE] Failed to save auto rules: %s", e)
        return False


def create_encoding_preset(name: str, settings: dict) -> Optional[Dict]:
    """Create a custom encoding preset. Returns the new row or None on duplicate."""
    try:
        with get_cursor() as cursor:
            cursor.execute(
                "INSERT INTO encoding_presets (name, is_default, settings) "
                "VALUES (%s, FALSE, %s) RETURNING id, name, is_default, settings, created_at, updated_at",
                (name, Json(settings)),
            )
            return dict(cursor.fetchone())
    except psycopg2.IntegrityError:
        return None


def update_encoding_preset(preset_id: int, name: str, settings: dict) -> Optional[Dict]:
    """Update a custom preset. Returns None if not found or is a default."""
    with get_cursor() as cursor:
        cursor.execute(
            "UPDATE encoding_presets SET name = %s, settings = %s, updated_at = %s "
            "WHERE id = %s AND is_default = FALSE "
            "RETURNING id, name, is_default, settings, created_at, updated_at",
            (name, Json(settings), time.time(), preset_id),
        )
        row = cursor.fetchone()
        return dict(row) if row else None


def delete_encoding_preset(preset_id: int) -> bool:
    """Delete a custom preset. Returns False if not found or is a default."""
    with get_cursor() as cursor:
        cursor.execute(
            "DELETE FROM encoding_presets WHERE id = %s AND is_default = FALSE",
            (preset_id,),
        )
        return cursor.rowcount > 0


def restore_default_presets() -> int:
    """Re-insert any missing default presets. Returns count inserted."""
    count = 0
    with get_cursor() as cursor:
        for p in DEFAULT_PRESETS:
            cursor.execute("SELECT id FROM encoding_presets WHERE name = %s", (p["name"],))
            if not cursor.fetchone():
                cursor.execute(
                    "INSERT INTO encoding_presets (name, is_default, settings) VALUES (%s, TRUE, %s)",
                    (p["name"], Json(p["settings"])),
                )
                count += 1
        # Restore per-backend hardware presets too (pinned to real detected backends,
        # not the vendor DEFAULT_PRESETS used to hardcode).
        count += _ensure_hardware_presets(cursor)
        # Restore Auto preset if missing
        cursor.execute("SELECT id FROM encoding_presets WHERE name = 'Auto'")
        if not cursor.fetchone():
            _seed_auto_preset(cursor)
            count += 1
    if count:
        logging.info("[DATABASE] Restored %d default encoding presets", count)
    return count


# ============================================================
# Phase 3: bulk media-cache helpers (DB as source of truth, JSON fallback)
# ============================================================

from psycopg2.extras import execute_values

_MOVIE_COLS = ("path", "title", "year", "imdb_id", "tmdb_id", "size_gb", "runtime_min",
               "container", "vcodec", "acodec", "resolution", "video_bitrate", "audio_bitrate",
               "total_bitrate", "frame_rate", "audio_channels", "audio_sample_rate",
               "mtime", "status", "processed_at", "processing_duration", "source_size",
               "compression_ratio", "last_scanned")

_TV_COLS = ("path", "show_path", "show", "season", "episode", "episode_count", "title",
            "size_gb", "runtime_min", "container", "vcodec", "acodec", "resolution",
            "video_bitrate", "audio_bitrate", "total_bitrate", "frame_rate",
            "audio_channels", "audio_sample_rate", "mtime", "status",
            "processed_at", "processing_duration", "source_size", "compression_ratio",
            "last_scanned")


def _movie_row(item: Dict) -> tuple:
    return (
        item.get("path"), item.get("title"), item.get("year"),
        item.get("imdb_id"), item.get("tmdb_id"),
        item.get("size_gb"), item.get("runtime_min"), item.get("container"),
        item.get("vcodec"), item.get("acodec"), item.get("resolution"),
        item.get("video_bitrate"), item.get("audio_bitrate"), item.get("total_bitrate"),
        item.get("frame_rate"), item.get("audio_channels"), item.get("audio_sample_rate"),
        item.get("mtime"), item.get("status", "ready"),
        item.get("processed_at"), item.get("processing_duration"),
        item.get("source_size"), item.get("compression_ratio"),
        time.time(),
    )


def _tv_row(item: Dict) -> tuple:
    season = item.get("season")
    episode = item.get("episode")
    # show_path FKs to tv_shows.path which we don't separately populate from cache scans.
    # Leave NULL; the FK has ON DELETE SET NULL so it stays valid.
    show_path = None
    return (
        item.get("path"), show_path, item.get("show"),
        season, episode, len(item.get("episodes") or []) or 1,
        item.get("title"),
        item.get("size_gb"), item.get("runtime_min"), item.get("container"),
        item.get("vcodec"), item.get("acodec"), item.get("resolution"),
        item.get("video_bitrate"), item.get("audio_bitrate"), item.get("total_bitrate"),
        item.get("frame_rate"), item.get("audio_channels"), item.get("audio_sample_rate"),
        item.get("mtime"), item.get("status", "ready"),
        item.get("processed_at"), item.get("processing_duration"),
        item.get("source_size"), item.get("compression_ratio"),
        time.time(),
    )


def _bulk_upsert(table: str, cols: tuple, rows: List[tuple], chunk: int = 500) -> int:
    """Chunked INSERT ... ON CONFLICT (path) DO UPDATE for the cache tables."""
    if not rows:
        return 0
    update_cols = [c for c in cols if c != "path"]
    update_clause = ", ".join(f"{c} = EXCLUDED.{c}" for c in update_cols)
    sql = f"INSERT INTO {table} ({', '.join(cols)}) VALUES %s ON CONFLICT (path) DO UPDATE SET {update_clause}"
    total = 0
    with get_cursor() as cursor:
        for i in range(0, len(rows), chunk):
            execute_values(cursor, sql, rows[i:i+chunk], page_size=chunk)
            total += cursor.rowcount
    return total


def bulk_upsert_movies(items: List[Dict]) -> int:
    """Write a full scan to the movies cache table in one transaction-friendly batch."""
    rows = [_movie_row(it) for it in items if it.get("path")]
    n = _bulk_upsert("movies", _MOVIE_COLS, rows)
    logging.info("[DATABASE] bulk_upsert_movies: %d rows", n)
    return n


def bulk_upsert_tv_episodes(items: List[Dict]) -> int:
    """Write a full scan to the tv_episodes cache table."""
    rows = [_tv_row(it) for it in items if it.get("path")]
    n = _bulk_upsert("tv_episodes", _TV_COLS, rows)
    logging.info("[DATABASE] bulk_upsert_tv_episodes: %d rows", n)
    return n


def delete_movies_not_in(paths: List[str]) -> int:
    """Drop cache rows whose source files no longer exist."""
    if paths is None:
        return 0
    with get_cursor() as cursor:
        if not paths:
            cursor.execute("DELETE FROM movies")
        else:
            cursor.execute("DELETE FROM movies WHERE path NOT IN %s", (tuple(paths),))
        return cursor.rowcount


def delete_tv_episodes_not_in(paths: List[str]) -> int:
    if paths is None:
        return 0
    with get_cursor() as cursor:
        if not paths:
            cursor.execute("DELETE FROM tv_episodes")
        else:
            cursor.execute("DELETE FROM tv_episodes WHERE path NOT IN %s", (tuple(paths),))
        return cursor.rowcount


def cache_count_movies() -> int:
    with get_cursor() as cursor:
        cursor.execute("SELECT COUNT(*) AS n FROM movies")
        return cursor.fetchone()["n"]


def cache_count_tv_episodes() -> int:
    with get_cursor() as cursor:
        cursor.execute("SELECT COUNT(*) AS n FROM tv_episodes")
        return cursor.fetchone()["n"]
