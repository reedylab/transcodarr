# web/routers/system.py
from fastapi import APIRouter, Request, Query
from fastapi.responses import JSONResponse
from pathlib import Path
import os, logging, time, uuid
import psutil

from web.shared_state import (
    _stats_lock, _stats_timestamps, _cpu_history, _ram_history,
    read_log_tail,
)
from transcodarr_core.database import get_storage_history
from transcodarr_core.ffmpeg.capabilities import detect_capabilities

router = APIRouter()


_HW_ENV_KEYS = ("GPU_DEVICE", "RENDER_GID", "VIDEO_GID", "NVIDIA_VISIBLE_DEVICES")


def _hw_env() -> dict:
    """Hardware-passthrough env vars as the container actually sees them.

    Populated by the GPU compose overlays; empty here means the base (GPU-less)
    compose is in use. Read-only surface — the real passthrough is done by the
    overlay's devices/group_add, not these values.
    """
    return {k: os.environ.get(k, "") for k in _HW_ENV_KEYS}


@router.get("/system/capabilities")
def api_system_capabilities(refresh: bool = Query(default=False)):
    """
    Report this node's transcoding backends (hardware + software) and the
    hardware-passthrough env config.

    Cached after first probe; pass ?refresh=1 to re-probe after attaching a GPU.
    """
    # Copy so the cached capability dict isn't mutated with per-request env.
    return {**detect_capabilities(force=refresh), "env": _hw_env()}


@router.get("/system/stats")
def api_system_stats(request: Request):
    """Return current + historical CPU/RAM/disk stats."""
    with _stats_lock:
        timestamps = list(_stats_timestamps)
        cpu = list(_cpu_history)
        ram = list(_ram_history)

    cur_cpu = psutil.cpu_percent(interval=0)
    cur_mem = psutil.virtual_memory()
    output_folder = request.app.state.settings.OUTPUT_FOLDER
    try:
        cur_disk = psutil.disk_usage(output_folder)
        disk_info = {
            "total": cur_disk.total,
            "used": cur_disk.used,
            "free": cur_disk.free,
            "percent": cur_disk.percent,
        }
    except Exception:
        disk_info = None

    return {
        "current": {
            "cpu_percent": cur_cpu,
            "ram_percent": cur_mem.percent,
            "ram_used": cur_mem.used,
            "ram_total": cur_mem.total,
            "disk": disk_info,
        },
        "history": {
            "timestamps": timestamps,
            "cpu": cpu,
            "ram": ram,
        },
    }


@router.get("/system/stats/storage")
def api_storage_history():
    """Return DB-persisted storage history for graphing."""
    rows = get_storage_history()
    return {"history": rows}


@router.get("/logs/tail")
def api_logs_tail(
    request: Request,
    pos: int = Query(default=0),
    inode: str = Query(default=None),
):
    """Offset-based tail with rotation detection."""
    return read_log_tail(request.app.state.log_path, pos=pos, inode=inode)


@router.get("/debug/logging")
def api_debug_logging(request: Request):
    """Diagnostic endpoint to test logging."""
    log_path = request.app.state.log_path
    test_id = str(uuid.uuid4())[:8]
    test_msg = f"[DIAG_TEST_{test_id}] Logging test message"

    handlers_before = []
    for h in logging.root.handlers:
        info = {"type": type(h).__name__, "level": h.level}
        if hasattr(h, 'baseFilename'):
            info["file"] = h.baseFilename
        if hasattr(h, 'stream') and h.stream:
            info["stream"] = str(h.stream)
        handlers_before.append(info)

    file_before = None
    if os.path.exists(log_path):
        file_before = os.path.getsize(log_path)

    logging.info(test_msg)

    for h in logging.root.handlers:
        try:
            h.flush()
            if hasattr(h, 'stream') and h.stream:
                try:
                    os.fsync(h.stream.fileno())
                except Exception:
                    pass
        except Exception:
            pass

    time.sleep(0.1)

    found_in_file = False
    file_after = None
    last_lines = []
    if os.path.exists(log_path):
        file_after = os.path.getsize(log_path)
        try:
            with open(log_path, 'r', errors='replace') as f:
                content = f.read()
                found_in_file = test_id in content
                lines = content.strip().split('\n')
                last_lines = lines[-5:] if len(lines) > 5 else lines
        except Exception as e:
            last_lines = [f"Error reading file: {e}"]

    direct_write_success = False
    direct_write_error = None
    direct_test_msg = f"[DIRECT_WRITE_{test_id}] Direct file write test\n"
    try:
        with open(log_path, 'a') as f:
            f.write(direct_test_msg)
            f.flush()
            os.fsync(f.fileno())
        direct_write_success = True
    except Exception as e:
        direct_write_error = str(e)

    file_after_direct = None
    found_direct = False
    if os.path.exists(log_path):
        file_after_direct = os.path.getsize(log_path)
        try:
            with open(log_path, 'r', errors='replace') as f:
                content = f.read()
                found_direct = f"DIRECT_WRITE_{test_id}" in content
                lines = content.strip().split('\n')
                last_lines = lines[-5:] if len(lines) > 5 else lines
        except Exception:
            pass

    file_stat = None
    try:
        import stat
        st = os.stat(log_path)
        file_stat = {
            "mode": oct(st.st_mode),
            "uid": st.st_uid,
            "gid": st.st_gid,
            "size": st.st_size,
        }
    except Exception as e:
        file_stat = {"error": str(e)}

    return {
        "test_id": test_id,
        "test_message": test_msg,
        "log_path": log_path,
        "log_path_absolute": os.path.abspath(log_path),
        "file_exists": os.path.exists(log_path),
        "file_size_before": file_before,
        "file_size_after": file_after,
        "file_size_after_direct": file_after_direct,
        "found_in_file": found_in_file,
        "direct_write_success": direct_write_success,
        "direct_write_error": direct_write_error,
        "found_direct_write": found_direct,
        "file_stat": file_stat,
        "handlers": handlers_before,
        "last_lines": last_lines,
        "root_logger_level": logging.root.level,
        "working_directory": os.getcwd(),
    }
