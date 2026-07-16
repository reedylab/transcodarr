"""Tests for hardware capability detection."""
from unittest.mock import patch

import pytest

from transcodarr_core.ffmpeg import capabilities as caps


# Trimmed to the shape the parser cares about, including the legend rows that
# must NOT be mistaken for encoder names.
FFMPEG_ENCODERS = """Encoders:
 V..... = Video
 A..... = Audio
 ------
 V....D libx264              libx264 H.264 / AVC (codec h264)
 V....D libx265              libx265 H.265 / HEVC (codec hevc)
 V....D libvpx-vp9           libvpx VP9 (codec vp9)
 V....D libsvtav1            SVT-AV1 (codec av1)
 V..... h264_qsv             H.264 (Intel Quick Sync Video acceleration) (codec h264)
 V..... hevc_qsv             HEVC (Intel Quick Sync Video acceleration) (codec hevc)
 V..... vp9_qsv              VP9 video (Intel Quick Sync Video acceleration) (codec vp9)
 V....D h264_vaapi           H.264/AVC (VAAPI) (codec h264)
 V....D hevc_vaapi           H.265/HEVC (VAAPI) (codec hevc)
 V....D vp9_vaapi            VP9 (VAAPI) (codec vp9)
 V....D h264_nvenc           NVIDIA NVENC H.264 encoder (codec h264)
 V....D hevc_nvenc           NVIDIA NVENC hevc encoder (codec hevc)
"""

FFMPEG_HWACCELS = """Hardware acceleration methods:
vaapi
qsv
cuda
drm
"""

# Real UHD 630 shape: H264/HEVC encode, but VP9 is decode-only (VLD, no EncSlice).
VAINFO_UHD630 = """libva info: VA-API version 1.20.0
vainfo: Driver version: Intel iHD driver for Intel(R) Gen Graphics - 23.1.1 ()
vainfo: Supported profile and entrypoints
      VAProfileH264Main               : VAEntrypointVLD
      VAProfileH264Main               : VAEntrypointEncSlice
      VAProfileH264High               : VAEntrypointVLD
      VAProfileH264High               : VAEntrypointEncSlice
      VAProfileH264High               : VAEntrypointEncSliceLP
      VAProfileHEVCMain               : VAEntrypointVLD
      VAProfileHEVCMain               : VAEntrypointEncSlice
      VAProfileVP9Profile0            : VAEntrypointVLD
      VAProfileVP9Profile2            : VAEntrypointVLD
"""


@pytest.fixture(autouse=True)
def _clear_cache():
    caps._cache = None
    yield
    caps._cache = None


def _fake_run(vainfo_out=VAINFO_UHD630):
    def run(cmd):
        if cmd[0] == "ffmpeg" and "-encoders" in cmd:
            return FFMPEG_ENCODERS
        if cmd[0] == "ffmpeg" and "-hwaccels" in cmd:
            return FFMPEG_HWACCELS
        if cmd[0] == "vainfo":
            return vainfo_out
        if cmd[0] == "nvidia-smi":
            return ""
        return ""
    return run


def test_canonical_collapses_h265_alias():
    assert caps._canonical("h265") == "hevc"
    assert caps._canonical("hevc") == "hevc"
    assert caps._canonical("h264") == "h264"


def test_encoder_parser_skips_legend_rows():
    with patch.object(caps, "_run", return_value=FFMPEG_ENCODERS):
        encoders = caps._ffmpeg_encoders()
    assert "h264_qsv" in encoders
    assert "libx264" in encoders
    assert "=" not in encoders  # legend lines must not parse as encoder names
    assert "Video" not in encoders


def test_encodable_codecs_excludes_decode_only():
    """VP9 on this GPU is VLD (decode) only — it must not count as encodable."""
    profiles = {}
    for line in VAINFO_UHD630.splitlines():
        import re
        m = re.match(r"\s*(VAProfile\S+)\s*:\s*(VAEntrypoint\S+)", line)
        if m:
            profiles.setdefault(m.group(1), []).append(m.group(2))
    codecs = caps._encodable_codecs(profiles)
    assert codecs == {"h264", "hevc"}
    assert "vp9" not in codecs


