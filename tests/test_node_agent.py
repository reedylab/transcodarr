"""Tests for the node agent's storage self-check.

The register/heartbeat protocol is integration-tested against a live master; this
covers the storage gate — the thing that decides whether a node is eligible for
work at all — without a filesystem or a master.
"""
from unittest.mock import MagicMock, patch

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


# ----- job dispatch (Phase 2 step 3) -----

def _resp(status=200, payload=None):
    r = MagicMock(status_code=status)
    r.json.return_value = payload or {}
    return r


def test_heartbeat_acts_on_assignment():
    a = _agent()
    a.master, a.token = "http://m", "t"
    seen = []
    a._accept_job = lambda job: seen.append(job)
    assignment = {"id": "j1", "base_name": "A", "input_path": "/x"}
    with patch("transcodarr_core.node_agent.requests.post",
               return_value=_resp(200, {"status": "ok", "assignment": assignment})):
        a._heartbeat()
    assert seen == [assignment]


def test_heartbeat_no_assignment_does_nothing():
    a = _agent()
    a.master, a.token = "http://m", "t"
    seen = []
    a._accept_job = lambda job: seen.append(job)
    with patch("transcodarr_core.node_agent.requests.post",
               return_value=_resp(200, {"status": "ok", "assignment": None})):
        a._heartbeat()
    assert seen == []


def test_heartbeat_409_reregisters_and_skips_assignment():
    a = _agent()
    a.master, a.token, a._registered = "http://m", "t", True
    seen = []
    a._accept_job = lambda job: seen.append(job)
    with patch("transcodarr_core.node_agent.requests.post", return_value=_resp(409)):
        a._heartbeat()
    assert a._registered is False and seen == []


def test_heartbeat_reports_running_jobs():
    a = _agent()
    a.master, a.token = "http://m", "t"
    a._jobs = {"j1": 10.0, "j2": 50.0}
    captured = {}

    def fake_post(url, json=None, **kw):
        captured.update(json=json)
        return _resp(200, {"status": "ok", "assignment": None})

    with patch("transcodarr_core.node_agent.requests.post", side_effect=fake_post):
        a._heartbeat()
    assert set(captured["json"]["jobs"]) == {"j1", "j2"}


def test_accept_job_dedupes_and_submits():
    a = _agent()
    submitted = []
    fake_exec = MagicMock()
    fake_exec.submit.side_effect = lambda fn, job: submitted.append(job)
    a._executor = fake_exec
    job = {"id": "j1", "base_name": "A"}
    a._accept_job(job)
    a._accept_job(job)  # duplicate assignment ignored while running
    assert len(submitted) == 1
    assert "j1" in a._jobs


def test_report_job_posts_expected_url_and_body():
    a = _agent()
    a.master, a.token = "http://m", "tok"
    captured = {}

    def fake_post(url, json=None, headers=None, timeout=None):
        captured.update(url=url, json=json, headers=headers)
        return MagicMock()

    with patch("transcodarr_core.node_agent.requests.post", side_effect=fake_post):
        a._report_job("j1", "progress", {"progress": 50.0})
    assert captured["url"] == "http://m/api/cluster/job/j1/progress"
    assert captured["json"] == {"node_id": a.node_id, "progress": 50.0}
    assert captured["headers"] == {"Authorization": "Bearer tok"}


def test_run_job_reports_complete_on_success(tmp_path):
    a = _agent()
    a.s.MEDIA_TEMP_FOLDER = str(tmp_path)
    a._jobs["j1"] = 0.0
    reports = []
    a._report_job = lambda jid, kind, extra=None: reports.append((jid, kind))
    job = {"id": "j1", "input_path": "/i", "srt_path": None, "tmp_out_path": "/o",
           "base_name": "A", "settings_override": {}}
    with patch("transcodarr_core.pipeline._execute_transcode", return_value=True):
        a._run_job(job)
    assert ("j1", "complete") in reports
    assert "j1" not in a._jobs  # cleared when done


def test_run_job_reports_failed_on_exec_false(tmp_path):
    a = _agent()
    a.s.MEDIA_TEMP_FOLDER = str(tmp_path)
    a._jobs["j1"] = 0.0
    reports = []
    a._report_job = lambda jid, kind, extra=None: reports.append((jid, kind))
    job = {"id": "j1", "input_path": "/i", "srt_path": None, "tmp_out_path": "/o",
           "base_name": "A", "settings_override": {}}
    with patch("transcodarr_core.pipeline._execute_transcode", return_value=False):
        a._run_job(job)
    assert ("j1", "failed") in reports


def test_run_job_reports_failed_on_exception(tmp_path):
    a = _agent()
    a.s.MEDIA_TEMP_FOLDER = str(tmp_path)
    a._jobs["j1"] = 0.0
    reports = []
    a._report_job = lambda jid, kind, extra=None: reports.append((jid, kind))
    job = {"id": "j1", "input_path": "/i", "srt_path": None, "tmp_out_path": "/o",
           "base_name": "A", "settings_override": {}}
    with patch("transcodarr_core.pipeline._execute_transcode",
               side_effect=RuntimeError("boom")):
        a._run_job(job)
    assert ("j1", "failed") in reports
    assert "j1" not in a._jobs
