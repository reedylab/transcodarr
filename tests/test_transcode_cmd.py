"""
Tests for build_ffmpeg_cmd codec dispatch and HDR handling.

These guard against the regression from issue #1 where TARGET_VIDEO_CODEC and
TARGET_AUDIO_CODEC were in the UI schema but silently ignored by the encoder.
"""

from unittest.mock import patch

import pytest

from transcodarr_core.ffmpeg.transcode import (
    _audio_encoder_args,
    _resolve_hdr_action,
    _video_encoder_args,
    build_ffmpeg_cmd,
)


# ── helpers ─────────────────────────────────────────────────────────────────

_SDR_PROBE = {
    "is_hdr": False, "color_transfer": "", "color_primaries": "",
    "pix_fmt": "yuv420p", "height": 1080, "width": 1920,
}
_HDR_PROBE = {
    "is_hdr": True, "color_transfer": "smpte2084", "color_primaries": "bt2020",
    "pix_fmt": "yuv420p10le", "height": 2160, "width": 3840,
}


def _build(overrides, out="/tmp/out.mp4", hdr_info=None):
    """Run build_ffmpeg_cmd with mocked probe + settings_override.

    get_setting is stubbed to return each caller's own default. Without this the
    builder reads the live settings DB for anything `overrides` doesn't set, so
    results depend on whatever preset the host happens to have active (e.g. a
    stored VIDEO_STREAM_MODE=copy silently skips the whole encoder block) and the
    suite passes in CI but fails on a configured machine.
    """
    probe = hdr_info or _SDR_PROBE
    with patch("transcodarr_core.ffmpeg.transcode.detect_hdr", return_value=probe), \
         patch("transcodarr_core.ffmpeg.transcode.get_setting",
               side_effect=lambda key, default=None: default):
        return build_ffmpeg_cmd(
            file_path="/tmp/in.mkv",
            srt_path=None,
            out_temp=out,
            settings_override=overrides,
        )


# ── _video_encoder_args unit tests ──────────────────────────────────────────

def test_video_args_h264():
    args = _video_encoder_args("h264", "slow", "high", "18", "4")
    assert args[:2] == ["-c:v", "libx264"]
    assert "-x264-params" in args
    assert "threads=4" in args
    assert "-preset" in args and "slow" in args
    assert "-profile:v" in args and "high" in args
    assert "-crf" in args and "18" in args


def test_video_args_h265_uses_x265():
    args = _video_encoder_args("h265", "slow", "high", "20", "4")
    assert args[:2] == ["-c:v", "libx265"]
    assert "-x265-params" in args
    assert "pools=4" in args
    # H.265 profile dropdown values don't match libx265 profile names — skipped.
    assert "-profile:v" not in args
    assert "high" not in args


def test_video_args_av1_translates_preset():
    args = _video_encoder_args("av1", "slow", "high", "18", "4")
    assert args[:2] == ["-c:v", "libsvtav1"]
    # "slow" → svt-av1 preset 4
    assert "-preset" in args
    assert "4" in args
    # No x264/x265 params for AV1
    assert "-x264-params" not in args
    assert "-x265-params" not in args
    # AV1 CRF mode needs -b:v 0
    assert "-crf" in args and "18" in args
    assert "-b:v" in args and "0" in args
    # H264 profile doesn't apply
    assert "-profile:v" not in args


def test_video_args_av1_unknown_preset_defaults():
    args = _video_encoder_args("av1", "not-a-preset", "", "", "")
    assert args[:2] == ["-c:v", "libsvtav1"]
    assert "-preset" in args
    assert "8" in args  # default fallback


def test_video_args_vp9():
    args = _video_encoder_args("vp9", "medium", "high", "30", "4")
    assert args[:2] == ["-c:v", "libvpx-vp9"]
    assert "-cpu-used" in args
    assert "-row-mt" in args
    # VP9 CRF mode needs -b:v 0
    assert "-b:v" in args and "0" in args
    assert "-crf" in args and "30" in args
    # Threads applied
    idx = args.index("-threads")
    assert args[idx + 1] == "4"


def test_video_args_av1_threads():
    args = _video_encoder_args("av1", "slow", "", "18", "6")
    assert "-svtav1-params" in args
    idx = args.index("-svtav1-params")
    assert args[idx + 1] == "lp=6"


