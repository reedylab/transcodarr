"""Tests for the node agent's storage self-check.

The register/heartbeat protocol is integration-tested against a live master; this
covers the storage gate — the thing that decides whether a node is eligible for
work at all — without a filesystem or a master.
"""
from unittest.mock import patch

from transcodarr_core.node_agent import NodeAgent
from transcodarr_core.config import Settings


def _agent():
    a = NodeAgent(Settings())
    a.s.MEDIA_TEMP_FOLDER = "/temp"
    return a


_PATHS = {
    "movies_watch": "/watch/movies", "tv_watch": "/watch/tv",
    "movies_output": "/output/movies", "tv_output": "/output/tv",
}


def test_storage_ok_when_all_visible_and_writable():
    a = _agent()
    with patch("transcodarr_core.node_agent.get_media_paths", return_value=_PATHS), \
         patch("os.path.isdir", return_value=True), \
         patch("os.access", return_value=True):
        ok, detail = a.storage_check()
    assert ok is True
    assert detail == "shared storage visible"


def test_storage_fails_when_output_missing():
    a = _agent()
    with patch("transcodarr_core.node_agent.get_media_paths", return_value=_PATHS), \
         patch("os.path.isdir", side_effect=lambda p: p != "/output/movies"), \
         patch("os.access", return_value=True):
        ok, detail = a.storage_check()
    assert ok is False
    assert "movies_output not visible" in detail


def test_storage_fails_when_output_not_writable():
    a = _agent()
    with patch("transcodarr_core.node_agent.get_media_paths", return_value=_PATHS), \
         patch("os.path.isdir", return_value=True), \
         patch("os.access", return_value=False):
        ok, detail = a.storage_check()
    assert ok is False
    assert "not writable" in detail


def test_storage_ignores_blank_watch_paths():
    """Re-encode-only setups leave watch paths blank — that must not fail the check."""
    paths = {**_PATHS, "movies_watch": "", "tv_watch": ""}
    a = _agent()
    with patch("transcodarr_core.node_agent.get_media_paths", return_value=paths), \
         patch("os.path.isdir", return_value=True), \
         patch("os.access", return_value=True):
        ok, _ = a.storage_check()
    assert ok is True


def test_agent_wont_start_without_master_or_token():
    a = NodeAgent(Settings())
    a.master = ""
    a.token = "x"
    a.start()
    assert a._thread is None  # no master URL -> no thread
