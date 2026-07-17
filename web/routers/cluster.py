# web/routers/cluster.py
"""
Master-side cluster API — nodes register and heartbeat here.

All endpoints are node-initiated and bearer-authenticated with NODE_TOKEN. A
master only accepts nodes when it has TRANSCODARR_MODE=master and a NODE_TOKEN set;
otherwise clustering is effectively off and registrations are refused.

Dispatch (handing a job back in the heartbeat reply) is Phase 2 — for now the
heartbeat reply carries no work.
"""
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from transcodarr_core import cluster

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
    """Liveness + current status. Reply will carry a job assignment in Phase 2."""
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
    return {"status": "ok", "assignment": None}


@router.post("/cluster/deregister")
async def api_cluster_deregister(request: Request):
    ok, err = _auth(request)
    if not ok:
        return err
    body = await request.json()
    cluster.remove_node(body.get("node_id"))
    return {"status": "deregistered"}


@router.get("/cluster/nodes")
def api_cluster_nodes(request: Request):
    """Registry snapshot for the UI (no auth — read-only status, same as other UI reads)."""
    nodes = cluster.list_nodes()
    return {
        "mode": (request.app.state.settings.TRANSCODARR_MODE or "master").lower(),
        "clustering_enabled": bool(request.app.state.settings.NODE_TOKEN),
        "nodes": nodes,
        "node_worker_total": cluster.aggregate_worker_count(),
    }
