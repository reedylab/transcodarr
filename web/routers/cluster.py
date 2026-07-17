# web/routers/cluster.py
"""
Master-side cluster API — nodes register and heartbeat here.

All endpoints are node-initiated and bearer-authenticated with NODE_TOKEN. A
master only accepts nodes when it has TRANSCODARR_MODE=master and a NODE_TOKEN set;
otherwise clustering is effectively off and registrations are refused.

The heartbeat reply may carry a job assignment; a node then executes it on shared
storage and reports back via the /cluster/job/{id}/{progress,complete,failed}
endpoints, where the master runs post-processing.
"""
import logging
import threading

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from transcodarr_core import cluster, dispatch

router = APIRouter()


def _auth(request: Request):
    """Return (ok, error_response). Validates the node's bearer token against the
    master's NODE_TOKEN, and that this instance is actually a master."""
    s = request.app.state.settings
    if (s.TRANSCODARR_MODE or "master").lower() != "master":
        return False, JSONResponse({"error": "not a master instance"}, status_code=403)
    if not s.NODE_TOKEN:
        return False, JSONResponse(
            {"error": "clustering disabled — set NODE_TOKEN on the master"}, status_code=403)
    auth = request.headers.get("authorization", "")
    token = auth[7:] if auth.lower().startswith("bearer ") else ""
    if token != s.NODE_TOKEN:
        return False, JSONResponse({"error": "invalid node token"}, status_code=401)
    return True, None


@router.post("/cluster/register")
async def api_cluster_register(request: Request):
    """A node announces itself: identity, capabilities, worker count, storage check."""
    ok, err = _auth(request)
    if not ok:
        return err
    body = await request.json()
    node_id = body.get("node_id")
    if not node_id:
        return JSONResponse({"error": "node_id required"}, status_code=400)
    cluster.register_node(
        node_id=node_id,
        address=body.get("address") or (request.client.host if request.client else ""),
        capabilities=body.get("capabilities") or {},
        worker_count=body.get("worker_count") or 0,
        storage_ok=bool(body.get("storage_ok")),
        storage_detail=body.get("storage_detail") or "",
    )
    return {"status": "registered", "node_id": node_id,
            "heartbeat_interval_s": max(2, int(cluster.HEARTBEAT_TIMEOUT_S / 3))}


@router.post("/cluster/heartbeat")
async def api_cluster_heartbeat(request: Request):
    """Liveness + current status. The reply may carry a job assignment: if this node
    is eligible and has a free slot, the scheduler hands it the next pending job."""
    ok, err = _auth(request)
    if not ok:
        return err
    body = await request.json()
    node_id = body.get("node_id")
    known = cluster.heartbeat(node_id, worker_count=body.get("worker_count"),
                              jobs=body.get("jobs"))
    if not known:
        # Master was restarted / never saw this node — tell it to re-register.
        return JSONResponse({"status": "unknown", "action": "reregister"}, status_code=409)
    return {"status": "ok", "assignment": dispatch.claim_for_node(node_id)}


@router.post("/cluster/deregister")
async def api_cluster_deregister(request: Request):
    ok, err = _auth(request)
    if not ok:
        return err
    body = await request.json()
    cluster.remove_node(body.get("node_id"))
    return {"status": "deregistered"}


@router.post("/cluster/job/{job_id}/progress")
async def api_job_progress(job_id: str, request: Request):
    """A node streams encode progress (0-100) for a job it is running."""
    ok, err = _auth(request)
    if not ok:
        return err
    body = await request.json()
    dispatch.mark_progress(job_id, body.get("progress"))
    return {"status": "ok"}


def _finalize_job(job: dict) -> None:
    """Run master post-processing for a node-completed job off the request thread."""
    from transcodarr_core.pipeline import finalize_dispatched_job
    finalize_dispatched_job(job.get("ctx"))


@router.post("/cluster/job/{job_id}/complete")
async def api_job_complete(job_id: str, request: Request):
    """A node reports a job finished + verified. The master promotes the output and
    finishes post-processing (history, Radarr/Sonarr, Jellyfin) in the background."""
    ok, err = _auth(request)
    if not ok:
        return err
    job = dispatch.complete(job_id)
    if not job:
        return JSONResponse({"status": "unknown job"}, status_code=404)
    threading.Thread(target=_finalize_job, args=(job,), daemon=True,
                     name=f"finalize-{job_id}").start()
    return {"status": "accepted"}


@router.post("/cluster/job/{job_id}/failed")
async def api_job_failed(job_id: str, request: Request):
    """A node reports a job failed. Drop its partial temp output; source is left intact
    so the watchdog re-processes the title later."""
    ok, err = _auth(request)
    if not ok:
        return err
    body = await request.json()
    job = dispatch.fail(job_id, requeue=False)
    if job:
        logging.warning("[CLUSTER] node %r reported job %s failed: %s",
                        job.get("node_id"), job_id, body.get("error") or "")
        from transcodarr_core.pipeline import discard_dispatched_job
        threading.Thread(target=discard_dispatched_job, args=(job.get("ctx"),),
                         daemon=True, name=f"discard-{job_id}").start()
    return {"status": "ok"}


@router.get("/node/status")
def api_node_status(request: Request):
    """This instance's own role/status — drives the node-mode dormant UI."""
    s = request.app.state.settings
    mode = (s.TRANSCODARR_MODE or "master").lower()
    if mode != "node":
        return {"mode": mode}
    agent = getattr(request.app.state, "node_agent", None)
    if not agent:
        return {"mode": "node", "registered": False, "error": "agent not started"}
    return {"mode": "node", **agent.status()}


@router.get("/cluster/nodes")
def api_cluster_nodes(request: Request):
    """Registry snapshot for the UI (no auth — read-only status, same as other UI reads)."""
    nodes = cluster.list_nodes()
    return {
        "mode": (request.app.state.settings.TRANSCODARR_MODE or "master").lower(),
        "clustering_enabled": bool(request.app.state.settings.NODE_TOKEN),
        "dispatch_enabled": bool(getattr(request.app.state.settings, "CLUSTER_DISPATCH_ENABLED", False)),
        "nodes": nodes,
        "node_worker_total": cluster.aggregate_worker_count(),
        "dispatch": dispatch.snapshot(),
    }
