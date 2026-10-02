"""
utils/pdf_report.py - builds reports/<site>/report.pdf with ReportLab.

Report layout
-------------
1. Title block (website, date, crawler version, analysis source)
2. At-a-glance table (pages crawled, issue counts, coverage)
3. Executive summary
4. Severity breakdown (bar chart + table + notes)
5. Priority actions
6. SEO recommendations
7. Issue groups (what was found, how often, what to do)
8. Appendix: first rows of issues.csv
9. Scope & limitations

Text safety
-----------
ReportLab's built-in fonts cover Latin-1/Windows-1252 only. Anything else (for
example Urdu or emoji in a page title) would render as black boxes, so text is
converted to cp1252 with unsupported characters replaced by '?'.
"""

import logging
import math
import re
from datetime import datetime
from pathlib import Path
from xml.sax.saxutils import escape

import pandas as pd
from reportlab.graphics.charts.barcharts import VerticalBarChart
from reportlab.graphics.shapes import Drawing
from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4, LETTER
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    KeepTogether,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from agents.models import SEVERITY_ORDER, AnalysisResult, CrawlResult, WebsiteTarget
from config import Config
from utils.helpers import truncate

log = logging.getLogger("PDFReport")

NAVY = colors.HexColor("#1F2A44")
GREY = colors.HexColor("#5F6B7A")
LIGHT = colors.HexColor("#F3F5F9")
RULE = colors.HexColor("#D5DAE3")
SEVERITY_COLORS = {
    "Critical": colors.HexColor("#7B1E1E"),
    "High": colors.HexColor("#C0392B"),
    "Medium": colors.HexColor("#E08E0B"),
    "Low": colors.HexColor("#2E86C1"),
    "Info": colors.HexColor("#7F8C8D"),
    "Unrated": colors.HexColor("#B0B7C3"),
}

_CHAR_REPLACEMENTS = {
    "\u2192": "->", "\u2190": "<-", "\u2713": "OK", "\u2714": "OK", "\u2717": "x",
    "\u2022": "-", "\u00a0": " ", "\u200b": "",
}
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


# --------------------------------------------------------------------------
# Text helpers
# --------------------------------------------------------------------------
def _safe(text: object) -> str:
    """Make text renderable with ReportLab's built-in fonts."""
    value = _CONTROL_CHARS.sub("", str(text if text is not None else ""))
    for src, dst in _CHAR_REPLACEMENTS.items():
        value = value.replace(src, dst)
    return value.encode("cp1252", errors="replace").decode("cp1252")


def _p(text: object) -> str:
    """Escape text for use inside a Paragraph (markup-safe, newlines -> <br/>)."""
    return escape(_safe(text)).replace("\n", "<br/>")


def _sev_tag(severity: str) -> str:
    color = SEVERITY_COLORS.get(severity, GREY).hexval().replace("0x", "#")
    return f'<font color="{color}"><b>{_p(severity)}</b></font>'


# --------------------------------------------------------------------------
# Styles & building blocks
# --------------------------------------------------------------------------
def _styles() -> dict[str, ParagraphStyle]:
    base = getSampleStyleSheet()
    return {
        "title": ParagraphStyle("RTitle", parent=base["Title"], fontSize=24, leading=28,
                                alignment=TA_LEFT, textColor=NAVY, spaceAfter=4),
        "subtitle": ParagraphStyle("RSub", parent=base["Normal"], fontSize=12, leading=15,
                                   textColor=NAVY, spaceAfter=2),
        "meta": ParagraphStyle("RMeta", parent=base["Normal"], fontSize=9, leading=12, textColor=GREY),
        "h1": ParagraphStyle("RH1", parent=base["Heading1"], fontSize=15, leading=19, textColor=NAVY,
                             spaceBefore=16, spaceAfter=6, keepWithNext=1),
        "body": ParagraphStyle("RBody", parent=base["Normal"], fontSize=10, leading=14, spaceAfter=6),
        "rec_title": ParagraphStyle("RRec", parent=base["Normal"], fontSize=10.5, leading=14,
                                    textColor=NAVY, spaceBefore=4, spaceAfter=1, keepWithNext=1),
        "small": ParagraphStyle("RSmall", parent=base["Normal"], fontSize=8.5, leading=11, textColor=GREY),
        "cell": ParagraphStyle("RCell", parent=base["Normal"], fontSize=8.5, leading=11),
        "cell_head": ParagraphStyle("RCellHead", parent=base["Normal"], fontSize=8.5, leading=11,
                                    textColor=colors.white, fontName="Helvetica-Bold"),
    }


def _table(rows: list[list[str]], widths: list[float], st: dict[str, ParagraphStyle]) -> Table:
    """Build a striped table. ``rows[0]`` is the header; cells are Paragraph markup."""
    data = [[Paragraph(cell, st["cell_head"]) for cell in rows[0]]]
    data += [[Paragraph(cell, st["cell"]) for cell in row] for row in rows[1:]]
    table = Table(data, colWidths=widths, repeatRows=1)
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), NAVY),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, LIGHT]),
        ("LINEBELOW", (0, 0), (-1, -1), 0.25, RULE),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 5),
        ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    return table


