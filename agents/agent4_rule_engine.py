"""
agents/agent4_rule_engine.py - Agent 4: Rule-Based SEO Analyzer.

Responsibility
--------------
Read a website's normalized ``issues.csv`` (written by Agent 2) and return a
structured ``AnalysisResult`` - the exact same shape Agent 3 produces - built
entirely from a predefined set of SEO rules. No LLM, no network calls, fully
deterministic: the same issues.csv always produces the same report.

How it works
-------------
1. Every issue row is matched, by its ``code``/``evidence`` text, against
   ``SEO_RULES`` below. The first rule whose keywords appear wins; issues that
   match nothing fall back to a generic rule so nothing is silently dropped.
2. Each matched rule assigns a severity of High / Medium / Low. A rule may
   force a specific severity (e.g. a broken link is always High); otherwise
   the crawler's own severity is kept, collapsed onto the three-level scale.
3. Rows are grouped by (severity, issue code) - counts, affected pages and a
   short example - exactly like Agent 3's digest, so both agents' output
   renders through the same PDF section builders in ``utils/pdf_report.py``.
4. Recommendations and priority actions are generated straight from the
   matched rules' own text, ordered most-severe / most-frequent first.

This agent has no external dependencies (no Ollama, no network), so
``preflight()`` never fails and ``analyze()`` is fast even on very large
issues.csv files.
"""

import logging
from pathlib import Path

import pandas as pd

from agents.models import AnalysisResult, WebsiteTarget, normalize_severity
from config import Config
from utils.helpers import truncate