def test_video_args_h265_pools():
    args = _video_encoder_args("h265", "slow", "", "20", "4")
    assert "-x265-params" in args
    idx = args.index("-x265-params")
    assert args[idx + 1] == "pools=4"


def test_video_args_no_threads_when_empty():
    """Empty threads string should suppress the per-codec threading flag."""
    av1 = _video_encoder_args("av1", "fast", "", "", "")
    assert "-svtav1-params" not in av1
    vp9 = _video_encoder_args("vp9", "fast", "", "", "")
    assert "-threads" not in vp9
    h264 = _video_encoder_args("h264", "fast", "", "", "")
    assert "-x264-params" not in h264


def test_video_args_unknown_codec_falls_back_to_libx264():
    args = _video_encoder_args("xyzzy", "fast", "", "", "")
    assert args[:2] == ["-c:v", "libx264"]


# ── _audio_encoder_args unit tests ──────────────────────────────────────────

def test_audio_args_aac():
    args = _audio_encoder_args("aac", "448k", "6")
    assert args[:2] == ["-c:a", "aac"]
    assert "-b:a" in args and "448k" in args
    assert "-ac" in args and "6" in args


def test_audio_args_flac_skips_bitrate():
    args = _audio_encoder_args("flac", "448k", "8")
    assert args[:2] == ["-c:a", "flac"]
    # FLAC is lossless — bitrate makes no sense
    assert "-b:a" not in args
    assert "448k" not in args
    assert "-compression_level" in args
    assert "-ac" in args and "8" in args


def test_audio_args_opus_uses_libopus():
    args = _audio_encoder_args("opus", "192k", "2")
    assert args[:2] == ["-c:a", "libopus"]
    assert "-b:a" in args and "192k" in args


def test_audio_args_eac3():
    args = _audio_encoder_args("eac3", "640k", "6")
    assert args[:2] == ["-c:a", "eac3"]


# ── _resolve_hdr_action ─────────────────────────────────────────────────────

def test_hdr_auto_tonemaps_for_h264():
    assert _resolve_hdr_action("auto", "h264") == "tonemap"


def test_hdr_auto_passthrough_for_av1():
    assert _resolve_hdr_action("auto", "av1") == "passthrough"


def test_hdr_auto_passthrough_for_h265():
    assert _resolve_hdr_action("auto", "h265") == "passthrough"


def test_hdr_explicit_tonemap_forces_tonemap():
    assert _resolve_hdr_action("tonemap", "av1") == "tonemap"


def test_hdr_explicit_passthrough_forces_passthrough():
    assert _resolve_hdr_action("passthrough", "h264") == "passthrough"


# ── end-to-end build_ffmpeg_cmd tests ───────────────────────────────────────

def test_build_uses_av1_when_selected():
    """Regression test for issue #1: AV1 must actually produce libsvtav1."""
    cmd = _build({
        "TARGET_VIDEO_CODEC": "av1",
        "TARGET_AUDIO_CODEC": "flac",
        "TARGET_PRESET": "slow",
        "TARGET_CRF": "18",
        "TARGET_PROFILE": "high",
    })
    assert "libsvtav1" in cmd
    assert "libx264" not in cmd
    assert "flac" in cmd
    assert "aac" not in cmd


def test_build_h264_still_uses_libx264():
    cmd = _build({
        "TARGET_VIDEO_CODEC": "h264",
        "TARGET_AUDIO_CODEC": "aac",
        "TARGET_PRESET": "fast",
        "TARGET_CRF": "23",
        "TARGET_PROFILE": "high",
    })
    assert "libx264" in cmd
    assert "-x264-params" in cmd
    assert "aac" in cmd


def test_build_mp4_adds_faststart():
    cmd = _build({"TARGET_VIDEO_CODEC": "h264"}, out="/tmp/out.mp4")
    assert "-movflags" in cmd
    assert "+faststart" in cmd


def test_build_mkv_skips_faststart():
    cmd = _build({"TARGET_VIDEO_CODEC": "av1"}, out="/tmp/out.mkv")
    assert "+faststart" not in cmd


def test_build_mkv_uses_srt_subtitle_codec():
    """MKV should use native srt, not mov_text (which is MP4-only)."""
    with patch("transcodarr_core.ffmpeg.transcode.detect_hdr", return_value=_SDR_PROBE), \
         patch("transcodarr_core.ffmpeg.transcode.sanitize_for_movtext",
               return_value="/tmp/safe.srt"):
        cmd = build_ffmpeg_cmd(
            file_path="/tmp/in.mkv",
            srt_path="/tmp/in.srt",
            out_temp="/tmp/out.mkv",
            settings_override={"TARGET_VIDEO_CODEC": "av1"},
        )
    assert "srt" in cmd
    assert "mov_text" not in cmd


