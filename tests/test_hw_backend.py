"""Tests for hardware encode backend selection and command construction.

The rule these guard: asking for hardware must never fail a job. Every way a
backend can be unusable has to degrade to software instead of raising.
"""
from unittest.mock import patch

import pytest

from transcodarr_core.ffmpeg.transcode import (
    _hw_device_args,
    _hw_upload_filters,
    _hw_video_encoder_args,
    _resolve_backend,
    build_ffmpeg_cmd,
)

_SDR = {"is_hdr": False, "color_transfer": "", "color_primaries": "",
        "pix_fmt": "yuv420p", "height": 1080, "width": 1920}
_HDR = {"is_hdr": True, "color_transfer": "smpte2084", "color_primaries": "bt2020",
        "pix_fmt": "yuv420p10le", "height": 2160, "width": 3840}

_QSV_OK = {"id": "qsv", "available": True, "device": "/dev/dri/renderD128",
           "encoders": {"h264": "h264_qsv", "hevc": "hevc_qsv"}, "reason": None}


def _build(overrides, backend=None, probe=_SDR, out="/tmp/out.mp4"):
    with patch("transcodarr_core.ffmpeg.transcode.detect_hdr", return_value=probe), \
         patch("transcodarr_core.ffmpeg.transcode.get_setting",
               side_effect=lambda key, default=None: default):
        return build_ffmpeg_cmd("/tmp/in.mkv", None, out,
                                settings_override=overrides, backend=backend)


# ── _resolve_backend: every failure mode degrades to software ────────────────

@pytest.mark.parametrize("requested", [None, "", "software", "sw", "none", "SOFTWARE"])
def test_software_requests_resolve_to_software(requested):
    assert _resolve_backend(requested, "h264", "none", "/x.mkv") == ("software", None)


@pytest.mark.parametrize("hdr_action", ["tonemap", "passthrough"])
def test_hdr_falls_back_to_software(hdr_action):
    """HDR stays on software until the hardware tonemap path lands."""
    assert _resolve_backend("qsv", "h264", hdr_action, "/x.mkv") == ("software", None)


def test_unknown_backend_falls_back():
    assert _resolve_backend("magic-gpu", "h264", "none", "/x.mkv") == ("software", None)


def test_codec_without_hw_encoder_falls_back():
    """No hardware AV1 encoder is wired up, so AV1 must stay on software."""
    assert _resolve_backend("qsv", "av1", "none", "/x.mkv") == ("software", None)


def test_unavailable_backend_falls_back():
    unavailable = {"id": "qsv", "available": False, "reason": "no render node",
                   "device": None, "encoders": {}}
    with patch("transcodarr_core.ffmpeg.capabilities.get_backend", return_value=unavailable):
        assert _resolve_backend("qsv", "h264", "none", "/x.mkv") == ("software", None)


def test_host_without_codec_support_falls_back():
    """Driver can't encode this codec even though the backend exists."""
    no_h264 = {"id": "qsv", "available": True, "device": "/dev/dri/renderD128",
               "encoders": {"hevc": "hevc_qsv"}, "reason": None}
    with patch("transcodarr_core.ffmpeg.capabilities.get_backend", return_value=no_h264):
        assert _resolve_backend("qsv", "h264", "none", "/x.mkv") == ("software", None)


def test_probe_failure_falls_back_instead_of_raising():
    """A broken probe must cost speed, not the job."""
    with patch("transcodarr_core.ffmpeg.capabilities.get_backend",
               side_effect=RuntimeError("probe exploded")):
        assert _resolve_backend("qsv", "h264", "none", "/x.mkv") == ("software", None)


def test_available_backend_resolves_with_device():
    with patch("transcodarr_core.ffmpeg.capabilities.get_backend", return_value=_QSV_OK):
        assert _resolve_backend("qsv", "h264", "none", "/x.mkv") == ("qsv", "/dev/dri/renderD128")


# ── device init + upload filters ────────────────────────────────────────────

def test_software_emits_no_device_or_upload_filters():
    assert _hw_device_args("software", None) == []
    assert _hw_upload_filters("software") == []


def test_vaapi_device_args():
    assert _hw_device_args("vaapi", "/dev/dri/renderD128") == \
        ["-vaapi_device", "/dev/dri/renderD128"]


def test_qsv_pins_device_via_vaapi_parent():
    """A bare qsv=hw grabs an arbitrary GPU; the node must be pinned."""
    args = _hw_device_args("qsv", "/dev/dri/renderD129")
    assert "vaapi=va:/dev/dri/renderD129" in args
    assert "qsv=hw@va" in args
    assert args[-2:] == ["-filter_hw_device", "hw"]


def test_nvenc_needs_no_device_or_upload():
    """NVENC consumes software frames directly."""
    assert _hw_device_args("nvenc", "/dev/nvidia0") == []
    assert _hw_upload_filters("nvenc") == []


