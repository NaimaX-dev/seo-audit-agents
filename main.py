"""
main.py - orchestrates the SEO audit pipeline.

    data/websites.csv
      -> Agent 1  (read + validate URLs)
      -> Agent 2  (BeyondSEO crawl -> reports/<site>/crawl/ + issues.csv)
      -> Agent 3  (Ollama Qwen3 analysis of issues.csv)          -- or --
      -> Agent 4  (deterministic rule-based analysis of issues.csv, no LLM)
      -> PDF report (reports/<site>/report.pdf)

Exactly one analysis agent runs per audit:

    Agent1 -> Agent2 -> Agent3 -> PDF     (LLM analysis; config: enable_agent3=true)
    Agent1 -> Agent2 -> Agent4 -> PDF     (rule-based analysis; config: enable_agent4=true)

Which one runs is controlled by the 'enable_agent3' / 'enable_agent4' options in
config.json (see config.py), or overridden for a single run with --engine.

Every website is processed independently: a failure on one site is logged and
recorded, and the run continues with the next one.

Usage:
    python main.py                          # audit everything in data/websites.csv
    python main.py --limit 1                # only the first website
    python main.py --skip-crawl             # re-analyse / re-report existing crawls
    python main.py --no-llm                 # Agent 3's own rule-based fallback (still Agent 3)
    python main.py --engine agent4          # force Agent 4 (rule-based) for this run
    python main.py --engine agent3          # force Agent 3 (LLM) for this run
    python main.py --input my.csv --max-pages 30 --model qwen3:14b

Exit codes: 0 = all sites succeeded, 1 = at least one site failed, 2 = could not start.
"""

import argparse
import logging
import sys
import threading
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from agents.agent1_reader import FileReaderAgent, FileReaderError
from agents.agent2_crawler import CrawlerAgent, CrawlerError
from agents.agent3_analyzer import AnalysisAgent
from agents.agent4_rule_engine import RuleEngineAgent
from agents.models import WebsiteTarget
from config import Config, ConfigError, load_config
from utils.logging_setup import setup_logging
from utils.pdf_report import build_pdf

log = logging.getLogger("main")


@dataclass
class SiteOutcome:
    """One row of the end-of-run summary."""

    url: str
    folder: str
    status: str          # "ok" or "failed"
    issues: int = 0
    analysis: str = ""   # "ollama:qwen3:8b" / "rule-based"
    message: str = ""


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="seo-audit-agents",
        description="Multi-agent SEO audit: BeyondSEO crawl + Ollama (Qwen3) analysis + PDF report.",
    )
    parser.add_argument("--input", type=Path, help="CSV with a 'url' column (default: data/websites.csv)")
    parser.add_argument("--output-dir", type=Path, help="where per-website folders are created (default: reports/)")
    parser.add_argument("--config", type=Path, help="JSON config file (default: config.json if present)")
    parser.add_argument("--max-pages", type=int, help="max pages BeyondSEO crawls per website")
    parser.add_argument("--model", help="Ollama model name, e.g. qwen3:8b")
    parser.add_argument("--limit", type=int, help="only process the first N websites")
    parser.add_argument("--skip-crawl", action="store_true",
                        help="reuse existing crawl output in reports/<site>/crawl/ instead of crawling")
    parser.add_argument("--no-llm", action="store_true",
                        help="when Agent 3 runs, skip Ollama and use its own rule-based fallback")
    parser.add_argument("--engine", choices=["agent3", "agent4"],
                        help="override which analysis agent runs this pipeline "
                             "(default: from config's enable_agent3 / enable_agent4)")
    parser.add_argument("--log-level", choices=["DEBUG", "INFO", "WARNING", "ERROR"], help="console log level")
    return parser.parse_args(argv)


def select_analysis_engine(cfg: Config, requested: str | None) -> str:
    """Decide whether Agent 3 or Agent 4 analyses this run's crawls.

    ``requested`` (from --engine) must be enabled in config to be used. Without
    it, Agent 4 is preferred when both are enabled (it is faster and needs no
    LLM); otherwise whichever single agent is enabled runs. load_config()
    already guarantees at least one of enable_agent3 / enable_agent4 is True.
    """
    if requested == "agent3":
        if not cfg.enable_agent3:
            raise ConfigError("--engine agent3 was requested but 'enable_agent3' is false in config.")
        return "agent3"
    if requested == "agent4":
        if not cfg.enable_agent4:
            raise ConfigError("--engine agent4 was requested but 'enable_agent4' is false in config.")
        return "agent4"
    return "agent4" if cfg.enable_agent4 else "agent3"