def test_build_mp4_uses_mov_text():
    with patch("transcodarr_core.ffmpeg.transcode.detect_hdr", return_value=_SDR_PROBE), \
         patch("transcodarr_core.ffmpeg.transcode.sanitize_for_movtext",
               return_value="/tmp/safe.srt"):
        cmd = build_ffmpeg_cmd(
            file_path="/tmp/in.mkv",
            srt_path="/tmp/in.srt",
            out_temp="/tmp/out.mp4",
            settings_override={"TARGET_VIDEO_CODEC": "h264"},
        )
    assert "mov_text" in cmd


def test_build_hdr_auto_h264_applies_tonemap():
    """Jake's gray-movie bug: HDR source + h264 must tonemap."""
    cmd = _build(
        {"TARGET_VIDEO_CODEC": "h264", "TARGET_HDR_MODE": "auto"},
        hdr_info=_HDR_PROBE,
    )
    vf_idx = cmd.index("-vf")
    vf_chain = cmd[vf_idx + 1]
    assert "tonemap" in vf_chain
    # Tonemap chain bakes in format=yuv420p so no explicit -pix_fmt is emitted
    assert "format=yuv420p" in vf_chain
    assert "yuv420p10le" not in cmd


def test_build_hdr_auto_av1_passthrough():
    """HDR source + AV1 should preserve HDR (10-bit, BT.2020)."""
    cmd = _build(
        {"TARGET_VIDEO_CODEC": "av1", "TARGET_HDR_MODE": "auto"},
        hdr_info=_HDR_PROBE,
    )
    # No tonemap filter
    if "-vf" in cmd:
        vf_idx = cmd.index("-vf")
        assert "tonemap" not in cmd[vf_idx + 1]
    # 10-bit pix_fmt
    assert "yuv420p10le" in cmd
    # Source color metadata preserved
    assert "-color_primaries" in cmd
    assert "bt2020" in cmd
    assert "-color_trc" in cmd
    assert "smpte2084" in cmd


def test_build_hdr_forced_tonemap_overrides_av1():
    """User can force tonemap even on AV1 if they want SDR output."""
    cmd = _build(
        {"TARGET_VIDEO_CODEC": "av1", "TARGET_HDR_MODE": "tonemap"},
        hdr_info=_HDR_PROBE,
    )
    vf_idx = cmd.index("-vf")
    assert "tonemap" in cmd[vf_idx + 1]
    # No 10-bit pix_fmt when tonemapping
    assert "yuv420p10le" not in cmd


def test_build_sdr_source_no_tonemap_regardless_of_mode():
    cmd = _build(
        {"TARGET_VIDEO_CODEC": "h264", "TARGET_HDR_MODE": "tonemap"},
        hdr_info=_SDR_PROBE,
    )
    # SDR sources should never trigger tonemap regardless of mode
    if "-vf" in cmd:
        vf_idx = cmd.index("-vf")
        assert "tonemap" not in cmd[vf_idx + 1]


def test_build_prefers_encoder_threads_over_legacy_key():
    """New ENCODER_THREADS wins when both are present."""
    cmd = _build({
        "TARGET_VIDEO_CODEC": "av1",
        "ENCODER_THREADS": "8",
        "X264_THREADS": "2",
    })
    idx = cmd.index("-svtav1-params")
    assert cmd[idx + 1] == "lp=8"


def test_build_falls_back_to_legacy_x264_threads():
    """Presets stored before the rename still supply thread count via X264_THREADS."""
    cmd = _build({
        "TARGET_VIDEO_CODEC": "av1",
        "X264_THREADS": "2",
        # ENCODER_THREADS deliberately absent
    })
    idx = cmd.index("-svtav1-params")
    assert cmd[idx + 1] == "lp=2"


def test_build_copy_video_still_works():
    cmd = _build({"VIDEO_STREAM_MODE": "copy", "TARGET_VIDEO_CODEC": "av1"})
    # Copy mode should bypass codec dispatch entirely
    assert "libsvtav1" not in cmd
    assert "libx264" not in cmd
    # -c:v copy should be present
    idx = cmd.index("-c:v")
    assert cmd[idx + 1] == "copy"
