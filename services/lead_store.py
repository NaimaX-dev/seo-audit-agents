"""
services/lead_store.py - persistent storage for Agent 5's leads.

    data/leads.csv                  one row per business (the lead list)
    data/lead_discoveries.json      one entry per "Find Businesses" run
    data/last_discovery.csv         the latest run, exactly as Agent 5 exported it
    data/lead_businesses.json       identities of every business Google ever returned
                                    (so "Total leads" does not double count repeat searches)

leads.csv columns:
    business_name, address, phone, website, audit_status, audit_date,
    job_id, keyword, location, discovered_at

The first six are the columns asked for; job_id links a lead to its audit in the
existing job history, keyword/location/discovered_at remember where it came from.

audit_status is one of: Not audited | Queued | Auditing | Audited | Failed

A lead is identified by its website (host + path, ignoring www and a trailing
slash), so discovering the same business twice updates the row instead of
duplicating it, and keeps its audit status.
"""

import csv
import hashlib
import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

from agents.agent5_google_maps import DiscoveryResult, website_key

COLUMNS = [
    "business_name", "address", "phone", "website", "audit_status", "audit_date",
    "job_id", "keyword", "location", "discovered_at",
]
STATUS_NOT_AUDITED = "Not audited"
STATUS_QUEUED = "Queued"
STATUS_AUDITING = "Auditing"
STATUS_AUDITED = "Audited"
STATUS_FAILED = "Failed"
ACTIVE_STATUSES = {STATUS_QUEUED, STATUS_AUDITING}
MAX_LOG_ENTRIES = 200

_lock = threading.RLock()


def lead_id(website: str) -> str:
    """Short, URL-safe id for a lead, derived from its website."""
    return hashlib.sha1(website_key(website).encode("utf-8")).hexdigest()[:10]


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class LeadStore:
    def __init__(self, leads_csv: Path) -> None:
        self.path = Path(leads_csv)
        self.log_path = self.path.with_name("lead_discoveries.json")
        self.last_export_path = self.path.with_name("last_discovery.csv")
        self.seen_path = self.path.with_name("lead_businesses.json")
        self._ensure_file()

    # ------------------------------------------------------------------ read
    def all(self) -> list[dict]:
        with _lock:
            return [self._with_id(row) for row in self._read()]

    def get(self, lid: str) -> dict | None:
        return next((row for row in self.all() if row["id"] == lid), None)

    def latest_batch(self) -> list[dict]:
        """Leads from the most recent "Find Businesses" run, in discovery order."""
        runs = self.discoveries()
        if not runs:
            return []
        batch = runs[-1]["at"]
        return [row for row in self.all() if row["discovered_at"] == batch]

    def discoveries(self) -> list[dict]:
        with _lock:
            try:
                data = json.loads(self.log_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return []
        return data if isinstance(data, list) else []

    # ----------------------------------------------------------------- write
    def add_discovery(self, result: DiscoveryResult) -> tuple[str, int, int]:
        """Save a discovery run. Returns (batch_stamp, new_leads, updated_leads)."""
        stamp = utcnow_iso()
        with _lock:
            rows = self._read()
            by_key = {website_key(r["website"]): r for r in rows}
            new = updated = 0
            for lead in result.leads:
                key = website_key(lead.website)
                existing = by_key.get(key)
                if existing is None:
                    row = {c: "" for c in COLUMNS}
                    row["audit_status"] = STATUS_NOT_AUDITED
                    rows.append(row)
                    by_key[key] = row
                    new += 1
                else:
                    row, updated = existing, updated + 1
                row.update(
                    business_name=lead.business_name, address=lead.address,
                    phone=lead.phone, website=lead.website,
                    keyword=result.keyword, location=result.location, discovered_at=stamp,
                )
            self._write(rows)

            runs = self.discoveries()
            runs.append({
                "at": stamp, "keyword": result.keyword, "location": result.location,
                "businesses_found": result.businesses_found,
                "with_website": len(result.leads),
                "new": new,
            })
            self._write_log(runs[-MAX_LOG_ENTRIES:])
            seen = self._seen() | set(result.business_keys)
            self._atomic_json(self.seen_path, sorted(seen))
        return stamp, new, updated

    def set_status(self, lid: str, status: str, job_id: str | None = None,
                   audit_date: str | None = None) -> bool:
        with _lock:
            rows = self._read()
            for row in rows:
                if lead_id(row["website"]) == lid:
                    row["audit_status"] = status
                    if job_id is not None:
                        row["job_id"] = job_id
                    if audit_date is not None:
                        row["audit_date"] = audit_date
                    self._write(rows)
                    return True
        return False

    # --------------------------------------------------------------- summary
    def summary(self, job_store) -> dict:
        """Numbers for the dashboard cards (read-only)."""
        rows = self.all()
        runs = self.discoveries()
        audited = [r for r in rows if r["audit_status"] == STATUS_AUDITED]
        potential = 0
        for row in audited:
            job = job_store.get(row["job_id"]) if row["job_id"] else None
            breakdown = (((job or {}).get("result") or {}).get("analysis") or {}).get("severity_breakdown") or {}
            if breakdown.get("Critical", 0) + breakdown.get("High", 0) > 0:
                potential += 1
        latest = runs[-1] if runs else None
        return {
            "total_leads": len(self._seen()),
            "with_website": len(rows),
            "audited": len(audited),
            "potential_clients": potential,
            "latest": latest,
            "runs": len(runs),
        }

    # -------------------------------------------------------------- internals
    @staticmethod
    def _with_id(row: dict) -> dict:
        out = dict(row)
        out["id"] = lead_id(row["website"])
        return out

    def _ensure_file(self) -> None:
        with _lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if not self.path.is_file() or self.path.stat().st_size == 0:
                self._write([])

    def _read(self) -> list[dict]:
        try:
            with self.path.open(newline="", encoding="utf-8-sig") as fh:
                rows = list(csv.DictReader(fh))
        except OSError:
            return []
        return [{c: (row.get(c) or "") for c in COLUMNS} for row in rows if (row.get("website") or "").strip()]

    def _write(self, rows: list[dict]) -> None:
        tmp = self.path.with_suffix(".csv.tmp")
        with tmp.open("w", newline="", encoding="utf-8-sig") as fh:
            writer = csv.DictWriter(fh, fieldnames=COLUMNS)
            writer.writeheader()
            writer.writerows({c: row.get(c, "") for c in COLUMNS} for row in rows)
        os.replace(tmp, self.path)

    def _write_log(self, runs: list[dict]) -> None:
        self._atomic_json(self.log_path, runs)

    def _seen(self) -> set[str]:
        try:
            data = json.loads(self.seen_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return set()
        return {str(x) for x in data} if isinstance(data, list) else set()

    @staticmethod
    def _atomic_json(path: Path, payload) -> None:
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, path)
