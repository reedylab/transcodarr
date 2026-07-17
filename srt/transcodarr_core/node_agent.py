# srt/transcodarr_core/node_agent.py
"""
Node-side agent. In TRANSCODARR_MODE=node, this connects out to the master:
probe local hardware, verify shared storage, register, then heartbeat.

Node-initiated by design — the master never dials the node, so this works behind
NAT and needs no inbound ports. Dispatch (acting on a job handed back in the
heartbeat reply) is Phase 2; for now the agent just keeps the node present in the
master's registry with its capabilities and worker count.
"""
from __future__ import annotations

import logging
import os
import socket
import threading

import requests

from .config import Settings, get_media_paths
from .ffmpeg.capabilities import detect_capabilities


class NodeAgent:
    def __init__(self, settings: Settings):
        self.s = settings
        self.node_id = settings.NODE_ID or socket.gethostname()
        self.master = (settings.MASTER_URL or "").rstrip("/")
        self.token = settings.NODE_TOKEN or ""
        # What this node offers to run concurrently. Reuses AUTO_WORKERS (env on a
        # node, since it has no DB) — the pool that will execute jobs in Phase 2.
        try:
            self.worker_count = max(0, int(self.s.AUTO_WORKERS))
        except (ValueError, TypeError):
            self.worker_count = 2
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._interval = 5.0
        self._registered = False

    # ----- checks -----

    def storage_check(self) -> tuple[bool, str]:
        """Verify this node can actually see the shared media at the master's paths.

        Because a node runs the same image with the same media-path env, these are
        the same container paths the master uses; if they're not visible here, the
        node can't run any job and says so instead of failing encodes silently.
        """
        paths = get_media_paths(self.s)
        issues: list[str] = []
        for key in ("movies_watch", "tv_watch"):
            p = paths.get(key)
            if p and not os.path.isdir(p):
                issues.append(f"{key} not visible")
        for key in ("movies_output", "tv_output"):
            p = paths.get(key)
            if not p:
                continue
            if not os.path.isdir(p):
                issues.append(f"{key} not visible")
            elif not os.access(p, os.W_OK):
                issues.append(f"{key} not writable")
        temp = self.s.MEDIA_TEMP_FOLDER
        if temp and not (os.path.isdir(temp) and os.access(temp, os.W_OK)):
            issues.append("temp not writable")
        if issues:
            return False, "; ".join(issues)
        return True, "shared storage visible"

    # ----- master conversation -----

    def _headers(self):
        return {"Authorization": f"Bearer {self.token}"}

    def _register(self):
        storage_ok, detail = self.storage_check()
        body = {
            "node_id": self.node_id,
            "address": self.s.TRANSCODARR_URL or "",
            "capabilities": detect_capabilities(),
            "worker_count": self.worker_count,
            "storage_ok": storage_ok,
            "storage_detail": detail,
        }
        r = requests.post(f"{self.master}/api/cluster/register",
                          json=body, headers=self._headers(), timeout=10)
        r.raise_for_status()
        self._interval = float(r.json().get("heartbeat_interval_s", 5))
        self._registered = True
        logging.info("[NODE] registered with master %s as %r — %d workers, storage_ok=%s%s",
                     self.master, self.node_id, self.worker_count, storage_ok,
                     "" if storage_ok else f" ({detail})")

    def _heartbeat(self):
        body = {"node_id": self.node_id, "worker_count": self.worker_count, "jobs": []}
        r = requests.post(f"{self.master}/api/cluster/heartbeat",
                          json=body, headers=self._headers(), timeout=10)
        if r.status_code == 409:  # master lost us (restart) — re-register next tick
            self._registered = False
            logging.info("[NODE] master asked us to re-register")
            return
        r.raise_for_status()
        # Phase 2: act on r.json().get("assignment")

    def _loop(self):
        while not self._stop.is_set():
            try:
                self._register() if not self._registered else self._heartbeat()
            except Exception as e:
                if self._registered:
                    logging.warning("[NODE] master unreachable (%s) — will retry", e)
                self._registered = False
            self._stop.wait(self._interval)

    # ----- lifecycle -----

    def start(self):
        if not self.master:
            logging.error("[NODE] MASTER_URL not set — cannot connect to a master")
            return
        if not self.token:
            logging.error("[NODE] NODE_TOKEN not set — cannot authenticate to the master")
            return
        self._thread = threading.Thread(target=self._loop, daemon=True, name="node-agent")
        self._thread.start()
        logging.info("[NODE] agent started, target master %s (heartbeat ~%.0fs)",
                     self.master, self._interval)

    def stop(self):
        self._stop.set()
        try:  # best-effort clean deregister
            requests.post(f"{self.master}/api/cluster/deregister",
                          json={"node_id": self.node_id}, headers=self._headers(), timeout=3)
        except Exception:
            pass
