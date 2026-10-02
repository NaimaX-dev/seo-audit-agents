"""
agents/agent2_crawler.py - Agent 2: Crawler Agent.

Responsibility
--------------
For one ``WebsiteTarget`` (from Agent 1):

1. create a dedicated folder   reports/<slug>/
2. run the external BeyondSEO CLI:
       beyondseo crawl <url> --out reports/<slug>/crawl ...
   (all raw crawl output - pages.csv, links.csv, report.md, summary.json, ... -
   stays inside reports/<slug>/crawl/)
3. read the SEO issues BeyondSEO found (issues.csv, or issues.json as fallback)
4. normalize them and save   reports/<slug>/issues.csv

Normalized issues.csv columns:
    url, code, severity, confidence, evidence, action   (+ any extra columns)
Severity is one of Critical / High / Medium / Low / Info / Unrated, and rows are
sorted most-severe first.

BeyondSEO exit codes: 0 = at least one HTML page extracted, 1 = nothing extracted,
2 = setup/configuration failure. Anything other than 0 is treated as a failed
crawl, because a report built from a failed crawl would wrongly say "0 issues".
"""

import json
import logging
import shutil
import subprocess
import time
from pathlib import Path

import pandas as pd

from agents.models import SEVERITY_RANK, CrawlResult, WebsiteTarget, normalize_severity
from config import Config
from utils.helpers import ensure_within

# Canonical column -> accepted spellings in the crawler's output (lower-case).
ISSUE_COLUMNS = ["url", "code", "severity", "confidence", "evidence", "action"]
COLUMN_ALIASES: dict[str, tuple[str, ...]] = {
    "url": ("url", "page", "page_url", "address", "source_url"),
    "code": ("code", "category", "type", "rule", "check", "issue", "issue_type", "id"),
    "severity": ("severity", "priority", "level", "impact"),
    "confidence": ("confidence",),
    "evidence": ("evidence", "description", "message", "observation", "finding", "details", "detail"),
    "action": ("action", "recommendation", "fix", "suggested_action", "next_step"),
}


class CrawlerError(RuntimeError):
    """Raised when the crawl fails or its output cannot be used."""


