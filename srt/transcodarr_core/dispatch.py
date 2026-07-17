# srt/transcodarr_core/dispatch.py
"""Master-side job scheduler for multi-node dispatch.

A prepped job — input path, sidecar srt, temp/out paths, and the fully resolved
encode settings — is enqueued here by the master after PREP. Connected nodes claim
an eligible job in their heartbeat reply and run the pure-ffmpeg EXECUTE phase on
shared storage, then report back so the master runs POST. The master keeps the full
prep context (`ctx`) alongside each job so it can finish (promote/history/arr/jellyfin)
when the node completes.

In-memory on purpose, same rationale as the node registry (`cluster.py`): dispatch is
live state, cheap to rebuild. Routing is capability-aware — a software job runs on any
node, a hardware preset only on a node advertising that backend.

Wiring status (Phase 2): the queue + heartbeat assignment (this module) and the
`try_dispatch` hook in the pipeline are gated behind `CLUSTER_DISPATCH_ENABLED`
(default off). Node-side execution and master post-processing on completion are the
next steps; until they land, leave the flag off so jobs are never stranded.
"""
from __future__ import annotations

import logging
import os
import threading
import time
import uuid

from . import cluster

# Reentrant: try_dispatch -> enqueue both take the lock, and claim_for_node calls
# into cluster (which has its own lock). Lock order is always dispatch -> cluster.
_lock = threading.RLock()

_pending: list[dict] = []        # FIFO of jobs awaiting a node
_inflight: dict[str, dict] = {}  # job_id -> job currently assigned to / running on a node

# The subset of a job handed to the node — everything EXECUTE needs, nothing to look
# up. All paths are shared-storage container paths, identical on master and node.
_PAYLOAD_KEYS = ("id", "input_path", "srt_path", "tmp_out_path", "base_name",
                 "settings_override")

_SOFTWARE = ("", "software", "sw", "none")


def _job_backend(settings_override) -> str:
    """The backend a job asks for. Mirrors build_ffmpeg_cmd's HW_BACKEND lookup; the
    node still re-resolves this against its own hardware and falls back to software."""
    return str((settings_override or {}).get("HW_BACKEND", "software") or "software").lower()


def _node_can_run(node: dict, backend: str) -> bool:
    """Software runs anywhere; a hardware backend needs the node advertising it as
    available in its capability record."""
    if backend in _SOFTWARE:
        return True
    for b in (node.get("capabilities") or {}).get("backends", []):
        if b.get("id") == backend and b.get("available"):
            return True
    return False


def eligible_nodes(backend: str) -> list[dict]:
    """Online, storage-ok nodes that can run this backend (ignoring free slots)."""
    return [n for n in cluster.online_nodes() if _node_can_run(n, backend)]


def _node_free_slots(node_id: str, worker_count) -> int:
    """Free slots = advertised workers minus jobs the master has in flight on this
    node. Caller holds _lock."""
    running = sum(1 for j in _inflight.values() if j["node_id"] == node_id)
    return max(0, int(worker_count or 0) - running)


def enqueue(payload: dict, ctx=None) -> str:
    """Add a prepped job to the pending queue. `payload` must carry input_path,
    tmp_out_path, base_name (srt_path/settings_override optional); `ctx` is the
    master-only prep context used to run POST on completion. Returns the job id."""
    job_id = payload.get("id") or uuid.uuid4().hex[:12]
    job = {
        "id": job_id,
        "input_path": payload["input_path"],
        "srt_path": payload.get("srt_path"),
        "tmp_out_path": payload["tmp_out_path"],
        "base_name": payload["base_name"],
        "settings_override": payload.get("settings_override") or {},
        "required_backend": _job_backend(payload.get("settings_override")),
        "ctx": ctx,               # master-only; never sent to a node
        "state": "pending",
        "node_id": None,
        "progress": 0.0,
        "created_at": time.time(),
    }
    with _lock:
        _pending.append(job)
        depth = len(_pending)
    logging.info("[DISPATCH] queued job %s (%s, backend=%s) — %d pending",
                 job_id, os.path.basename(job["input_path"]), job["required_backend"], depth)
    return job_id


