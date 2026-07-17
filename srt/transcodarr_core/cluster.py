# srt/transcodarr_core/cluster.py
"""
Master-side registry of connected transcode nodes.

A node registers, then heartbeats every few seconds; the master tracks liveness by
heartbeat age and aggregates each node's worker count. In-memory on purpose — the
registry is live state, rebuilt in seconds when nodes re-register after a master
restart, so there's nothing worth persisting.

This module holds no dispatch logic yet (Phase 1 is plumbing) — just the registry
the scheduler and UI read from.
"""
from __future__ import annotations

import logging
import threading
import time

# A node is considered offline once its last heartbeat is older than this. The node
# heartbeats several times inside the window so a single dropped beat isn't fatal.
HEARTBEAT_TIMEOUT_S = 15.0

# A node offline longer than this is dropped from the registry entirely — it shows
# as offline for a grace period (so you can see it dropped), then disappears rather
# than lingering forever. A returning node just re-registers.
STALE_TIMEOUT_S = 120.0

_nodes: dict[str, dict] = {}
_lock = threading.Lock()


def register_node(node_id: str, address: str, capabilities: dict,
                  worker_count: int, storage_ok: bool, storage_detail: str = "") -> None:
    """Add or refresh a node. Called on the node's initial connect (and re-connect)."""
    with _lock:
        existing = _nodes.get(node_id, {})
        _nodes[node_id] = {
            "node_id": node_id,
            "address": address,
            "capabilities": capabilities or {},
            "worker_count": max(0, int(worker_count or 0)),
            "storage_ok": bool(storage_ok),
            "storage_detail": storage_detail or "",
            "jobs": existing.get("jobs", []),
            "registered_at": existing.get("registered_at", time.time()),
            "last_seen": time.time(),
        }
    logging.info("[CLUSTER] node %r registered — %d workers, storage_ok=%s%s",
                 node_id, worker_count, storage_ok,
                 "" if storage_ok else f" ({storage_detail})")


def heartbeat(node_id: str, worker_count: int | None = None,
              jobs: list | None = None) -> bool:
    """Record a heartbeat. Returns False if the node is unknown (it should re-register)."""
    with _lock:
        n = _nodes.get(node_id)
        if not n:
            return False
        n["last_seen"] = time.time()
        if worker_count is not None:
            n["worker_count"] = max(0, int(worker_count))
        if jobs is not None:
            n["jobs"] = jobs
        return True


def remove_node(node_id: str) -> None:
    """Explicit deregister (e.g. node shutting down cleanly)."""
    with _lock:
        _nodes.pop(node_id, None)
    logging.info("[CLUSTER] node %r deregistered", node_id)


def _online(node: dict) -> bool:
    return (time.time() - node["last_seen"]) <= HEARTBEAT_TIMEOUT_S


def _prune_stale(now: float) -> None:
    """Drop nodes that have been offline past the stale window. Caller holds _lock."""
    for nid in [k for k, n in _nodes.items() if now - n["last_seen"] > STALE_TIMEOUT_S]:
        _nodes.pop(nid, None)
        logging.info("[CLUSTER] node %r pruned (offline > %ds)", nid, int(STALE_TIMEOUT_S))


def list_nodes() -> list[dict]:
    """All known nodes with a computed `online` flag and `last_seen_age` (seconds).
    Prunes nodes offline past the stale window so dropped nodes don't linger."""
    now = time.time()
    with _lock:
        _prune_stale(now)
        out = []
        for n in _nodes.values():
            rec = dict(n)
            rec["online"] = _online(n)
            rec["last_seen_age"] = round(now - n["last_seen"], 1)
            out.append(rec)
    return sorted(out, key=lambda r: r["node_id"])


def online_nodes() -> list[dict]:
    """Nodes that are alive AND can see the shared storage — the ones eligible for work."""
    with _lock:
        return [dict(n) for n in _nodes.values() if _online(n) and n["storage_ok"]]


def aggregate_worker_count() -> int:
    """Total node workers available for dispatch (online + storage-ok). The master's
    own local workers are counted separately by the worker pool."""
    return sum(n["worker_count"] for n in online_nodes())
