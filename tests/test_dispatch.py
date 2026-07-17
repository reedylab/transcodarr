"""Tests for the master-side dispatch scheduler (Phase 2 step 2)."""
from types import SimpleNamespace

import pytest

from transcodarr_core import cluster, dispatch


@pytest.fixture(autouse=True)
def _clear():
    cluster._nodes.clear()
    dispatch._pending.clear()
    dispatch._inflight.clear()
    yield
    cluster._nodes.clear()
    dispatch._pending.clear()
    dispatch._inflight.clear()


def caps(*backends):
    """A capability record advertising the given hardware backends as available."""
    return {"backends": [{"id": b, "available": True} for b in backends]}


def payload(base="Movie", backend=None, **over):
    p = {
        "input_path": f"/output/movies/{base}/{base}.mkv",
        "srt_path": f"/output/movies/{base}/{base}.srt",
        "tmp_out_path": f"/temp/movies/{base}/{base}.tmp.mp4",
        "base_name": base,
        "settings_override": {"HW_BACKEND": backend} if backend else {},
    }
    p.update(over)
    return p


# ---- eligibility / capability routing ----

def test_software_job_claimable_by_any_node():
    cluster.register_node("sw", "10.0.0.1", caps(), 2, True)
    dispatch.enqueue(payload("A"))
    job = dispatch.claim_for_node("sw")
    assert job is not None
    assert set(job) == set(dispatch._PAYLOAD_KEYS)
    assert job["base_name"] == "A"
    assert dispatch.snapshot()["inflight_count"] == 1
    assert dispatch.snapshot()["pending_count"] == 0


def test_hw_job_only_goes_to_capable_node():
    cluster.register_node("sw", "10.0.0.1", caps(), 2, True)
    cluster.register_node("gpu", "10.0.0.2", caps("nvenc"), 2, True)
    dispatch.enqueue(payload("N", backend="nvenc"))
    # Software-only node cannot run an nvenc job -> nothing handed out, job stays pending.
    assert dispatch.claim_for_node("sw") is None
    assert dispatch.snapshot()["pending_count"] == 1
    # The nvenc node claims it.
    job = dispatch.claim_for_node("gpu")
    assert job is not None and job["base_name"] == "N"


def test_node_skips_incompatible_job_and_takes_a_runnable_one():
    cluster.register_node("sw", "10.0.0.1", caps(), 2, True)
    dispatch.enqueue(payload("HW", backend="qsv"))   # first, but sw can't run it
    dispatch.enqueue(payload("SW"))                   # second, sw can
    job = dispatch.claim_for_node("sw")
    assert job["base_name"] == "SW"
    assert dispatch.snapshot()["pending_count"] == 1  # the qsv job is still waiting


def test_hw_backend_unavailable_on_node_is_not_eligible():
    cluster.register_node("gpu", "10.0.0.2",
                          {"backends": [{"id": "nvenc", "available": False}]}, 2, True)
    dispatch.enqueue(payload("N", backend="nvenc"))
    assert dispatch.claim_for_node("gpu") is None


# ---- capacity / liveness / storage gating ----

def test_free_slot_gating():
    cluster.register_node("sw", "10.0.0.1", caps(), 1, True)  # one worker
    id1 = dispatch.enqueue(payload("A"))
    dispatch.enqueue(payload("B"))
    assert dispatch.claim_for_node("sw")["base_name"] == "A"
    assert dispatch.claim_for_node("sw") is None             # no free slot
    dispatch.complete(id1)                                    # frees the slot
    assert dispatch.claim_for_node("sw")["base_name"] == "B"


def test_unknown_or_offline_node_claims_nothing():
    dispatch.enqueue(payload("A"))
    assert dispatch.claim_for_node("ghost") is None
    import time
    cluster.register_node("dead", "10.0.0.9", caps(), 2, True)
    cluster._nodes["dead"]["last_seen"] = time.time() - 999
    assert dispatch.claim_for_node("dead") is None
    assert dispatch.snapshot()["pending_count"] == 1


def test_storage_bad_node_claims_nothing():
    cluster.register_node("nostore", "10.0.0.3", caps(), 2, False, "not visible")
    dispatch.enqueue(payload("A"))
    assert dispatch.claim_for_node("nostore") is None