def _severity_chart(breakdown: dict[str, int]) -> Drawing | None:
    items = [(sev, breakdown[sev]) for sev in SEVERITY_ORDER if breakdown.get(sev)]
    if not items:
        return None

    max_value = max(count for _, count in items)
    step = max(1, math.ceil(max_value / 5))
    top = (math.ceil(max_value / step) + 1) * step

    drawing = Drawing(440, 175)
    chart = VerticalBarChart()
    chart.x, chart.y, chart.width, chart.height = 40, 28, 380, 125
    chart.data = [[count for _, count in items]]
    chart.categoryAxis.categoryNames = [sev for sev, _ in items]
    chart.categoryAxis.labels.fontName = "Helvetica"
    chart.categoryAxis.labels.fontSize = 8
    chart.valueAxis.valueMin, chart.valueAxis.valueMax, chart.valueAxis.valueStep = 0, top, step
    chart.valueAxis.labels.fontName = "Helvetica"
    chart.valueAxis.labels.fontSize = 8
    chart.barLabelFormat = "%d"
    chart.barLabels.fontName = "Helvetica"
    chart.barLabels.fontSize = 8
    chart.barLabels.nudge = 7
    chart.bars.strokeColor = colors.white
    chart.barWidth = 28
    for index, (sev, _) in enumerate(items):
        chart.bars[(0, index)].fillColor = SEVERITY_COLORS[sev]
    drawing.add(chart)
    return drawing