# --------------------------------------------------------------------------
# Predefined SEO rules
# --------------------------------------------------------------------------
# Checked in order against "<code> <evidence>" (lower-cased). The first rule
# whose `match` keyword appears anywhere in that text wins.
#   id       - stable identifier, used to de-duplicate recommendations
#   match    - keywords/phrases to look for in the issue code or evidence
#   severity - forced severity (High/Medium/Low); omit to keep the crawler's
#              own severity, collapsed onto the three-level scale
#   title    - short recommendation heading
#   detail   - the recommendation text shown in the report
SEO_RULES: list[dict] = [
    {
        "id": "missing_title",
        "match": ("missing_title", "title_missing", "no_title", "empty_title",
                  "title tag is missing", "missing <title>"),
        "severity": "High",
        "title": "Add a unique <title> tag",
        "detail": "Every page needs a unique, descriptive <title> tag (roughly 50-60 characters). "
                  "Search engines use it as the clickable headline in results, and pages without one "
                  "are far less likely to rank or be clicked.",
    },
    {
        "id": "title_length",
        "match": ("title_too_long", "title_too_short", "title_length",
                  "title too long", "title too short"),
        "severity": "Medium",
        "title": "Fix title length",
        "detail": "Keep titles between roughly 50 and 60 characters so they are not truncated in "
                  "search results and still read naturally.",
    },
    {
        "id": "duplicate_title",
        "match": ("duplicate_title", "duplicate title", "title_duplicate"),
        "severity": "Medium",
        "title": "Remove duplicate titles",
        "detail": "Give each page its own title. Duplicate titles make it harder for search engines "
                  "to tell pages apart and can cause the wrong page to rank for a query.",
    },
    {
        "id": "missing_meta_description",
        "match": ("missing_meta_description", "meta_description_missing", "no_meta_description",
                  "meta description is missing"),
        "severity": "Medium",
        "title": "Add a meta description",
        "detail": "Write a unique meta description (roughly 120-158 characters) summarising the page. "
                  "It is often used as the search-result snippet and influences click-through rate.",
    },
    {
        "id": "meta_description_length",
        "match": ("meta_description_too_long", "meta_description_too_short", "description_length",
                  "description too long", "description too short"),
        "severity": "Low",
        "title": "Fix meta description length",
        "detail": "Trim or expand the meta description to roughly 120-158 characters so it is not "
                  "cut off, or padded out, in search results.",
    },
    {
        "id": "duplicate_meta_description",
        "match": ("duplicate_meta_description", "duplicate description", "description_duplicate"),
        "severity": "Medium",
        "title": "Remove duplicate meta descriptions",
        "detail": "Write a distinct meta description for each page instead of reusing the same text "
                  "across multiple URLs.",
    },
    {
        "id": "missing_h1",
        "match": ("missing_h1", "h1_missing", "no_h1", "h1 tag is missing"),
        "severity": "High",
        "title": "Add a single H1 heading",
        "detail": "Every page should have exactly one <h1> summarising its main topic. It helps both "
                  "users and search engines understand what the page is about.",
    },
    {
        "id": "multiple_h1",
        "match": ("multiple_h1", "h1_multiple", "duplicate_h1", "more than one h1"),
        "severity": "Low",
        "title": "Use a single H1 per page",
        "detail": "Reduce the page to one <h1> and use <h2>/<h3> for subsequent sections so the "
                  "heading structure stays a clear outline.",
    },
    {
        "id": "missing_alt_text",
        "match": ("missing_alt", "alt_missing", "image_alt", "alt attribute",
                  "images without alt", "missing_image_alt"),
        "severity": "Low",
        "title": "Add descriptive alt text to images",
        "detail": "Give every meaningful <img> a short, descriptive alt attribute. It helps screen "
                  "readers, image search, and provides context if the image fails to load.",
    },
    {
        "id": "broken_link",
        "match": ("broken_link", "404", "link_error", "dead_link", "http_error", "not_found"),
        "severity": "High",
        "title": "Fix broken links",
        "detail": "Update or remove links that return an error (4xx/5xx). Broken links waste crawl "
                  "budget, break navigation and hurt user trust.",
    },
    {
        "id": "redirect_chain",
        "match": ("redirect_chain", "redirect_loop", "multiple_redirects", "redirect chain"),
        "severity": "Medium",
        "title": "Shorten redirect chains",
        "detail": "Point links directly at the final destination URL instead of chaining several "
                  "redirects; each hop adds latency and dilutes link equity.",
    },
    {
        "id": "canonical_missing",
        "match": ("missing_canonical", "canonical_missing", "no_canonical",
                  "canonical tag is missing"),
        "severity": "Medium",
        "title": "Add a canonical tag",
        "detail": "Add a self-referencing <link rel=\"canonical\"> to tell search engines which URL "
                  "is the authoritative version of the page, especially where parameters or "
                  "duplicates exist.",
    },
    {
        "id": "canonical_mismatch",
        "match": ("canonical_mismatch", "duplicate_canonical", "conflicting_canonical",
                  "canonical points"),
        "severity": "Medium",
        "title": "Fix conflicting canonical tags",
        "detail": "Make sure the canonical URL is consistent and points to a real, indexable page - "
                  "not to a redirect, a 404, or a different page's canonical.",
    },
    {
        "id": "noindex",
        "match": ("noindex", "robots_noindex", "indexability", "blocked_from_indexing"),
        "severity": "High",
        "title": "Review noindex / indexability blocks",
        "detail": "Confirm this page is meant to be excluded from search results. If not, remove the "
                  "noindex directive or robots.txt block that is keeping it out of the index.",
    },
    {
        "id": "missing_robots_txt",
        "match": ("missing_robots_txt", "robots_txt_missing", "no_robots_txt"),
        "severity": "Low",
        "title": "Add a robots.txt file",
        "detail": "Publish a robots.txt at the site root so crawlers get explicit guidance on what to "
                  "crawl, and link the sitemap from it.",
    },
    {
        "id": "missing_sitemap",
        "match": ("missing_sitemap", "sitemap_missing", "no_sitemap"),
        "severity": "Low",
        "title": "Publish an XML sitemap",
        "detail": "Generate and submit an XML sitemap listing indexable URLs so search engines can "
                  "discover and prioritise the site's pages more efficiently.",
    },
    {
        "id": "slow_page",
        "match": ("slow_page", "page_speed", "large_page_size", "slow_response",
                  "response_time", "large_html"),
        "severity": "Medium",
        "title": "Improve page load speed",
        "detail": "Compress images, minify assets and reduce page weight/response time. Slow pages "
                  "hurt both user experience and Core Web Vitals-related ranking signals.",
    },
    {
        "id": "thin_content",
        "match": ("thin_content", "low_word_count", "short_content", "insufficient_content"),
        "severity": "Medium",
        "title": "Expand thin content",
        "detail": "Add substantive, useful content to this page. Very short pages tend to rank "
                  "poorly and can be treated as low-value by search engines.",
    },
    {
        "id": "duplicate_content",
        "match": ("duplicate_content", "content_duplicate", "near_duplicate"),
        "severity": "Medium",
        "title": "Resolve duplicate content",
        "detail": "Differentiate or consolidate near-identical pages (merge, canonicalize, or "
                  "rewrite) so search engines are not forced to choose which duplicate to rank.",
    },
    {
        "id": "insecure_http",
        "match": ("missing_https", "mixed_content", "insecure", "not_https", "http_only"),
        "severity": "High",
        "title": "Serve every page over HTTPS",
        "detail": "Migrate remaining HTTP resources/pages to HTTPS and fix mixed-content warnings. "
                  "Browsers flag insecure pages, and HTTPS is a confirmed ranking signal.",
    },
    {
        "id": "missing_structured_data",
        "match": ("missing_schema", "structured_data", "schema_missing", "no_structured_data"),
        "severity": "Low",
        "title": "Add structured data (schema.org)",
        "detail": "Add relevant schema.org markup (e.g. Article, Product, Organization) so search "
                  "engines can show rich results and better understand the page's content.",
    },
    {
        "id": "broken_image",
        "match": ("broken_image", "image_404", "image_error"),
        "severity": "Medium",
        "title": "Fix broken images",
        "detail": "Replace or remove image references that fail to load; broken images hurt user "
                  "experience and waste crawl budget.",
    },
    {
        "id": "mobile_viewport",
        "match": ("missing_viewport", "not_mobile_friendly", "viewport_missing", "mobile_usability"),
        "severity": "High",
        "title": "Add a responsive viewport meta tag",
        "detail": "Add <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\"> and "
                  "verify the layout adapts on mobile - mobile-friendliness affects both usability "
                  "and ranking.",
    },
]