def test_dri_backends_upload_frames():
    assert _hw_upload_filters("vaapi") == ["format=nv12", "hwupload"]
    assert "hwupload=extra_hw_frames=64" in _hw_upload_filters("qsv")


# ── encoder args per backend ────────────────────────────────────────────────

def test_qsv_args_quality_and_preset():
    args = _hw_video_encoder_args("h264", "qsv", "slow", "high", "23")
    assert args[:2] == ["-c:v", "h264_qsv"]
    assert "-global_quality" in args and "23" in args
    assert "-preset" in args and "slow" in args
    assert "-crf" not in args


def test_qsv_maps_presets_it_lacks():
    """QSV has no ultrafast/superfast."""
    args = _hw_video_encoder_args("h264", "qsv", "ultrafast", "", "")
    assert "veryfast" in args
    assert "ultrafast" not in args


def test_vaapi_uses_qp_and_has_no_preset():
    args = _hw_video_encoder_args("h264", "vaapi", "slow", "high", "23")
    assert args[:2] == ["-c:v", "h264_vaapi"]
    assert "-qp" in args and "23" in args
    assert "-preset" not in args  # VAAPI exposes no preset knob


def test_nvenc_translates_preset_to_p_levels():
    assert "p7" in _hw_video_encoder_args("h264", "nvenc", "veryslow", "", "")
    assert "p1" in _hw_video_encoder_args("h264", "nvenc", "ultrafast", "", "")
    args = _hw_video_encoder_args("h264", "nvenc", "fast", "high", "20")
    assert "-cq" in args and "20" in args
    assert "p4" in args


def test_nvenc_unknown_preset_defaults():
    assert "p4" in _hw_video_encoder_args("h264", "nvenc", "bogus", "", "")


def test_profile_only_applied_to_h264_like_software_path():
    assert "-profile:v" not in _hw_video_encoder_args("hevc", "qsv", "", "high", "")
    assert "-profile:v" in _hw_video_encoder_args("h264", "qsv", "", "high", "")


# ── build_ffmpeg_cmd integration ────────────────────────────────────────────

def test_backend_param_does_not_disturb_software_output():
    """backend=None and backend='software' must be identical."""
    ov = {"TARGET_VIDEO_CODEC": "h264", "TARGET_CRF": "19", "TARGET_PRESET": "slow"}
    assert _build(ov, backend=None) == _build(ov, backend="software")


def test_hw_device_args_precede_input():
    with patch("transcodarr_core.ffmpeg.capabilities.get_backend", return_value=_QSV_OK):
        cmd = _build({"TARGET_VIDEO_CODEC": "h264"}, backend="qsv")
    assert cmd.index("-init_hw_device") < cmd.index("-i"), "device init must come before -i"


def test_hw_cmd_uploads_and_omits_pix_fmt():
    with patch("transcodarr_core.ffmpeg.capabilities.get_backend", return_value=_QSV_OK):
        cmd = _build({"TARGET_VIDEO_CODEC": "h264"}, backend="qsv")
    vf = cmd[cmd.index("-vf") + 1]
    assert vf.endswith("hwupload=extra_hw_frames=64")
    # an explicit -pix_fmt fights the hardware frames context
    assert "-pix_fmt" not in cmd
    assert "h264_qsv" in cmd
    assert "libx264" not in cmd


def test_hw_scaling_still_applied_before_upload():
    with patch("transcodarr_core.ffmpeg.capabilities.get_backend", return_value=_QSV_OK):
        cmd = _build({"TARGET_VIDEO_CODEC": "h264", "TARGET_RESOLUTION": "1280x720"},
                     backend="qsv")
    vf = cmd[cmd.index("-vf") + 1]
    assert vf.startswith("scale=1280:720"), "scale must precede the GPU upload"


def test_hw_request_on_hdr_produces_software_command():
    with patch("transcodarr_core.ffmpeg.capabilities.get_backend", return_value=_QSV_OK):
        cmd = _build({"TARGET_VIDEO_CODEC": "h264"}, backend="qsv", probe=_HDR)
    assert "libx264" in cmd
    assert "h264_qsv" not in cmd
    assert "-init_hw_device" not in cmd
    vf = cmd[cmd.index("-vf") + 1]
    assert "tonemap=hable:desat=0" in vf
    assert "format=yuv420p" in vf  # tonemap chain lands back in 8-bit SDR
    assert "hwupload" not in vf    # nothing should have been staged to the GPU


def test_video_copy_ignores_backend_entirely():
    with patch("transcodarr_core.ffmpeg.capabilities.get_backend", return_value=_QSV_OK):
        cmd = _build({"VIDEO_STREAM_MODE": "copy"}, backend="qsv")
    assert "-c:v" in cmd and cmd[cmd.index("-c:v") + 1] == "copy"
    assert "-init_hw_device" not in cmd
    assert "h264_qsv" not in cmd