def test_dri_backend_reports_only_driver_encodable_codecs():
    """ffmpeg has vp9_vaapi compiled in, but the driver can't encode VP9."""
    with patch.object(caps, "_run", side_effect=_fake_run()), \
         patch.object(caps, "_render_nodes", return_value=["/dev/dri/renderD128"]):
        entry = caps._dri_backend("vaapi", caps._ffmpeg_encoders(), caps._ffmpeg_hwaccels())
    assert entry["available"] is True
    assert entry["device"] == "/dev/dri/renderD128"
    assert entry["codecs"] == ["h264", "hevc"]
    assert "vp9" not in entry["codecs"]
    assert "iHD" in entry["driver"]


def test_dri_backend_unavailable_without_render_node():
    with patch.object(caps, "_run", side_effect=_fake_run()), \
         patch.object(caps, "_render_nodes", return_value=[]):
        entry = caps._dri_backend("qsv", caps._ffmpeg_encoders(), caps._ffmpeg_hwaccels())
    assert entry["available"] is False
    assert "render node" in entry["reason"]


def test_dri_backend_unavailable_when_driver_reports_no_encode():
    """Render node present but driver only decodes — must not claim availability."""
    decode_only = "vainfo: Driver version: x\n      VAProfileH264High : VAEntrypointVLD\n"
    with patch.object(caps, "_run", side_effect=_fake_run(vainfo_out=decode_only)), \
         patch.object(caps, "_render_nodes", return_value=["/dev/dri/renderD128"]):
        entry = caps._dri_backend("vaapi", caps._ffmpeg_encoders(), caps._ffmpeg_hwaccels())
    assert entry["available"] is False
    assert "no encode entrypoint" in entry["reason"]


def test_nvenc_unavailable_without_device():
    with patch.object(caps, "_run", side_effect=_fake_run()), \
         patch.object(caps, "_nvidia_devices", return_value=[]):
        entry = caps._nvenc_backend(caps._ffmpeg_encoders(), caps._ffmpeg_hwaccels())
    assert entry["available"] is False
    assert "nvidia" in entry["reason"].lower()


def test_nvenc_available_with_device():
    with patch.object(caps, "_run", side_effect=_fake_run()), \
         patch.object(caps, "_nvidia_devices", return_value=["/dev/nvidia0"]):
        entry = caps._nvenc_backend(caps._ffmpeg_encoders(), caps._ffmpeg_hwaccels())
    assert entry["available"] is True
    assert entry["encoders"]["h264"] == "h264_nvenc"
    assert entry["max_sessions"] == 2  # consumer driver session cap


def test_software_always_available():
    with patch.object(caps, "_run", side_effect=_fake_run()):
        entry = caps._software_backend(caps._ffmpeg_encoders())
    assert entry["available"] is True
    assert entry["encoders"]["h264"] == "libx264"
    assert entry["max_sessions"] is None  # not a hard limit, unlike hardware


def test_detect_capabilities_is_node_scoped_and_cached():
    with patch.object(caps, "_run", side_effect=_fake_run()), \
         patch.object(caps, "_render_nodes", return_value=["/dev/dri/renderD128"]), \
         patch.object(caps, "_nvidia_devices", return_value=[]), \
         patch.dict("os.environ", {"NODE_ID": "media01"}):
        first = caps.detect_capabilities()
        assert first["node_id"] == "media01"
        assert first["hardware_available"] is True
        assert caps.available_backends() == ["qsv", "vaapi", "software"]

        # cached: a second call must not re-probe
        with patch.object(caps, "_run", side_effect=AssertionError("re-probed")):
            assert caps.detect_capabilities() is first


def test_detect_capabilities_force_reprobes():
    with patch.object(caps, "_run", side_effect=_fake_run()), \
         patch.object(caps, "_render_nodes", return_value=[]), \
         patch.object(caps, "_nvidia_devices", return_value=[]):
        first = caps.detect_capabilities()
        assert first["hardware_available"] is False

        with patch.object(caps, "_render_nodes", return_value=["/dev/dri/renderD128"]):
            second = caps.detect_capabilities(force=True)
    assert second is not first
    assert second["hardware_available"] is True


def test_get_backend_lookup():
    with patch.object(caps, "_run", side_effect=_fake_run()), \
         patch.object(caps, "_render_nodes", return_value=["/dev/dri/renderD128"]), \
         patch.object(caps, "_nvidia_devices", return_value=[]):
        assert caps.get_backend("qsv")["available"] is True
        assert caps.get_backend("nope") is None
