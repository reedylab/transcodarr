"""Tests for the global hardware encode concurrency cap."""
import threading
import time
from unittest.mock import patch

import pytest

from transcodarr_core.ffmpeg import limits


@pytest.fixture(autouse=True)
def _reset():
    limits._in_use = 0
    yield
    limits._in_use = 0


SW_CMD = ["ffmpeg", "-i", "in.mkv", "-c:v", "libx264", "out.mp4"]
QSV_CMD = ["ffmpeg", "-i", "in.mkv", "-c:v", "h264_qsv", "out.mp4"]
VAAPI_CMD = ["ffmpeg", "-i", "in.mkv", "-c:v", "hevc_vaapi", "out.mp4"]
NVENC_CMD = ["ffmpeg", "-i", "in.mkv", "-c:v", "h264_nvenc", "out.mp4"]


def test_backend_read_from_built_command():
    """The command is the source of truth — a request may have degraded to software."""
    assert limits.backend_of_cmd(SW_CMD) == "software"
    assert limits.backend_of_cmd(QSV_CMD) == "qsv"
    assert limits.backend_of_cmd(VAAPI_CMD) == "vaapi"
    assert limits.backend_of_cmd(NVENC_CMD) == "nvenc"
    assert limits.backend_of_cmd(["ffmpeg", "-i", "x"]) == "software"


def test_software_is_never_gated():
    """Even at zero slots, software must pass straight through."""
    with patch.object(limits, "hw_limit", return_value=1):
        limits._in_use = 99  # hardware fully saturated
        with limits.hw_slot(SW_CMD):
            pass  # must not block


def test_hw_slot_accounting():
    with patch.object(limits, "hw_limit", return_value=2):
        assert limits.in_use() == 0
        with limits.hw_slot(QSV_CMD):
            assert limits.in_use() == 1
            with limits.hw_slot(VAAPI_CMD):
                assert limits.in_use() == 2
            assert limits.in_use() == 1
        assert limits.in_use() == 0


def test_slot_released_on_exception():
    with patch.object(limits, "hw_limit", return_value=1):
        with pytest.raises(RuntimeError):
            with limits.hw_slot(QSV_CMD):
                raise RuntimeError("encode blew up")
        assert limits.in_use() == 0, "a failed encode must not leak its slot"


def test_third_encode_blocks_until_a_slot_frees():
    """The real point: an over-sized pool queues on the GPU instead of thrashing it."""
    started = threading.Event()
    acquired = threading.Event()

    with patch.object(limits, "hw_limit", return_value=2):
        def worker():
            started.set()
            with limits.hw_slot(QSV_CMD):
                acquired.set()

        with limits.hw_slot(QSV_CMD), limits.hw_slot(VAAPI_CMD):
            assert limits.in_use() == 2
            t = threading.Thread(target=worker, daemon=True)
            t.start()
            started.wait(timeout=2)
            # third encode must NOT get in while both slots are held
            assert not acquired.wait(timeout=1.5), "cap was exceeded"
            assert limits.in_use() == 2

        # a slot freed — the waiter should now proceed
        assert acquired.wait(timeout=5), "waiter never acquired after release"
        t.join(timeout=5)
    assert limits.in_use() == 0


def test_qsv_and_vaapi_share_one_ceiling():
    """They're the same physical device, so they must not each get a full budget."""
    with patch.object(limits, "hw_limit", return_value=1):
        with limits.hw_slot(QSV_CMD):
            blocked = threading.Event()

            def worker():
                with limits.hw_slot(VAAPI_CMD):
                    blocked.set()

            t = threading.Thread(target=worker, daemon=True)
            t.start()
            assert not blocked.wait(timeout=1.0), "VAAPI bypassed the QSV-held slot"
        assert blocked.wait(timeout=5)
        t.join(timeout=5)


# ── limit resolution ────────────────────────────────────────────────────────

def test_setting_wins_over_detection():
    with patch.object(limits, "get_setting", return_value="4"):
        assert limits.hw_limit() == 4


def test_setting_is_floored_at_one():
    with patch.object(limits, "get_setting", return_value="0"):
        assert limits.hw_limit() == 1


def test_garbage_setting_falls_back_to_detection():
    caps = {"backends": [
        {"id": "qsv", "available": True, "max_sessions": 3},
        {"id": "software", "available": True, "max_sessions": None},
    ]}
    with patch.object(limits, "get_setting", return_value="not-a-number"), \
         patch("transcodarr_core.ffmpeg.capabilities.detect_capabilities", return_value=caps):
        assert limits.hw_limit() == 3


def test_detection_takes_tightest_ceiling():
    """One GPU usually backs every backend, so the budget is the min, not the sum."""
    caps = {"backends": [
        {"id": "qsv", "available": True, "max_sessions": 3},
        {"id": "vaapi", "available": True, "max_sessions": 3},
        {"id": "nvenc", "available": True, "max_sessions": 2},
        {"id": "software", "available": True, "max_sessions": None},
    ]}
    with patch.object(limits, "get_setting", return_value=""), \
         patch("transcodarr_core.ffmpeg.capabilities.detect_capabilities", return_value=caps):
        assert limits.hw_limit() == 2


def test_probe_failure_uses_conservative_default():
    with patch.object(limits, "get_setting", return_value=""), \
         patch("transcodarr_core.ffmpeg.capabilities.detect_capabilities",
               side_effect=RuntimeError("probe down")):
        assert limits.hw_limit() == limits._DEFAULT_LIMIT
