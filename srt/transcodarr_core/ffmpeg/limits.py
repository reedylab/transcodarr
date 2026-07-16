# srt/transcodarr_core/ffmpeg/limits.py
"""
Global concurrency cap for hardware encode sessions.

GPU encoders have hard session ceilings — an Intel iGPU sustains roughly three
concurrent 1080p encodes, and consumer NVIDIA cards are driver-capped (Pascal
allows two, full stop). Unlike software encoding, which simply gets slower as you
pile work on, exceeding these degrades throughput badly or fails outright.

The worker pools can't express this: they're sized for CPU work, they know
nothing about the GPU, and the auto and manual pools draw on the *same* device.
So the cap has to be global and live below the pools, right around the ffmpeg
invocation itself.

Software encodes are never gated here — the CPU degrades gracefully and the pool
sizes already bound it.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from contextlib import contextmanager

from ..config import get_setting

# Hardware encoder -> backend. Mirrors _HW_VIDEO_ENCODER in transcode.py, but keyed
# the other way: the built command is the source of truth for what will actually run
# (the requested backend may have degraded to software).
_ENCODER_BACKEND = {
    "h264_qsv": "qsv", "hevc_qsv": "qsv",
    "h264_vaapi": "vaapi", "hevc_vaapi": "vaapi",
    "h264_nvenc": "nvenc", "hevc_nvenc": "nvenc",
}

_DEFAULT_LIMIT = 2  # conservative: the lowest common ceiling (consumer NVENC)

_cond = threading.Condition()
_in_use = 0


def backend_of_cmd(cmd: list[str]) -> str:
    """Which backend a built ffmpeg command will actually use."""
    for i, arg in enumerate(cmd):
        if arg == "-c:v" and i + 1 < len(cmd):
            return _ENCODER_BACKEND.get(cmd[i + 1], "software")
    return "software"


def hw_limit() -> int:
    """
    Max concurrent hardware encodes.

    HW_MAX_WORKERS wins when set; otherwise fall back to what the device reports
    it can sustain, and finally to a conservative default. Read on every check so
    the cap can be retuned without a restart.
    """
    raw = get_setting("HW_MAX_WORKERS", "")
    if raw:
        try:
            return max(1, int(raw))
        except (TypeError, ValueError):
            logging.warning("[HW-LIMIT] HW_MAX_WORKERS=%r is not an integer — ignoring", raw)

    try:
        from .capabilities import detect_capabilities
        sessions = [
            b["max_sessions"] for b in detect_capabilities()["backends"]
            if b["available"] and b["id"] != "software" and b.get("max_sessions")
        ]
        if sessions:
            # One physical GPU usually backs every hardware backend (QSV and VAAPI
            # share a render node), so take the tightest ceiling rather than the sum.
            return max(1, min(sessions))
    except Exception as e:
        logging.debug("[HW-LIMIT] capability lookup failed: %s", e)

    return _DEFAULT_LIMIT


def in_use() -> int:
    """Hardware slots currently held (for status/UI)."""
    with _cond:
        return _in_use


@contextmanager
def hw_slot(cmd: list[str]):
    """
    Hold a hardware encode slot for the duration of `cmd`, if it uses one.

    Software commands pass straight through. Hardware commands block until a slot
    frees, so an over-sized worker pool queues on the GPU instead of thrashing it.
    """
    global _in_use

    backend = backend_of_cmd(cmd)
    if backend == "software":
        yield
        return

    waited_from = None
    with _cond:
        while _in_use >= hw_limit():
            if waited_from is None:
                waited_from = time.monotonic()
                logging.info("[HW-LIMIT] %s encode waiting — %d/%d slots busy",
                             backend, _in_use, hw_limit())
            # Timed wait so a missed notify can't wedge a job forever.
            _cond.wait(timeout=1.0)
        _in_use += 1
        held = _in_use

    if waited_from is not None:
        logging.info("[HW-LIMIT] %s encode acquired a slot after %.1fs",
                     backend, time.monotonic() - waited_from)
    logging.debug("[HW-LIMIT] %s encode started (%d/%d)", backend, held, hw_limit())

    try:
        yield
    finally:
        with _cond:
            _in_use -= 1
            _cond.notify()