def claim_for_node(node_id: str) -> dict | None:
    """Called from the heartbeat handler. If this node is online, storage-ok, and has
    a free slot, pop the first pending job it can run, move it in-flight, and return
    the node payload. Returns None when there is nothing to hand out."""
    with _lock:
        node = next((n for n in cluster.online_nodes() if n["node_id"] == node_id), None)
        if not node:
            return None
        if _node_free_slots(node_id, node.get("worker_count", 0)) <= 0:
            return None
        for i, job in enumerate(_pending):
            if _node_can_run(node, job["required_backend"]):
                _pending.pop(i)
                job["state"] = "assigned"
                job["node_id"] = node_id
                job["assigned_at"] = time.time()
                _inflight[job["id"]] = job
                logging.info("[DISPATCH] assigned job %s (%s) -> node %r",
                             job["id"], job["base_name"], node_id)
                return {k: job[k] for k in _PAYLOAD_KEYS}
    return None


def mark_progress(job_id: str, progress) -> bool:
    """Record streamed progress for an in-flight job. Returns False if unknown."""
    with _lock:
        j = _inflight.get(job_id)
        if not j:
            return False
        try:
            j["progress"] = float(progress)
        except (TypeError, ValueError):
            pass
        j["state"] = "running"
        return True


def complete(job_id: str) -> dict | None:
    """Node reports success. Remove from in-flight and return the job (with its ctx)
    so the master can run POST. Returns None if the job is unknown."""
    with _lock:
        j = _inflight.pop(job_id, None)
    if j:
        logging.info("[DISPATCH] job %s completed on node %r", job_id, j.get("node_id"))
    return j


def fail(job_id: str, requeue: bool = False) -> dict | None:
    """Node reports failure. Drop from in-flight; optionally requeue for another node.
    Returns the job (None if unknown)."""
    with _lock:
        j = _inflight.pop(job_id, None)
        if j and requeue:
            _reset_to_pending(j)
    if j:
        logging.info("[DISPATCH] job %s failed on node %r%s",
                     job_id, j.get("node_id"), " — requeued" if requeue else "")
    return j


def requeue_node_jobs(node_id: str) -> int:
    """Move a (dead/departed) node's in-flight jobs back to pending for another node.
    Returns how many were moved."""
    with _lock:
        moved = [jid for jid, j in _inflight.items() if j["node_id"] == node_id]
        for jid in moved:
            _reset_to_pending(_inflight.pop(jid))
    if moved:
        logging.info("[DISPATCH] requeued %d job(s) from node %r", len(moved), node_id)
    return len(moved)


def _reset_to_pending(job: dict) -> None:
    """Return an in-flight job to the pending queue. Caller holds _lock."""
    job["state"] = "pending"
    job["node_id"] = None
    job["progress"] = 0.0
    _pending.append(job)


def reap_offline() -> int:
    """Requeue the in-flight jobs of any node that has gone offline (missed heartbeats)
    or vanished from the registry, so a surviving/returning node can pick them up. Meant
    to be called periodically by the master's reaper. Returns the number requeued.

    Idempotent: once a node's jobs are moved back to pending they're no longer in-flight
    for it, so a later pass finds nothing. The source file is never touched by dispatch,
    so a job that finds no eligible node simply waits in the queue."""
    with _lock:
        stuck_nodes = {j["node_id"] for j in _inflight.values() if j["node_id"]}
    total = 0
    for node_id in stuck_nodes:
        if not cluster.is_online(node_id):
            total += requeue_node_jobs(node_id)
    return total


def try_dispatch(ctx) -> bool:
    """Pipeline hook, called after PREP. If cluster dispatch is enabled and an eligible
    node is connected, enqueue this prepped job for a node and return True — the master
    then skips local EXECUTE/POST and finishes the job when the node reports back.
    Returns False to run the job locally as usual (the default, and always the default
    until CLUSTER_DISPATCH_ENABLED is set)."""
    s = ctx.s
    if not getattr(s, "CLUSTER_DISPATCH_ENABLED", False):
        return False
    if (getattr(s, "TRANSCODARR_MODE", "master") or "master").lower() != "master":
        return False
    backend = _job_backend(ctx.settings_override)
    if not eligible_nodes(backend):
        return False
    enqueue({
        "input_path": ctx.ffmpeg_input,
        "srt_path": ctx.chosen_srt,
        "tmp_out_path": ctx.tmp_path,
        "base_name": ctx.base_name,
        "settings_override": ctx.settings_override,
    }, ctx=ctx)
    return True


def snapshot() -> dict:
    """Read-only view of the queues for the UI / diagnostics."""
    with _lock:
        return {
            "pending_count": len(_pending),
            "inflight_count": len(_inflight),
            "pending": [{k: j.get(k) for k in
                         ("id", "base_name", "required_backend", "created_at")}
                        for j in _pending],
            "inflight": [{k: j.get(k) for k in
                          ("id", "base_name", "required_backend", "node_id",
                           "state", "progress")}
                         for j in _inflight.values()],
        }