# ---- lifecycle: complete / fail / requeue ----

def test_complete_returns_job_with_ctx_and_clears_inflight():
    cluster.register_node("sw", "10.0.0.1", caps(), 2, True)
    sentinel = object()
    jid = dispatch.enqueue(payload("A"), ctx=sentinel)
    dispatch.claim_for_node("sw")
    done = dispatch.complete(jid)
    assert done is not None and done["ctx"] is sentinel
    assert dispatch.complete(jid) is None            # already gone
    assert dispatch.snapshot()["inflight_count"] == 0


def test_fail_with_requeue_returns_to_pending():
    cluster.register_node("sw", "10.0.0.1", caps(), 2, True)
    jid = dispatch.enqueue(payload("A"))
    dispatch.claim_for_node("sw")
    dispatch.fail(jid, requeue=True)
    snap = dispatch.snapshot()
    assert snap["inflight_count"] == 0 and snap["pending_count"] == 1
    # claimable again
    assert dispatch.claim_for_node("sw")["base_name"] == "A"


def test_requeue_node_jobs_moves_inflight_back():
    cluster.register_node("sw", "10.0.0.1", caps(), 4, True)
    dispatch.enqueue(payload("A"))
    dispatch.enqueue(payload("B"))
    dispatch.claim_for_node("sw")
    dispatch.claim_for_node("sw")
    assert dispatch.snapshot()["inflight_count"] == 2
    moved = dispatch.requeue_node_jobs("sw")
    assert moved == 2
    assert dispatch.snapshot()["pending_count"] == 2


def test_mark_progress():
    cluster.register_node("sw", "10.0.0.1", caps(), 2, True)
    jid = dispatch.enqueue(payload("A"))
    dispatch.claim_for_node("sw")
    assert dispatch.mark_progress(jid, 42.5) is True
    assert dispatch.mark_progress("nope", 10) is False
    inflight = dispatch.snapshot()["inflight"][0]
    assert inflight["progress"] == 42.5 and inflight["state"] == "running"


# ---- try_dispatch hook (the pipeline seam) ----

def _ctx(dispatch_enabled=True, mode="master", backend=None):
    return SimpleNamespace(
        s=SimpleNamespace(CLUSTER_DISPATCH_ENABLED=dispatch_enabled, TRANSCODARR_MODE=mode),
        settings_override=({"HW_BACKEND": backend} if backend else {}),
        ffmpeg_input="/output/movies/A/A.mkv",
        chosen_srt="/output/movies/A/A.srt",
        tmp_path="/temp/movies/A/A.tmp.mp4",
        base_name="A",
    )


def test_try_dispatch_off_by_default_runs_local():
    cluster.register_node("sw", "10.0.0.1", caps(), 2, True)
    assert dispatch.try_dispatch(_ctx(dispatch_enabled=False)) is False
    assert dispatch.snapshot()["pending_count"] == 0   # nothing enqueued


def test_try_dispatch_enqueues_when_enabled_and_eligible_node():
    cluster.register_node("sw", "10.0.0.1", caps(), 2, True)
    assert dispatch.try_dispatch(_ctx(dispatch_enabled=True)) is True
    snap = dispatch.snapshot()
    assert snap["pending_count"] == 1
    assert snap["pending"][0]["base_name"] == "A"


def test_try_dispatch_no_node_runs_local():
    assert dispatch.try_dispatch(_ctx(dispatch_enabled=True)) is False
    assert dispatch.snapshot()["pending_count"] == 0


def test_try_dispatch_on_node_instance_runs_local():
    cluster.register_node("sw", "10.0.0.1", caps(), 2, True)
    assert dispatch.try_dispatch(_ctx(dispatch_enabled=True, mode="node")) is False


def test_try_dispatch_hw_job_without_capable_node_runs_local():
    cluster.register_node("sw", "10.0.0.1", caps(), 2, True)  # software only
    assert dispatch.try_dispatch(_ctx(dispatch_enabled=True, backend="nvenc")) is False
    assert dispatch.snapshot()["pending_count"] == 0
