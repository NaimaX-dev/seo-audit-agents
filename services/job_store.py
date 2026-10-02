"""
services/job_store.py - persistent, thread-safe storage for audit jobs.

Every web-started audit is a "job" stored as one JSON file:

    reports/_jobs/<job_id>/job.json      status, stage, log tail, result summary
    reports/_jobs/<job_id>/input.csv     the one-row CSV handed to Agent 1
    reports/_jobs/<job_id>/issues.csv    snapshot of the finished audit's files,
    reports/_jobs/<job_id>/report.pdf    so downloads keep working even if the
    reports/_jobs/<job_id>/run_summary.csv   same site is audited again later

Active jobs are cached in memory (the worker updates them several times a
second); every change is also written to disk so jobs survive a page reload.
"""

import copy
import json
import os
import re
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

JOB_ID_RE = re.compile(r"^[a-f0-9]{12}$")
ACTIVE_STATUSES = {"queued", "running"}
MAX_LOG_LINES = 300
_LOG_FLUSH_INTERVAL = 0.5  # seconds between disk writes caused by log lines


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class JobStore:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._cache: dict[str, dict] = {}
        self._last_flush: dict[str, float] = {}

    # ----------------------------------------------------------------- paths
    def job_dir(self, job_id: str) -> Path:
        if not JOB_ID_RE.match(job_id):
            raise ValueError(f"Invalid job id: {job_id!r}")
        return self.root / job_id

    def _job_file(self, job_id: str) -> Path:
        return self.job_dir(job_id) / "job.json"

    # ---------------------------------------------------------------- create
    def create(self, url: str, engine_requested: str | None, max_pages: int | None,
               lead: dict | None = None) -> dict:
        with self._lock:
            job_id = uuid.uuid4().hex[:12]
            self.job_dir(job_id).mkdir(parents=True, exist_ok=True)
            job = {
                "id": job_id,
                "url": url,
                "slug": None,
                "status": "queued",           # queued | running | completed | failed
                "stage": "queued",            # see services.audit_runner.STAGE_KEYS
                "progress": 0,
                "message": "Waiting for a free worker...",
                "engine_requested": engine_requested,
                "engine": None,               # agent3 | agent4 (resolved when the job starts)
                "max_pages": max_pages,
                "created_at": utcnow_iso(),
                "started_at": None,
                "finished_at": None,
                "warnings": [],
                "error": None,                # {title, message, hint}
                "result": None,
                "log": [],
                # Set only for audits started from Lead Discovery (Agent 5):
                # {id, business_name, address, phone, website}. None for manual audits.
                "lead": lead,
            }
            self._cache[job_id] = job
            self._persist(job)
            return copy.deepcopy(job)

    # ------------------------------------------------------------------ read
    def get(self, job_id: str) -> dict | None:
        if not JOB_ID_RE.match(job_id or ""):
            return None
        with self._lock:
            cached = self._cache.get(job_id)
            if cached is not None:
                return copy.deepcopy(cached)
            return self._read_from_disk(job_id)

    def list(self, limit: int | None = None) -> list[dict]:
        """All jobs, newest first."""
        with self._lock:
            jobs = []
            for entry in self.root.iterdir():
                if entry.is_dir() and JOB_ID_RE.match(entry.name):
                    job = self.get(entry.name)
                    if job:
                        jobs.append(job)
        jobs.sort(key=lambda j: j.get("created_at") or "", reverse=True)
        return jobs[:limit] if limit else jobs

    def find_active_for_url(self, url: str) -> dict | None:
        for job in self.list():
            if job["status"] in ACTIVE_STATUSES and job["url"] == url:
                return job
        return None

    def count_active(self) -> int:
        return sum(1 for job in self.list() if job["status"] in ACTIVE_STATUSES)

    # ---------------------------------------------------------------- update
    def update(self, job_id: str, **fields) -> dict | None:
        with self._lock:
            job = self._cache.get(job_id) or self._read_from_disk(job_id)
            if job is None:
                return None
            job.update(fields)
            self._cache[job_id] = job
            self._persist(job)
            if job["status"] not in ACTIVE_STATUSES:
                self._cache.pop(job_id, None)   # finished: disk is the source of truth
                self._last_flush.pop(job_id, None)
            return copy.deepcopy(job)

    def add_warning(self, job_id: str, text: str) -> None:
        with self._lock:
            job = self._cache.get(job_id) or self._read_from_disk(job_id)
            if job is None or text in job["warnings"]:
                return
            job["warnings"].append(text)
            self._cache[job_id] = job
            self._persist(job)

    def append_log(self, job_id: str, line: str) -> None:
        with self._lock:
            job = self._cache.get(job_id)
            if job is None:
                return
            job["log"].append(line)
            del job["log"][:-MAX_LOG_LINES]
            now = time.monotonic()
            if now - self._last_flush.get(job_id, 0.0) >= _LOG_FLUSH_INTERVAL:
                self._persist(job)

    # --------------------------------------------------------------- restart
    def mark_interrupted(self) -> int:
        """Jobs that were queued/running when the server stopped can never finish."""
        fixed = 0
        with self._lock:
            for job in self.list():
                if job["status"] in ACTIVE_STATUSES:
                    self.update(
                        job["id"], status="failed", finished_at=utcnow_iso(),
                        message="The server was restarted while this audit was running.",
                        error={
                            "title": "Audit interrupted",
                            "message": "The web server was restarted before this audit finished.",
                            "hint": "Start the audit again.",
                        },
                    )
                    fixed += 1
        return fixed

    # -------------------------------------------------------------- internals
    def _read_from_disk(self, job_id: str) -> dict | None:
        path = self._job_file(job_id)
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def _persist(self, job: dict) -> None:
        path = self._job_file(job["id"])
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(job, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, path)   # atomic: a reader never sees a half-written file
        self._last_flush[job["id"]] = time.monotonic()