def process_site(
    target: WebsiteTarget,
    crawler: CrawlerAgent,
    analyzer: AnalysisAgent | RuleEngineAgent,
    cfg: Config,
    skip_crawl: bool,
) -> SiteOutcome:
    """Run Agent 2 -> (Agent 3 or Agent 4) -> PDF for one website.

    ``analyzer`` is whichever analysis agent this run selected; both expose the
    same warm_up()/analyze() interface so this function doesn't need to know
    which one it got. Loading Qwen3 from disk into RAM is the slowest part of
    the *first* Ollama call, so Agent 3's warm-up is kicked off here in a
    background thread as soon as the crawl starts, overlapping the load with
    the (much longer) crawl instead of eating into the analysis timeout
    afterwards. Agent 4's warm_up() is a no-op, so this costs nothing when it
    is the one selected.
    """
    warm_up_thread = threading.Thread(target=analyzer.warm_up, daemon=True)
    warm_up_thread.start()

    crawl = crawler.run(target, skip_crawl=skip_crawl)                  # Agent 2
    warm_up_thread.join(timeout=cfg.ollama_timeout_seconds)             # usually already finished

    analysis = analyzer.analyze(target, crawl.issues_csv)               # Agent 3 or Agent 4
    issues = pd.read_csv(crawl.issues_csv, dtype=str, encoding="utf-8-sig", keep_default_na=False)
    build_pdf(crawl.site_dir / "report.pdf", target, crawl, analysis, issues, cfg)
    return SiteOutcome(
        url=target.url, folder=str(crawl.site_dir), status="ok",
        issues=analysis.total_issues, analysis=analysis.source,
    )


def write_run_summary(outcomes: list[SiteOutcome], reports_dir: Path) -> Path:
    path = reports_dir / "run_summary.csv"
    pd.DataFrame([o.__dict__ for o in outcomes]).to_csv(path, index=False, encoding="utf-8-sig")
    return path


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    # ---- configuration (logging is not up yet, so report errors on stderr)
    try:
        cfg = load_config(args.config, {
            "input_csv": args.input,
            "reports_dir": args.output_dir,
            "crawl_max_pages": args.max_pages,
            "ollama_model": args.model,
            "log_level": args.log_level,
        })
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    log_file = setup_logging(cfg.logs_dir, cfg.log_level)
    log.info("SEO audit starting (log file: %s)", log_file)
    cfg.reports_dir.mkdir(parents=True, exist_ok=True)

    # ---- Agent 1: read the websites
    try:
        targets = FileReaderAgent(cfg).run()
    except FileReaderError as exc:
        log.error("Agent 1 failed: %s", exc)
        return 2
    if args.limit:
        targets = targets[: args.limit]

    # ---- prepare Agent 2 and the selected analysis agent (fail fast on setup problems)
    crawler = CrawlerAgent(cfg)
    if not args.skip_crawl:
        try:
            crawler.preflight()
        except CrawlerError as exc:
            log.error("Agent 2 setup failed: %s", exc)
            return 2

    try:
        engine = select_analysis_engine(cfg, args.engine)
    except ConfigError as exc:
        log.error("%s", exc)
        return 2
    analyzer: AnalysisAgent | RuleEngineAgent
    if engine == "agent4":
        analyzer = RuleEngineAgent(cfg)
    else:
        analyzer = AnalysisAgent(cfg, use_llm=not args.no_llm)
    analyzer.preflight()
    log.info("Analysis engine for this run: %s", engine)

    # ---- run the pipeline for every website
    outcomes: list[SiteOutcome] = []
    try:
        for index, target in enumerate(targets, start=1):
            log.info("=== [%d/%d] %s ===", index, len(targets), target.url)
            try:
                outcomes.append(process_site(target, crawler, analyzer, cfg, args.skip_crawl))
            except CrawlerError as exc:
                log.error("[%s] Crawl failed: %s", target.slug, exc)
                outcomes.append(SiteOutcome(target.url, target.slug, "failed", message=str(exc)))
            except Exception as exc:  # noqa: BLE001 - one bad site must not stop the batch
                log.exception("[%s] Unexpected error", target.slug)
                outcomes.append(SiteOutcome(target.url, target.slug, "failed", message=f"{type(exc).__name__}: {exc}"))
    except KeyboardInterrupt:
        log.warning("Interrupted by user - writing summary for finished sites")

    # ---- summary
    summary_path = write_run_summary(outcomes, cfg.reports_dir) if outcomes else None
    ok = sum(o.status == "ok" for o in outcomes)
    log.info("Finished: %d succeeded, %d failed", ok, len(outcomes) - ok)
    for o in outcomes:
        detail = f"{o.issues} issue(s), {o.analysis}" if o.status == "ok" else o.message
        log.info("  [%s] %s -> %s", o.status.upper(), o.url, detail)
    if summary_path:
        log.info("Run summary: %s", summary_path)

    return 0 if outcomes and ok == len(outcomes) else 1


if __name__ == "__main__":
    sys.exit(main())