# Used when no rule above matches an issue's code/evidence text, so unknown
# issue types are still reported instead of being silently dropped.
_DEFAULT_RULE = {
    "id": "general",
    "title": "Review and resolve this issue",
    "detail": "Investigate the affected page(s) and address the reported issue according to "
              "standard on-page SEO best practice.",
}

# Crawler severities collapse onto Agent 4's three levels when a rule does not
# force a specific severity of its own.
_SEVERITY_FLOOR = {
    "Critical": "High", "High": "High", "Medium": "Medium",
    "Low": "Low", "Info": "Low", "Unrated": "Low",
}
AGENT4_SEVERITY_ORDER = ["High", "Medium", "Low"]
AGENT4_SEVERITY_RANK = {name: rank for rank, name in enumerate(AGENT4_SEVERITY_ORDER)}


def _match_rule(code: str, evidence: str) -> dict:
    """Return the first rule whose keywords appear in the code/evidence text."""
    haystack = f"{code} {evidence}".lower()
    for rule in SEO_RULES:
        if any(keyword in haystack for keyword in rule["match"]):
            return rule
    return _DEFAULT_RULE


class RuleEngineAgent:
    """Agent 4: deterministic, rule-based SEO analysis (no LLM, no network)."""

    name = "Agent4-RuleEngine"

    def __init__(self, config: Config) -> None:
        self.cfg = config
        self.log = logging.getLogger(self.name)

    # ------------------------------------------------------------------ public
    def preflight(self) -> bool:
        """No external service is required, so this always succeeds."""
        self.log.info("Rule-based analysis ready (%d predefined rule(s))", len(SEO_RULES))
        return True

    def warm_up(self) -> None:
        """No-op. Kept so main.py can start Agent 3 and Agent 4 the same way."""
        return

    def analyze(self, target: WebsiteTarget, issues_csv: Path) -> AnalysisResult:
        """Analyse one website's issues.csv using the predefined SEO rules."""
        issues = self._read_issues(issues_csv)
        total = len(issues)
        pages = int(issues["url"].nunique()) if total else 0

        if total == 0:
            return AnalysisResult(
                summary="The crawl did not record any SEO issues for the pages it visited. "
                        "This does not prove the site is flawless: only the crawled sample was checked.",
                severity_breakdown={},
                severity_notes="No issues were recorded.",
                recommendations=[], priority_actions=[], issue_groups=[],
                total_issues=0, pages_affected=0, source="rule-based:agent4",
            )

        issues = issues.copy()
        matched = [_match_rule(code, evidence) for code, evidence in zip(issues["code"], issues["evidence"])]
        issues["rule_id"] = [rule["id"] for rule in matched]
        issues["agent4_severity"] = [
            rule.get("severity") or _SEVERITY_FLOOR.get(normalize_severity(sev), "Low")
            for rule, sev in zip(matched, issues["severity"])
        ]

        breakdown = self._severity_breakdown(issues)
        groups = self._group_issues(issues)

        return AnalysisResult(
            summary=self._summary(target, groups, total, pages, breakdown),
            severity_breakdown=breakdown,
            severity_notes="Severity levels (High / Medium / Low) are assigned by Agent 4's "
                           "predefined SEO rules, informed by the crawler's own findings.",
            recommendations=self._recommendations(groups),
            priority_actions=self._priority_actions(groups),
            issue_groups=groups,
            total_issues=total,
            pages_affected=pages,
            source="rule-based:agent4",
        )

    # ----------------------------------------------------------- data prepping
    @staticmethod
    def _read_issues(path: Path) -> pd.DataFrame:
        df = pd.read_csv(path, dtype=str, encoding="utf-8-sig", keep_default_na=False)
        for column in ("url", "code", "severity", "evidence", "action"):
            if column not in df.columns:
                df[column] = ""
        df["severity"] = df["severity"].map(normalize_severity)
        return df

    @staticmethod
    def _severity_breakdown(issues: pd.DataFrame) -> dict[str, int]:
        counts = issues["agent4_severity"].value_counts().to_dict()
        return {sev: int(counts[sev]) for sev in AGENT4_SEVERITY_ORDER if counts.get(sev)}

    def _group_issues(self, issues: pd.DataFrame) -> list[dict]:
        """Aggregate rows by (severity, code, rule): count, pages and examples."""
        groups: list[dict] = []
        for (severity, code, rule_id), rows in issues.groupby(
            ["agent4_severity", "code", "rule_id"], sort=False
        ):
            urls = [u for u in rows["url"].unique().tolist() if u]
            evidence = next((e for e in rows["evidence"] if e), "")
            action = next((a for a in rows["action"] if a), "")
            rule = next((r for r in SEO_RULES if r["id"] == rule_id), _DEFAULT_RULE)
            groups.append({
                "severity": severity,
                "code": code or "(unspecified)",
                "rule_id": rule_id,
                "rule_title": rule["title"],
                "count": int(len(rows)),
                "pages_affected": len(urls),
                "example_urls": urls[:2],
                "sample_evidence": truncate(evidence, 120),
                "crawler_action": truncate(action, 120),
                "recommendation": rule["detail"],
            })
        groups.sort(key=lambda g: (AGENT4_SEVERITY_RANK[g["severity"]], -g["count"], g["code"]))
        return groups

    # ---------------------------------------------------------- report pieces
    @staticmethod
    def _summary(
        target: WebsiteTarget, groups: list[dict], total: int, pages: int, breakdown: dict[str, int]
    ) -> str:
        sev_text = ", ".join(f"{count} {sev}" for sev, count in breakdown.items())
        top = groups[0]
        return (
            f"Agent 4's rule-based analysis of {target.url} found {total} issue(s) across "
            f"{pages} page(s) ({sev_text}). The most urgent finding is '{top['rule_title']}' "
            f"({top['severity']}, {top['count']} occurrence(s) on {top['pages_affected']} page(s)). "
            "This analysis is produced entirely from predefined SEO rules, without an LLM."
        )

    @staticmethod
    def _recommendations(groups: list[dict]) -> list[dict]:
        """One recommendation per distinct rule, most-severe first, capped at 10."""
        seen: set[str] = set()
        recommendations = []
        for group in groups:
            if group["rule_id"] in seen:
                continue
            seen.add(group["rule_id"])
            recommendations.append({
                "title": group["rule_title"],
                "detail": group["recommendation"],
                "related_issue": group["code"],
                "severity": group["severity"],
            })
            if len(recommendations) >= 10:
                break
        return recommendations

    @staticmethod
    def _priority_actions(groups: list[dict]) -> list[dict]:
        """Top issue groups turned into ranked, actionable steps, capped at 6."""
        actions = []
        for rank, group in enumerate(groups[:6], start=1):
            actions.append({
                "rank": rank,
                "action": f"{group['rule_title']} ({group['code']})",
                "reason": f"{group['count']} occurrence(s) on {group['pages_affected']} page(s); "
                          f"severity {group['severity']}.",
                "impact": group["severity"],
                "effort": "Low" if group["severity"] == "Low" else "Medium" if group["severity"] == "Medium" else "High",
            })
        return actions