# srt/transcodarr_core/node_agent.py
"""
Node-side agent. In TRANSCODARR_MODE=node, this connects out to the master:
probe local hardware, verify shared storage, register, then heartbeat.

Node-initiated by design — the master never dials the node, so this works behind
NAT and needs no inbound ports. When the heartbeat reply carries a job assignment,
the agent runs the pure-ffmpeg EXECUTE phase on the shared storage, streams progress
back, and reports completion/failure so the master can post-process.
"""
from __future__ import annotations

import contextlib
import json
import logging
import os
import socket
import threading
from concurrent.futures import ThreadPoolExecutor

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
        # Jobs this node is currently executing: job_id -> last progress (0-100).
        self._jobs: dict[str, float] = {}
        self._jobs_lock = threading.Lock()
        self._executor: ThreadPoolExecutor | None = None

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

    def status(self) -> dict:
        """Node's own state for its dormant status page."""
        storage_ok, detail = self.storage_check()
        try:
            caps = detect_capabilities()
            backends = [b["id"] for b in caps.get("backends", []) if b.get("available")]
        except Exception:
            backends = []
        return {
            "node_id": self.node_id,
            "master_url": self.master,
            "registered": self._registered,
            "worker_count": self.worker_count,
            "storage_ok": storage_ok,
            "storage_detail": detail,
            "backends": backends,
        }

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
        with self._jobs_lock:
            running = list(self._jobs.keys())
        body = {"node_id": self.node_id, "worker_count": self.worker_count, "jobs": running}
        r = requests.post(f"{self.master}/api/cluster/heartbeat",
                          json=body, headers=self._headers(), timeout=10)
        if r.status_code == 409:  # master lost us (restart) — re-register next tick
            self._registered = False
            logging.info("[NODE] master asked us to re-register")
            return
        r.raise_for_status()
        assignment = (r.json() or {}).get("assignment")
        if assignment:
            self._accept_job(assignment)

    # ----- job execution -----

    def _accept_job(self, job: dict):
        """Take a job the master handed us and run it on the shared storage."""
        job_id = job.get("id")
        if not job_id:
            return
        with self._jobs_lock:
            if job_id in self._jobs:
                return  # already running (duplicate assignment)
            self._jobs[job_id] = 0.0
        logging.info("[NODE] accepted job %s (%s)", job_id, job.get("base_name"))
        if self._executor:
            self._executor.submit(self._run_job, job)
        else:  # agent stopping
            with self._jobs_lock:
                self._jobs.pop(job_id, None)

    def _run_job(self, job: dict):
        job_id = job["id"]
        temp_root = self.s.MEDIA_TEMP_FOLDER or "/temp"
        progress_file = os.path.join(temp_root, f".node-{job_id}.progress.json")
        # Seed the progress file so the streamer has something to read from the start.
        with contextlib.suppress(Exception):
            with open(progress_file, "w", encoding="utf-8") as f:
                json.dump({"progress": 0.0}, f)
        stop_prog = threading.Event()
        pt = threading.Thread(target=self._stream_progress,
                              args=(job_id, progress_file, stop_prog),
                              daemon=True, name=f"node-prog-{job_id}")
        pt.start()
        ok = False
        try:
            from .pipeline import _execute_transcode
            ok = _execute_transcode(
                job["input_path"], job.get("srt_path"), job["tmp_out_path"],
                job["base_name"], self.s, progress_file,
                settings_override=job.get("settings_override") or {},
                register_path="",
            )
        except Exception as e:
            logging.error("[NODE] job %s crashed: %s", job_id, e)
            ok = False
        finally:
            stop_prog.set()
            with contextlib.suppress(Exception):
                if os.path.exists(progress_file):
                    os.remove(progress_file)
            with self._jobs_lock:
                self._jobs.pop(job_id, None)
        self._report_job(job_id, "complete" if ok else "failed")

    def _stream_progress(self, job_id: str, progress_file: str, stop: threading.Event):
        """Poll the ffmpeg progress file and stream the percentage to the master."""
        last = -1.0
        while not stop.wait(2.0):
            pct = 0.0
            with contextlib.suppress(Exception):
                with open(progress_file, "r", encoding="utf-8") as f:
                    pct = float(json.load(f).get("progress") or 0.0)
            with self._jobs_lock:
                if job_id in self._jobs:
                    self._jobs[job_id] = pct
            if pct != last:
                last = pct
                self._report_job(job_id, "progress", {"progress": pct})

    def _report_job(self, job_id: str, kind: str, extra: dict | None = None):
        body = {"node_id": self.node_id}
        if extra:
            body.update(extra)
        try:
            requests.post(f"{self.master}/api/cluster/job/{job_id}/{kind}",
                          json=body, headers=self._headers(), timeout=10)
        except Exception as e:
            logging.debug("[NODE] job %s %s report failed: %s", job_id, kind, e)

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
        # Pool that executes assigned jobs, sized to what this node advertises.
        self._executor = ThreadPoolExecutor(max_workers=max(1, self.worker_count),
                                            thread_name_prefix="node-job")
        self._thread = threading.Thread(target=self._loop, daemon=True, name="node-agent")
        self._thread.start()
        logging.info("[NODE] agent started, target master %s (heartbeat ~%.0fs, %d worker(s))",
                     self.master, self._interval, self.worker_count)

    def stop(self):
        self._stop.set()
        if self._executor:
            self._executor.shutdown(wait=False)
            self._executor = None
        try:  # best-effort clean deregister
            requests.post(f"{self.master}/api/cluster/deregister",
                          json={"node_id": self.node_id}, headers=self._headers(), timeout=3)
        except Exception:
            pass
