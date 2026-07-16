"""Tests for full GPU-decode pipeline selection and command construction.

The pipeline decodes on the GPU and keeps frames there through encode (~4x
faster, far less CPU). The rules these guard: it engages only when the whole
chain can consume GPU surfaces, and everything else falls back to the existing
CPU-decode path without failing a job.
"""
from unittest.mock import patch

import pytest

from transcodarr_core.ffmpeg.transcode import (
    _even,
    _full_gpu_filters,
    _gpu_scale_filter,
    _hw_decodable,
    _hw_decode_args,
    _use_full_gpu_decode,
    build_ffmpeg_cmd,
)

# UHD 630-shaped record: decodes H264/HEVC/VP9/MPEG2/VC1 but NOT av1.
_VAAPI = {"id": "vaapi", "available": True, "device": "/dev/dri/renderD128",
          "encoders": {"h264": "h264_vaapi", "hevc": "hevc_vaapi"},
          "decode_codecs": ["h264", "hevc", "vp9", "mpeg2video", "vc1"]}
_QSV = {**_VAAPI, "id": "qsv", "encoders": {"h264": "h264_qsv", "hevc": "hevc_qsv"}}


def _dec_backend(record):
    return patch("transcodarr_core.ffmpeg.capabilities.get_backend", return_value=record)


# ── _hw_decodable: gate on the driver's real decode list ─────────────────────

def test_decodable_true_for_supported_codec():
    with _dec_backend(_VAAPI):
        assert _hw_decodable("vaapi", "hevc") is True


def test_decodable_false_for_av1_on_uhd630():
    """AV1 + -hwaccel hard-fails on a GPU that can't decode it — must gate off."""
    with _dec_backend(_VAAPI):
        assert _hw_decodable("vaapi", "av1") is False


def test_decodable_false_when_backend_unavailable():
    with _dec_backend({"available": False}):
        assert _hw_decodable("vaapi", "hevc") is False


def test_decodable_false_for_software_or_empty():
    assert _hw_decodable("software", "hevc") is False
    with _dec_backend(_VAAPI):
        assert _hw_decodable("vaapi", "") is False


def test_qsv_decodable_needs_a_known_decoder():
    """QSV needs an explicit decoder name; a codec without one can't HW-decode."""
    rec = {**_QSV, "decode_codecs": ["h264", "theora"]}  # theora has no *_qsv decoder
    with _dec_backend(rec):
        assert _hw_decodable("qsv", "theora") is False
        assert _hw_decodable("qsv", "h264") is True


# ── decode args ──────────────────────────────────────────────────────────────

def test_vaapi_decode_args_generic():
    args = _hw_decode_args("vaapi", "hevc", "/dev/dri/renderD128")
    assert args == ["-hwaccel", "vaapi", "-hwaccel_output_format", "vaapi",
                    "-vaapi_device", "/dev/dri/renderD128"]


def test_qsv_decode_args_name_the_decoder():
    args = _hw_decode_args("qsv", "hevc", "/dev/dri/renderD128")
    assert "-c:v" in args and "hevc_qsv" in args
    assert "qsv=hw@va" in args


def test_qsv_decode_args_empty_without_known_decoder():
    assert _hw_decode_args("qsv", "theora", "/dev/dri/renderD128") == []


def test_nvenc_decode_args_use_cuda():
    assert _hw_decode_args("nvenc", "h264", None) == \
        ["-hwaccel", "cuda", "-hwaccel_output_format", "cuda"]


# ── GPU scale filters ────────────────────────────────────────────────────────

def test_even_rounding():
    assert _even(1919.4) == 1920
    assert _even(1281) % 2 == 0  # h264 needs even dimensions
    assert _even(1920.6) == 1920


def test_gpu_scale_1080p_max_computes_width_for_qsv():
    """vpp_qsv rejects auto width, so 4K -> 1080p becomes an explicit 1920x1080."""
    assert _gpu_scale_filter("qsv", "1080p_max", 3840, 2160) == "vpp_qsv=w=1920:h=1080"


def test_gpu_scale_1080p_max_vaapi_has_format():
    assert _gpu_scale_filter("vaapi", "1080p_max", 3840, 2160) == \
        "scale_vaapi=w=1920:h=1080:format=nv12"


def test_gpu_scale_skipped_when_already_small():
    assert _gpu_scale_filter("vaapi", "1080p_max", 1280, 720) == ""


def test_gpu_scale_source_means_no_scale():
    assert _gpu_scale_filter("vaapi", "source", 3840, 2160) == ""


def test_gpu_scale_explicit_resolution():
    assert _gpu_scale_filter("qsv", "1280x720", 1920, 1080) == "vpp_qsv=w=1280:h=720"


# ── full-pipeline decision matrix ────────────────────────────────────────────

def test_full_gpu_sdr_any_hw_backend():
    with _dec_backend(_VAAPI):
        assert _use_full_gpu_decode("vaapi", "none", "software", "hevc", "yuv420p") is True
    with _dec_backend(_QSV):
        assert _use_full_gpu_decode("qsv", "none", "software", "hevc", "yuv420p") is True