class CrawlerAgent:
    name = "Agent2-Crawler"

    def __init__(self, config: Config) -> None:
        self.cfg = config
        self.log = logging.getLogger(self.name)

    # ------------------------------------------------------------------ public
    def preflight(self) -> None:
        """Verify the BeyondSEO CLI is runnable before any crawling starts."""
        cmd = [*self.cfg.beyondseo_cmd, "--version"]
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=120, check=False,
            )
        except FileNotFoundError as exc:
            raise CrawlerError(
                f"BeyondSEO command not found: {cmd[0]!r}. Install BeyondSEO and set "
                "'beyondseo_cmd' in config.json (or SEO_BEYONDSEO_CMD). See README."
            ) from exc
        except (subprocess.TimeoutExpired, OSError) as exc:
            raise CrawlerError(f"Could not run {' '.join(cmd)}: {exc}") from exc

        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout).strip()[:300]
            raise CrawlerError(f"'{' '.join(cmd)}' exited with code {proc.returncode}: {detail}")
        self.log.info("BeyondSEO ready: %s", (proc.stdout or proc.stderr).strip())

    def run(self, target: WebsiteTarget, skip_crawl: bool = False) -> CrawlResult:
        """Crawl ``target`` (unless ``skip_crawl``) and write its normalized issues.csv."""
        started = time.monotonic()
        site_dir = self.cfg.reports_dir / target.slug
        crawl_dir = site_dir / "crawl"
        site_dir.mkdir(parents=True, exist_ok=True)

        if skip_crawl:
            if not crawl_dir.is_dir():
                raise CrawlerError(f"--skip-crawl was set but there is no earlier crawl in {crawl_dir}")
            self.log.info("[%s] Re-using existing crawl output in %s", target.slug, crawl_dir)
        else:
            self._run_crawl(target, site_dir, crawl_dir)

        summary = self._load_summary(crawl_dir)
        self._log_crawl_diagnostics(target, summary)
        if skip_crawl and summary.get("html_documents") == 0:
            # A failed crawl leaves a (nearly) empty issues file behind; reporting
            # on it would wrongly claim the site has no issues.
            raise CrawlerError(f"The earlier crawl in {crawl_dir} extracted no HTML pages; crawl again")

        raw_issues = self._load_raw_issues(crawl_dir)
        issues = self._normalize_issues(raw_issues)

        issues_csv = site_dir / "issues.csv"
        # utf-8-sig so Excel opens the file with the right encoding.
        issues.to_csv(issues_csv, index=False, encoding="utf-8-sig")

        duration = time.monotonic() - started
        self.log.info(
            "[%s] Agent 2 saved %d issue(s) to %s (%.1fs)",
            target.slug, len(issues), issues_csv, duration,
        )
        return CrawlResult(
            target=target,
            site_dir=site_dir,
            crawl_dir=crawl_dir,
            issues_csv=issues_csv,
            issue_count=len(issues),
            duration_seconds=duration,
            summary=summary,
            reused_existing=skip_crawl,
        )

    # --------------------------------------------------------------- crawling
    def _run_crawl(self, target: WebsiteTarget, site_dir: Path, crawl_dir: Path) -> None:
        resume = False
        if crawl_dir.exists() and any(crawl_dir.iterdir()):
            if self.cfg.crawl_fresh:
                # BeyondSEO refuses to write into a non-empty snapshot without
                # --resume, so a fresh crawl starts from an empty folder.
                ensure_within(crawl_dir, self.cfg.reports_dir)  # safety net before rmtree
                self.log.info("[%s] Removing previous crawl output", target.slug)
                shutil.rmtree(crawl_dir)
            else:
                resume = True
        crawl_dir.mkdir(parents=True, exist_ok=True)

        cmd = [
            *self.cfg.beyondseo_cmd, "crawl", target.url,
            "--out", str(crawl_dir),
            "--max-pages", str(self.cfg.crawl_max_pages),
            "--mode", self.cfg.crawl_mode,
            "--workers", str(self.cfg.crawl_workers),
            "--timeout", str(self.cfg.crawl_request_timeout),
            "--retries", str(self.cfg.crawl_retries),
            "--delay", str(self.cfg.crawl_delay),
            "--no-color", "--quiet",
        ]
        if self.cfg.crawl_include_www:
            cmd.append("--include-www")
        if not self.cfg.crawl_use_sitemaps:
            cmd.append("--no-sitemaps")
        if resume:
            cmd.append("--resume")
        cmd.extend(self.cfg.crawl_extra_args)

        self.log.info("[%s] Crawling %s", target.slug, target.url)
        self.log.debug("[%s] Command: %s", target.slug, " ".join(cmd))

        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=self.cfg.crawl_timeout_seconds, check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise CrawlerError(
                f"Crawl timed out after {self.cfg.crawl_timeout_seconds}s "
                "(raise 'crawl_timeout_seconds' or lower 'crawl_max_pages')"
            ) from exc
        except OSError as exc:
            raise CrawlerError(f"Could not start BeyondSEO: {exc}") from exc

        # Keep the raw tool output next to the results for debugging.
        (site_dir / "crawl.log").write_text(
            f"$ {' '.join(cmd)}\n\n[exit code] {proc.returncode}\n\n"
            f"[stdout]\n{proc.stdout}\n\n[stderr]\n{proc.stderr}\n",
            encoding="utf-8",
        )

        if proc.returncode == 0:
            return
        if proc.returncode == 1:
            hint = "no HTML pages could be extracted (site unreachable, blocked, or disallowed by robots.txt)"
        elif proc.returncode == 2:
            hint = "BeyondSEO setup/configuration failed (try running its 'doctor' command)"
        else:
            hint = "unexpected exit code"
        # stdout carries BeyondSEO's summary JSON; only stderr holds error text.
        stderr_lines = proc.stderr.strip().splitlines()
        tail = f" Last error line: {stderr_lines[-1][:200]}." if stderr_lines else ""
        raise CrawlerError(
            f"BeyondSEO exited with code {proc.returncode}: {hint}.{tail} "
            f"(details: {site_dir / 'crawl.log'})"
        )

    # ---------------------------------------------------------- issue loading
    def _load_raw_issues(self, crawl_dir: Path) -> pd.DataFrame:
        """Load BeyondSEO's issues.csv, falling back to issues.json."""
        csv_path = crawl_dir / "issues.csv"
        json_path = crawl_dir / "issues.json"

        try:
            if csv_path.is_file():
                # dtype=str + keep_default_na=False keeps every value exactly as
                # written (no "NA" -> NaN surprises). utf-8-sig strips the BOM.
                return pd.read_csv(csv_path, dtype=str, encoding="utf-8-sig", keep_default_na=False)
            if json_path.is_file():
                data = json.loads(json_path.read_text(encoding="utf-8-sig"))
                if isinstance(data, dict):
                    data = data.get("issues") or data.get("findings") or []
                return pd.DataFrame(data).fillna("").astype(str)
        except pd.errors.EmptyDataError:
            return pd.DataFrame()  # header-less empty file == no issues
        except (OSError, ValueError, pd.errors.ParserError) as exc:
            raise CrawlerError(f"Could not read crawl issues from {crawl_dir}: {exc}") from exc

        raise CrawlerError(f"Crawl finished but produced no issues.csv / issues.json in {crawl_dir}")

    @staticmethod
    def _normalize_issues(raw: pd.DataFrame) -> pd.DataFrame:
        """Rename columns to the canonical names, normalize severity and sort."""
        df = raw.copy()
        df.columns = [str(c).strip().lower() for c in df.columns]

        # Map each canonical column to the first alias that exists (once per column).
        rename: dict[str, str] = {}
        for canonical, aliases in COLUMN_ALIASES.items():
            for alias in aliases:
                if alias in df.columns and alias not in rename:
                    rename[alias] = canonical
                    break
        df = df.rename(columns=rename)

        for column in ISSUE_COLUMNS:
            if column not in df.columns:
                df[column] = ""
        df = df.fillna("").astype(str)

        df["severity"] = df["severity"].map(normalize_severity)
        df = df.drop_duplicates()

        df["_rank"] = df["severity"].map(SEVERITY_RANK)
        df = df.sort_values(["_rank", "code", "url"], kind="stable").drop(columns="_rank")

        extras = [c for c in df.columns if c not in ISSUE_COLUMNS]
        return df[ISSUE_COLUMNS + extras].reset_index(drop=True)

    @staticmethod
    def _load_summary(crawl_dir: Path) -> dict:
        """BeyondSEO's summary.json (coverage, counts, limits); {} if unavailable."""
        try:
            return json.loads((crawl_dir / "summary.json").read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            return {}

    def _log_crawl_diagnostics(self, target: WebsiteTarget, summary: dict) -> None:
        """Explain what actually happened during the crawl, not just how long it took.

        A slow crawl has exactly two likely causes: (1) many pages were rendered
        with a real headless browser (only "auto"/"browser" mode does this - each
        rendered page costs seconds, not milliseconds), or (2) the target is slow
        or rate-limiting, so requests are hitting timeouts/retries. Both are
        visible in BeyondSEO's own summary.json; this logs them so the cause is
        obvious in seo_audit.log instead of having to be guessed from wall-clock
        time alone.
        """
        if not summary:
            return
        used = summary.get("configuration", {})
        diag = summary.get("access_diagnostics", {})
        rendered = summary.get("rendered_documents", 0) or 0

        self.log.info(
            "[%s] Crawl config used: mode=%s workers=%s timeout=%ss retries=%s delay=%ss sitemaps=%s",
            target.slug, used.get("render_mode", "n/a"), used.get("workers", "n/a"),
            used.get("timeout", "n/a"), used.get("retries", "n/a"), used.get("delay", "n/a"),
            used.get("sitemaps", "n/a"),
        )
        self.log.info(
            "[%s] Crawl result: html_pages=%s rendered_with_browser=%s render_errors=%s "
            "failed_or_http_error=%s stop_reason=%s",
            target.slug, summary.get("html_documents", "n/a"), rendered,
            summary.get("render_errors", "n/a"), summary.get("failed_or_http_error_urls", "n/a"),
            ", ".join(diag.get("stop_reasons", [])) or "n/a",
        )

        if rendered:
            self.log.warning(
                "[%s] %d page(s) were rendered with a headless browser (mode=%s). Browser rendering "
                "is the most common cause of very long crawls on CPU-only / low-RAM machines. Set "
                "'crawl_mode': 'http' to skip rendering entirely if the site's SEO-relevant content "
                "does not require JavaScript.",
                target.slug, rendered, used.get("render_mode", "auto"),
            )
        if diag.get("challenge_attempts_recorded") or diag.get("http_429_attempts_recorded") or diag.get("http_access_denied_attempts_recorded"):
            self.log.warning(
                "[%s] Bot-protection signals seen: challenges=%s, HTTP 429=%s, access-denied=%s. "
                "Retries/timeouts on this site may be caused by rate limiting, not slow rendering - "
                "raising 'crawl_delay' or lowering 'crawl_workers' may help more than raising timeouts.",
                target.slug, diag.get("challenge_attempts_recorded", 0),
                diag.get("http_429_attempts_recorded", 0), diag.get("http_access_denied_attempts_recorded", 0),
            )