# --------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------
def build_pdf(
    output_path: Path,
    target: WebsiteTarget,
    crawl: CrawlResult,
    analysis: AnalysisResult,
    issues: pd.DataFrame,
    cfg: Config,
    lead: dict | None = None,
) -> Path:
    """Render the report and return ``output_path``.

    ``lead`` (optional) is the business this audit belongs to when it was started
    from Lead Discovery (Agent 5): {business_name, address, phone, website}. When
    given, a "Business" block is printed under the title. Without it the report
    is exactly the same as before.
    """
    st = _styles()
    page_size = A4 if cfg.pdf_page_size.upper() == "A4" else LETTER
    doc = SimpleDocTemplate(
        str(output_path), pagesize=page_size,
        leftMargin=18 * mm, rightMargin=18 * mm, topMargin=18 * mm, bottomMargin=20 * mm,
        title=_safe(f"SEO Audit - {target.url}"), author="seo-audit-agents",
    )
    width = doc.width
    summary = crawl.summary or {}
    story: list = []

    # ------------------------------------------------------------ 1. title
    generated = datetime.now().strftime("%Y-%m-%d %H:%M")
    story += [
        Paragraph("SEO Audit Report", st["title"]),
        Paragraph(_p(target.url), st["subtitle"]),
        Paragraph(
            _p(f"Generated {generated}  |  Crawler: BeyondSEO {summary.get('engine_version', 'n/a')}"
               f"  |  Analysis: {analysis.source}"),
            st["meta"],
        ),
        Spacer(1, 10),
    ]

    # ------------------------------------------- 1b. business (Agent 5 leads)
    if lead:
        business = [
            ["Business", "Details"],
            ["Business name", _p(lead.get("business_name") or "-")],
            ["Address", _p(lead.get("address") or "-")],
            ["Phone", _p(lead.get("phone") or "-")],
            ["Website", _p(lead.get("website") or target.url)],
        ]
        story += [_table(business, [width * 0.30, width * 0.70], st), Spacer(1, 10)]

    # ------------------------------------------------------- 2. at a glance
    glance = [
        ["Metric", "Value"],
        ["Pages crawled (HTML)", _p(summary.get("html_documents", "n/a"))],
        ["URLs attempted", _p(summary.get("attempted_urls", "n/a"))],
        ["Failed / HTTP-error URLs", _p(summary.get("failed_or_http_error_urls", "n/a"))],
        ["Total issues", _p(analysis.total_issues)],
        ["Pages with issues", _p(analysis.pages_affected)],
        ["Crawl limited by page budget", _p("yes" if summary.get("coverage_limited") else "no"
                                            if "coverage_limited" in summary else "n/a")],
    ]
    story.append(_table(glance, [width * 0.55, width * 0.45], st))

    # -------------------------------------------------- 3. executive summary
    story += [Paragraph("Executive summary", st["h1"]), Paragraph(_p(analysis.summary), st["body"])]
    if analysis.llm_error:
        story.append(Paragraph(
            _p(f"Note: the local LLM was not used ({analysis.llm_error}). "
               "The analysis below was produced by deterministic rules."), st["small"]))

    # ---------------------------------------------------- 4. severity section
    story.append(Paragraph("Severity breakdown", st["h1"]))
    chart = _severity_chart(analysis.severity_breakdown)
    if chart is not None:
        story.append(chart)
        rows = [["Severity", "Issues", "Share"]]
        for sev, count in analysis.severity_breakdown.items():
            share = f"{count / analysis.total_issues:.0%}" if analysis.total_issues else "-"
            rows.append([_sev_tag(sev), str(count), share])
        story += [_table(rows, [width * 0.4, width * 0.3, width * 0.3], st), Spacer(1, 6)]
    else:
        story.append(Paragraph("No issues were recorded.", st["body"]))
    if analysis.severity_notes:
        story.append(Paragraph(_p(analysis.severity_notes), st["body"]))

    # --------------------------------------------------- 5. priority actions
    if analysis.priority_actions:
        story.append(Paragraph("Priority actions", st["h1"]))
        rows = [["#", "Action", "Why", "Impact", "Effort"]]
        for item in analysis.priority_actions:
            rows.append([
                _p(item["rank"]), _p(item["action"]), _p(item["reason"] or "-"),
                _p(item["impact"] or "-"), _p(item["effort"] or "-"),
            ])
        story.append(_table(rows, [width * 0.06, width * 0.42, width * 0.32, width * 0.10, width * 0.10], st))

    # ---------------------------------------------------- 6. recommendations
    if analysis.recommendations:
        story.append(Paragraph("SEO recommendations", st["h1"]))
        for index, rec in enumerate(analysis.recommendations, start=1):
            heading = f"<b>{index}. {_p(rec['title'] or rec['related_issue'] or 'Recommendation')}</b>"
            if rec["severity"]:
                heading += f"  [{_sev_tag(rec['severity'].capitalize())}]"
            block = [Paragraph(heading, st["rec_title"]), Paragraph(_p(rec["detail"]), st["body"])]
            if rec["related_issue"]:
                block.append(Paragraph(_p(f"Related issue: {rec['related_issue']}"), st["small"]))
            story.append(KeepTogether(block))

    # ---------------------------------------------------------- 7. issue groups
    if analysis.issue_groups:
        story.append(Paragraph("Issues found", st["h1"]))
        rows = [["Severity", "Issue", "Count", "Pages", "Evidence / suggested action"]]
        for group in analysis.issue_groups:
            detail = _p(group["sample_evidence"] or "-")
            if group["crawler_action"]:
                detail += f"<br/><i>{_p(group['crawler_action'])}</i>"
            rows.append([
                _sev_tag(group["severity"]), _p(group["code"]),
                str(group["count"]), str(group["pages_affected"]), detail,
            ])
        story.append(_table(rows, [width * 0.10, width * 0.28, width * 0.07, width * 0.07, width * 0.48], st))

    # ------------------------------------------------------------ 8. appendix
    if len(issues):
        limit = cfg.pdf_max_appendix_rows
        story.append(Paragraph("Appendix: issue list", st["h1"]))
        story.append(Paragraph(
            _p(f"Showing {min(limit, len(issues))} of {len(issues)} rows. "
               "The complete list is in issues.csv in the same folder."), st["small"]))
        story.append(Spacer(1, 4))
        rows = [["Severity", "Issue", "URL", "Evidence"]]
        for _, row in issues.head(limit).iterrows():
            rows.append([
                _sev_tag(row["severity"]), _p(row["code"]),
                _p(truncate(row["url"], 90)), _p(truncate(row["evidence"], 140)),
            ])
        story.append(_table(rows, [width * 0.09, width * 0.27, width * 0.32, width * 0.32], st))

    # ------------------------------------------------------- 9. limitations
    story.append(Paragraph("Scope and limitations", st["h1"]))
    notes = []
    if summary.get("coverage_note"):
        notes.append(str(summary["coverage_note"]))
    unmeasured = summary.get("unmeasured")
    if isinstance(unmeasured, list) and unmeasured:
        notes.append("Not measured by this audit: " + ", ".join(map(str, unmeasured)) + ".")
    notes.append("Severity labels come from the crawler. Recommendations are guidance to review, "
                 "not guaranteed ranking improvements.")
    for note in notes:
        story.append(Paragraph(_p(note), st["small"]))
        story.append(Spacer(1, 3))

    def _footer(canvas, document) -> None:
        canvas.saveState()
        canvas.setFont("Helvetica", 8)
        canvas.setFillColor(GREY)
        canvas.drawString(document.leftMargin, 10 * mm, _safe(f"SEO audit - {target.url}"))
        canvas.drawRightString(page_size[0] - document.rightMargin, 10 * mm, f"Page {document.page}")
        canvas.restoreState()

    doc.build(story, onFirstPage=_footer, onLaterPages=_footer)
    log.info("[%s] PDF report written to %s", target.slug, output_path)
    return output_path
