"""
services/audit_runner.py - runs the existing agent pipeline as a background job.

This module contains NO audit logic of its own. It calls the same agents, in
the same order, as ``main.process_site`` - and reuses main.py's own helpers
(``select_analysis_engine``, ``SiteOutcome``, ``write_run_summary``):

    Agent 1  FileReaderAgent   validates the URL (fed a one-row CSV)
    Agent 2  CrawlerAgent      BeyondSEO crawl -> issues.csv
    Agent 3  AnalysisAgent     Ollama / Qwen3           (or)
    Agent 4  RuleEngineAgent   deterministic rules
    PDF      build_pdf         report.pdf

What it adds is only what a web UI needs: a job queue, stage-by-stage progress,
a live log tail, friendly errors, and a snapshot of the finished files.

Jobs run one at a time (a single worker thread). Crawls and local LLM inference
are both heavy, and every site writes into its own reports/<slug>/ folder, so
serialising them is both faster on a laptop and race-free.
"""

import csv
import logging
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
from datetime import datetime, timezone

import pandas as pd

from agents.agent1_reader import FileReaderAgent
from agents.agent2_crawler import CrawlerAgent
from agents.agent3_analyzer import AnalysisAgent
from agents.agent4_rule_engine import RuleEngineAgent
from config import load_config
from main import SiteOutcome, select_analysis_engine, write_run_summary
from services.errors import explain
from services.job_store import JobStore, utcnow_iso
from services.lead_store import (
    STATUS_AUDITED, STATUS_AUDITING, STATUS_FAILED, LeadStore,
)
from utils.pdf_report import build_pdf

log = logging.getLogger("audit_runner")

# Ordered pipeline stages: key -> (progress % when the stage starts).
STAGE_PROGRESS = {
    "queued": 0,
    "reading": 5,
    "preflight": 10,
    "crawling": 15,
    "analyzing": 70,
    "reporting": 90,
}
STAGE_KEYS = list(STAGE_PROGRESS)

# Loggers whose records are mirrored into the job's live log.
_JOB_LOGGERS = {
    "Agent1-FileReader", "Agent2-Crawler", "Agent3-Analyzer", "Agent4-RuleEngine",
    "PDFReport", "audit_runner",
}

# Files copied next to job.json when an audit completes: kind -> file name.
DELIVERABLES = {
    "issues": "issues.csv",
    "report": "report.pdf",
    "summary": "run_summary.csv",
}


class _JobLogHandler(logging.Handler):
    """Copies agent log lines into the running job so the browser can show them."""

    def __init__(self, store: JobStore, job_id: str) -> None:
        super().__init__(level=logging.INFO)
        self.store, self.job_id = store, job_id

    def emit(self, record: logging.LogRecord) -> None:
        if record.name not in _JOB_LOGGERS:
            return
        try:
            stamp = datetime.fromtimestamp(record.created).strftime("%H:%M:%S")
            self.store.append_log(
                self.job_id, f"{stamp} {record.levelname:<7} {record.getMessage()}"
            )
        except Exception:  # noqa: BLE001 - logging must never break the audit
            self.handleError(record)