def test_full_gpu_off_for_undecodable_source():
    with _dec_backend(_VAAPI):
        assert _use_full_gpu_decode("vaapi", "none", "software", "av1", "yuv420p") is False


def test_full_gpu_hdr_only_on_vaapi_tonemap():
    with _dec_backend(_VAAPI):
        assert _use_full_gpu_decode("vaapi", "tonemap", "vaapi", "hevc", "yuv420p10le") is True
        # QSV can't consume the GPU tonemap surfaces
        assert _use_full_gpu_decode("qsv", "tonemap", "software", "hevc", "yuv420p10le") is False
        # OpenCL tonemap path stays on CPU decode
        assert _use_full_gpu_decode("vaapi", "tonemap", "opencl", "hevc", "yuv420p10le") is False


def test_full_gpu_off_for_hdr_passthrough():
    with _dec_backend(_VAAPI):
        assert _use_full_gpu_decode("vaapi", "passthrough", "software", "hevc", "yuv420p10le") is False


def test_full_gpu_qsv_skips_10bit_sdr():
    """vpp 10->8 handling isn't wired for QSV, so 10-bit SDR uses the CPU path."""
    with _dec_backend(_QSV):
        assert _use_full_gpu_decode("qsv", "none", "software", "hevc", "yuv420p10le") is False
        assert _use_full_gpu_decode("vaapi", "none", "software", "hevc", "yuv420p10le") is True


# ── full-pipeline filter chains ──────────────────────────────────────────────

def test_full_gpu_filters_sdr_scale_only():
    vf = _full_gpu_filters("vaapi", "none", "1080p_max", 3840, 2160, is_10bit=False)
    assert vf == ["scale_vaapi=w=1920:h=1080:format=nv12"]


def test_full_gpu_filters_hdr_tonemap_then_scale():
    vf = _full_gpu_filters("vaapi", "tonemap", "1080p_max", 3840, 2160, is_10bit=True)
    assert vf[0].startswith("tonemap_vaapi")
    assert vf[1].startswith("scale_vaapi")


def test_full_gpu_filters_10bit_sdr_no_scale_still_converts():
    """10-bit SDR at source resolution still needs a 10->8 pass for h264_vaapi."""
    vf = _full_gpu_filters("vaapi", "none", "source", 1920, 1080, is_10bit=True)
    assert vf == ["scale_vaapi=format=nv12"]


def test_full_gpu_filters_8bit_no_scale_is_empty():
    assert _full_gpu_filters("vaapi", "none", "source", 1920, 1080, is_10bit=False) == []


# ── build_ffmpeg_cmd integration ─────────────────────────────────────────────

def _build(overrides, backend, probe):
    with patch("transcodarr_core.ffmpeg.transcode.detect_hdr", return_value=probe), \
         patch("transcodarr_core.ffmpeg.transcode.get_setting",
               side_effect=lambda key, default=None: default):
        return build_ffmpeg_cmd("/tmp/in.mkv", None, "/tmp/o.mp4",
                                settings_override=overrides, backend=backend)


_SDR_HEVC = {"is_hdr": False, "color_transfer": "", "color_primaries": "", "pix_fmt": "yuv420p",
             "codec_name": "hevc", "height": 2160, "width": 3840}
_SDR_AV1 = {**_SDR_HEVC, "codec_name": "av1"}


def test_build_full_gpu_has_hwaccel_and_no_hwupload():
    with _dec_backend(_VAAPI):
        cmd = _build({"TARGET_VIDEO_CODEC": "h264", "TARGET_RESOLUTION": "1080p_max"},
                     "vaapi", _SDR_HEVC)
    assert cmd.index("-hwaccel") < cmd.index("-i"), "decode init must precede -i"
    assert "hwupload" not in " ".join(cmd), "full pipeline must not upload from the CPU"
    assert "-pix_fmt" not in cmd
    vf = cmd[cmd.index("-vf") + 1]
    assert vf == "scale_vaapi=w=1920:h=1080:format=nv12"
    assert "h264_vaapi" in cmd


def test_build_undecodable_source_falls_back_to_cpu_path():
    with _dec_backend(_VAAPI):
        cmd = _build({"TARGET_VIDEO_CODEC": "h264", "TARGET_RESOLUTION": "1080p_max"},
                     "vaapi", _SDR_AV1)
    assert "-hwaccel" not in cmd, "AV1 can't HW-decode here — must use CPU decode"
    vf = cmd[cmd.index("-vf") + 1]
    assert "hwupload" in vf                 # the existing CPU-decode -> upload path
    assert "scale=-2:1080" in vf            # CPU scale, unchanged
    assert "h264_vaapi" in cmd              # ...still a hardware ENCODE


def test_build_missing_codec_name_uses_cpu_path():
    """A probe that couldn't read the codec must not attempt HW decode."""
    probe = {**_SDR_HEVC, "codec_name": ""}
    with _dec_backend(_VAAPI):
        cmd = _build({"TARGET_VIDEO_CODEC": "h264"}, "vaapi", probe)
    assert "-hwaccel" not in cmd
