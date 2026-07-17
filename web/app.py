# web/app.py
# FastAPI application entry point
import atexit
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.responses import HTMLResponse

from transcodarr_core import Settings
from transcodarr_core.logging_setup import setup_logging, archive_and_clear_once
from transcodarr_core.worker_pool import WorkerPoolManager, set_worker_pool, cleanup_stale_temp_files
from transcodarr_core.pipeline import transcode_file
from env_flag import get_stop_flag, set_stop_flag

from web.shared_state import start_stats_collector, migrate_json_cache_to_db
from web.routers import (
    control, settings, media, transcode, workers,
    subtitles, connections, webhooks, system, events, cluster,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # ── Startup ──
    log_file = os.path.abspath("logs/transcode.log")
    archive_dir = os.path.abspath("logs/archive")

    os.makedirs("logs", exist_ok=True)
    archive_and_clear_once(log_file, archive_dir)
    setup_logging(log_file)

    s = Settings()
    app.state.settings = s
    app.state.log_path = log_file
    app.state.stop_flag_fn = get_stop_flag
    app.state.set_stop_flag_fn = set_stop_flag
    app.state.run_lock_path = "/tmp/transcodarr.run"

    # Validate media paths
    from transcodarr_core.config import get_media_paths
    _mpaths = get_media_paths(s)
    for pname, pval in _mpaths.items():
        if not pval:
            logging.info("[STARTUP] %s = (not configured, watchdog disabled for %s)", pname, pname)
        elif os.path.isdir(pval):
            logging.info("[STARTUP] %s = %s OK", pname, pval)
        else:
            logging.error("[STARTUP] %s = %s does not exist!", pname, pval)

    # Clean up stale temp files
    if s.MEDIA_TEMP_FOLDER:
        try:
            cleaned = cleanup_stale_temp_files(s.MEDIA_TEMP_FOLDER)
            if cleaned > 0:
                logging.info("[STARTUP] Cleaned up %d stale temp files", cleaned)
        except Exception as e:
            logging.warning("[STARTUP] Failed to cleanup temp files: %s", e)

    # Initialize worker pool
    from transcodarr_core.config import get_setting
    try:
        init_mw = int(get_setting("MANUAL_WORKERS", s.MANUAL_WORKERS))
    except (ValueError, TypeError):
        init_mw = s.MANUAL_WORKERS
    try:
        init_aw = int(get_setting("AUTO_WORKERS", s.AUTO_WORKERS))
    except (ValueError, TypeError):
        init_aw = s.AUTO_WORKERS

    worker_pool = WorkerPoolManager(
        manual_workers=init_mw,
        auto_workers=init_aw,
        transcode_fn=transcode_file,
        settings=s,
    )
    worker_pool.start()
    app.state.worker_pool = worker_pool
    set_worker_pool(worker_pool)

    # In node mode, skip the watchdog entirely — a node doesn't discover files, it
    # runs jobs the master hands it — and start the agent that registers with the
    # master and heartbeats.
    _mode = (s.TRANSCODARR_MODE or "master").lower()
    app.state.node_agent = None
    if _mode == "node":
        from transcodarr_core.node_agent import NodeAgent
        agent = NodeAgent(s)
        agent.start()
        app.state.node_agent = agent
        logging.info("[STARTUP] Running in NODE mode — watchdog disabled, agent connecting to master")

    # Auto-start watchdog at boot when AUTO_WORKERS > 0 (master mode only).
    # Mirrors POST /api/start body; tolerates a stale /tmp/transcodarr.run lock
    # left behind if a previous process exited without releasing it.
    if _mode == "master" and init_aw > 0:
        try:
            from threading import Thread
            from web.shared_state import _state, acquire_run_lock, _bg

            try:
                acquire_run_lock(app.state.run_lock_path)
            except FileExistsError:
                logging.warning("[STARTUP] Stale run lock %s found, removing", app.state.run_lock_path)
                os.remove(app.state.run_lock_path)
                acquire_run_lock(app.state.run_lock_path)

            from transcodarr_core.config import get_setting
            debounce_sec = float(get_setting("WATCH_DEBOUNCE_SEC", 20.0))
            t = Thread(
                target=_bg,
                args=(s, get_stop_flag, set_stop_flag, app.state.run_lock_path, debounce_sec),
                daemon=True,
            )
            _state["thread"] = t
            t.start()
            logging.info("[STARTUP] Watchdog auto-started (AUTO_WORKERS=%d, debounce=%.1fs)", init_aw, debounce_sec)
        except Exception as e:
            logging.error("[STARTUP] Failed to auto-start watchdog: %s", e)

    # Master reaper: periodically requeue the in-flight jobs of nodes that have gone
    # offline, so their work isn't stranded. Cheap no-op when no jobs are dispatched
    # (the default until CLUSTER_DISPATCH_ENABLED is set).
    app.state.reaper_stop = None
    if _mode == "master":
        from threading import Event, Thread
        from transcodarr_core import dispatch

        reaper_stop = Event()

        def _reaper():
            while not reaper_stop.wait(10.0):
                try:
                    dispatch.reap_offline()
                except Exception as e:
                    logging.warning("[CLUSTER] reaper pass failed: %s", e)

        app.state.reaper_stop = reaper_stop
        Thread(target=_reaper, daemon=True, name="cluster-reaper").start()

    # Start system stats collector
    start_stats_collector()

    # Phase 3: one-shot JSON → DB cache migration. Idempotent.
    try:
        migrate_json_cache_to_db()
    except Exception as e:
        logging.warning("[STARTUP] JSON→DB migration skipped: %s", e)

    yield

    # ── Shutdown ──
    if getattr(app.state, "reaper_stop", None):
        app.state.reaper_stop.set()
    if getattr(app.state, "node_agent", None):
        app.state.node_agent.stop()
    if worker_pool:
        worker_pool.stop(wait=True)


# ── Create app ──
app = FastAPI(lifespan=lifespan)

# Static files
_web_dir = Path(__file__).parent
app.mount("/static", StaticFiles(directory=_web_dir / "static"), name="static")

# Templates
_templates = Jinja2Templates(directory=_web_dir / "templates")

# Include API routers
app.include_router(control.router, prefix="/api")
app.include_router(settings.router, prefix="/api")
app.include_router(media.router, prefix="/api")
app.include_router(transcode.router, prefix="/api")
app.include_router(workers.router, prefix="/api")
app.include_router(subtitles.router, prefix="/api")
app.include_router(connections.router, prefix="/api")
app.include_router(webhooks.router, prefix="/api")
app.include_router(system.router, prefix="/api")
app.include_router(events.router, prefix="/api")
app.include_router(cluster.router, prefix="/api")


_static_dir = _web_dir / "static"


def _asset_version(name: str) -> str:
    try:
        return str(int((_static_dir / name).stat().st_mtime))
    except Exception:
        return "0"


# ── UI route ──
@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    s = request.app.state.settings
    return _templates.TemplateResponse(
        "ui.html",
        {
            "request": request,
            "api_base": "/api",
            "css_version": _asset_version("style.css"),
            "js_version": _asset_version("ui.js"),
            "ui_boot": {
                "watch": s.WATCH_FOLDER, "output": s.OUTPUT_FOLDER,
                "movies_watch": s.MOVIES_WATCH_LABEL or s.MOVIES_WATCH_PATH,
                "tv_watch": s.TV_WATCH_LABEL or s.TV_WATCH_PATH,
                "movies_output": s.MOVIES_OUTPUT_LABEL or s.MOVIES_OUTPUT_PATH,
                "tv_output": s.TV_OUTPUT_LABEL or s.TV_OUTPUT_PATH,
            },
        },
    )


# ── Health check ──
@app.get("/health")
def health():
    return {"status": "ok"}