class AuditRunner:
    def __init__(self, store: JobStore) -> None:
        self.store = store
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="audit")

    def submit(self, job_id: str) -> None:
        self._executor.submit(self._run_safely, job_id)

    def shutdown(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)

    # ------------------------------------------------------------- wrapper
    def _run_safely(self, job_id: str) -> None:
        handler = _JobLogHandler(self.store, job_id)
        logging.getLogger().addHandler(handler)
        started = time.monotonic()
        try:
            self._run(job_id)
        except Exception as exc:  # noqa: BLE001 - every failure becomes a friendly job error
            log.exception("[job %s] Audit failed", job_id)
            friendly = explain(exc)
            job = self.store.get(job_id) or {}
            self.store.update(
                job_id, status="failed", finished_at=utcnow_iso(),
                message=friendly.title, error=friendly.to_dict(),
                stage=job.get("stage", "queued"),
            )
            self._lead_status(job, STATUS_FAILED, audit_date=utcnow_iso()[:10])
            log.error("[job %s] %s - %s (%.1fs)", job_id, friendly.title, friendly.message,
                      time.monotonic() - started)
        finally:
            logging.getLogger().removeHandler(handler)

    def _lead_status(self, job: dict | None, status: str, audit_date: str | None = None) -> None:
        """Keep data/leads.csv in step with an audit started from Lead Discovery.

        Does nothing for manual audits, and never lets bookkeeping break an audit.
        """
        lead = (job or {}).get("lead")
        if not lead:
            return
        try:
            cfg = load_config()
            LeadStore(cfg.leads_csv).set_status(lead["id"], status, job_id=job["id"], audit_date=audit_date)
        except Exception:  # noqa: BLE001
            log.warning("Could not update lead status for job %s", (job or {}).get("id"), exc_info=True)

    def _stage(self, job_id: str, stage: str, message: str) -> None:
        self.store.update(job_id, stage=stage, progress=STAGE_PROGRESS[stage], message=message)
        log.info("[job %s] %s", job_id, message)

    # ------------------------------------------------------------- pipeline
    def _run(self, job_id: str) -> None:
        job = self.store.get(job_id)
        if job is None:
            return
        self.store.update(job_id, status="running", started_at=utcnow_iso())
        self._lead_status(job, STATUS_AUDITING)
        started = time.monotonic()

        # Re-read config.json for every audit so edits apply without a restart.
        cfg = load_config()
        if job.get("max_pages"):
            cfg = replace(cfg, crawl_max_pages=int(job["max_pages"]))
        engine = select_analysis_engine(cfg, job.get("engine_requested"))
        self.store.update(job_id, engine=engine)
        cfg.reports_dir.mkdir(parents=True, exist_ok=True)

        # ---- Agent 1: read + validate the URL (through a one-row CSV, like the CLI)
        self._stage(job_id, "reading", "Agent 1: validating the website address")
        job_dir = self.store.job_dir(job_id)
        input_csv = job_dir / "input.csv"
        with input_csv.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            writer.writerow(["url"])
            writer.writerow([job["url"]])
        target = FileReaderAgent(replace(cfg, input_csv=input_csv)).run()[0]
        self.store.update(job_id, slug=target.slug)

        # ---- set up Agent 2 and the chosen analysis agent (fail fast, before the long crawl)
        self._stage(job_id, "preflight", "Checking that the crawler and analysis engine are ready")
        crawler = CrawlerAgent(cfg)
        crawler.preflight()

        analyzer: AnalysisAgent | RuleEngineAgent
        if engine == "agent4":
            analyzer = RuleEngineAgent(cfg)
            analyzer.preflight()
        else:
            analyzer = AnalysisAgent(cfg, use_llm=True)
            if not analyzer.preflight():
                reason = getattr(analyzer, "_llm_disabled_reason", None) or "Ollama is unavailable"
                if not cfg.llm_fallback:
                    raise RuntimeError(f"LLM unavailable and llm_fallback is off: {reason}")
                self.store.add_warning(
                    job_id,
                    f"Ollama is not available ({reason}). A rule-based analysis will be used instead.",
                )

        # ---- Agent 2: crawl (Agent 3's model loads in the background meanwhile, as in main.py)
        self._stage(job_id, "crawling", f"Agent 2: crawling {target.url} with BeyondSEO "
                                        f"(up to {cfg.crawl_max_pages} pages)")
        warm_up = threading.Thread(target=analyzer.warm_up, daemon=True)
        warm_up.start()
        crawl = crawler.run(target)
        warm_up.join(timeout=cfg.ollama_timeout_seconds)

        # ---- Agent 3 / Agent 4: analysis
        label = "Agent 3 (Ollama)" if engine == "agent3" else "Agent 4 (rules)"
        self._stage(job_id, "analyzing", f"{label}: analysing {crawl.issue_count} issue(s)")
        analysis = analyzer.analyze(target, crawl.issues_csv)
        if analysis.llm_error:
            self.store.add_warning(
                job_id,
                f"The AI analysis could not be completed ({analysis.llm_error}). "
                "The summary and recommendations below were generated from rules instead.",
            )

        # ---- PDF report + run summary
        self._stage(job_id, "reporting", "Building the PDF report")
        issues = pd.read_csv(crawl.issues_csv, dtype=str, encoding="utf-8-sig", keep_default_na=False)
        build_pdf(crawl.site_dir / "report.pdf", target, crawl, analysis, issues, cfg,
                  lead=job.get("lead"))
        outcome = SiteOutcome(
            url=target.url, folder=str(crawl.site_dir), status="ok",
            issues=analysis.total_issues, analysis=analysis.source,
        )
        # main.write_run_summary writes <dir>/run_summary.csv; pointing it at the site
        # folder keeps this audit's summary separate from the CLI's batch summary.
        write_run_summary([outcome], crawl.site_dir)

        # ---- snapshot the deliverables so this job's downloads never change
        for name in DELIVERABLES.values():
            src = crawl.site_dir / name
            if src.is_file():
                shutil.copy2(src, job_dir / name)
            else:
                self.store.add_warning(job_id, f"{name} was not produced by this audit.")

        crawl_summary = crawl.summary or {}
        if crawl_summary.get("coverage_limited"):
            self.store.add_warning(
                job_id,
                f"The crawl stopped at the page limit ({cfg.crawl_max_pages}). "
                "Raise the page limit to cover more of the site.",
            )

        result = {
            "site_url": target.url,
            "slug": target.slug,
            "engine": engine,
            "source": analysis.source,
            "duration_seconds": round(time.monotonic() - started, 1),
            "completed_at": utcnow_iso(),
            "crawl": {
                "pages_crawled": crawl_summary.get("html_documents"),
                "urls_attempted": crawl_summary.get("attempted_urls"),
                "failed_urls": crawl_summary.get("failed_or_http_error_urls"),
                "coverage_limited": bool(crawl_summary.get("coverage_limited")),
                "coverage_note": crawl_summary.get("coverage_note", ""),
                "crawler_version": crawl_summary.get("engine_version", ""),
                "max_pages": cfg.crawl_max_pages,
                "crawl_seconds": round(crawl.duration_seconds, 1),
            },
            "analysis": asdict(analysis),
        }
        self.store.update(
            job_id, status="completed", stage="reporting", progress=100,
            finished_at=utcnow_iso(), message="Audit complete", result=result,
        )
        self._lead_status(job, STATUS_AUDITED, audit_date=utcnow_iso()[:10])
        log.info("[job %s] Finished in %.1fs", job_id, time.monotonic() - started)
