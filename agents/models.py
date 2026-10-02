"""
agents/models.py - data structures passed between the agents.

Keeping them in one module means the agents never import each other; they only
share these plain dataclasses.
"""

from dataclasses import dataclass, field
from pathlib import Path

# --------------------------------------------------------------------------
# Severity handling
# --------------------------------------------------------------------------
# BeyondSEO emits high / medium / low / info. Critical and Unrated are kept so
# the pipeline also copes with other crawlers or unexpected values.
SEVERITY_ORDER = ["Critical", "High", "Medium", "Low", "Info", "Unrated"]
SEVERITY_RANK = {name: rank for rank, name in enumerate(SEVERITY_ORDER)}

_SEVERITY_ALIASES = {
    "critical": "Critical", "blocker": "Critical", "fatal": "Critical", "severe": "Critical",
    "high": "High", "major": "High", "error": "High",
    "medium": "Medium", "moderate": "Medium", "warning": "Medium", "warn": "Medium",
    "low": "Low", "minor": "Low",
    "info": "Info", "informational": "Info", "notice": "Info", "note": "Info",
    "opportunity": "Info",
}


def normalize_severity(value: object) -> str:
    """Map any severity label to one of SEVERITY_ORDER (unknown -> 'Unrated')."""
    return _SEVERITY_ALIASES.get(str(value or "").strip().lower(), "Unrated")


# --------------------------------------------------------------------------
# Pipeline data
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class WebsiteTarget:
    """One website to audit, produced by Agent 1 and consumed by Agent 2."""

    url: str    # normalized absolute URL
    slug: str   # unique, filesystem-safe folder name


@dataclass
class CrawlResult:
    """What Agent 2 hands to Agent 3 and the report builder."""

    target: WebsiteTarget
    site_dir: Path              # reports/<slug>/
    crawl_dir: Path             # reports/<slug>/crawl/  (raw BeyondSEO output)
    issues_csv: Path            # reports/<slug>/issues.csv (normalized)
    issue_count: int
    duration_seconds: float
    summary: dict = field(default_factory=dict)   # BeyondSEO summary.json
    reused_existing: bool = False                 # True when --skip-crawl was used


@dataclass
class AnalysisResult:
    """Structured analysis produced by Agent 3."""

    summary: str
    severity_breakdown: dict[str, int]
    severity_notes: str
    recommendations: list[dict]     # {title, detail, related_issue, severity}
    priority_actions: list[dict]    # {rank, action, reason, impact, effort}
    issue_groups: list[dict]        # aggregated issues (severity, code, count, ...)
    total_issues: int
    pages_affected: int
    source: str                     # "ollama:<model>" or "rule-based"
    llm_error: str | None = None    # why the LLM was not used, if it wasn't
