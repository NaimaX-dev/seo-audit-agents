"""
agents/agent3_analyzer.py - Agent 3: LLM Analysis Agent.

Responsibility
--------------
Read a website's normalized ``issues.csv`` and return a structured
``AnalysisResult``:

    * issue summary          (LLM)
    * severity breakdown     (counted with pandas - never left to the LLM)
    * SEO recommendations    (LLM)
    * priority actions       (LLM)

How it works
------------
1. pandas aggregates the issues into groups (severity + issue code) with counts,
   affected-page counts and a few example URLs. Only this compact digest goes to
   the model, so even a site with thousands of issues fits in a small context.
2. The digest is sent to a local Ollama model (default ``qwen3:8b``) with a JSON
   schema, so the reply is machine-readable.
3. The reply is validated and cleaned. On failure the call is retried; if it
   still fails and ``llm_fallback`` is enabled, a deterministic rule-based
   analysis is produced so the report is never blocked by the LLM.
"""

import json
import logging
import re
import time
from pathlib import Path

import httpx
import ollama
import pandas as pd

from agents.models import (
    SEVERITY_ORDER,
    SEVERITY_RANK,
    AnalysisResult,
    WebsiteTarget,
    normalize_severity,
)
from config import Config
from utils.helpers import truncate

# JSON schema handed to Ollama's structured-output ("format") feature.
ANALYSIS_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "severity_notes": {"type": "string"},
        "recommendations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "detail": {"type": "string"},
                    "related_issue": {"type": "string"},
                    "severity": {"type": "string"},
                },
                "required": ["title", "detail"],
            },
        },
        "priority_actions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "rank": {"type": "integer"},
                    "action": {"type": "string"},
                    "reason": {"type": "string"},
                    "impact": {"type": "string"},
                    "effort": {"type": "string"},
                },
                "required": ["action", "reason"],
            },
        },
    },
    "required": ["summary", "severity_notes", "recommendations", "priority_actions"],
}

SYSTEM_PROMPT = (
    "You are a senior technical SEO consultant reviewing the results of an automated "
    "website crawl. Rules: use ONLY the data supplied by the user; never invent pages, "
    "metrics, rankings or traffic numbers; severity labels come from the crawler and must "
    "not be changed; keep every recommendation specific to the issue codes provided; write "
    "in clear, plain English. Reply with a single JSON object and nothing else."
)

USER_PROMPT_TEMPLATE = """Analyse these crawl findings for {url}.

DATA (JSON):
{digest}

Return a JSON object with exactly these keys:
- "summary": 3-5 sentences describing the overall SEO health shown by the data.
- "severity_notes": 1-3 sentences interpreting the severity breakdown.
- "recommendations": 3-8 objects {{"title", "detail", "related_issue", "severity"}}.
  "related_issue" must be an issue code from the data; "detail" says what to change and how.
- "priority_actions": 3-6 objects {{"rank", "action", "reason", "impact", "effort"}} ordered
  by rank (1 = do first). "impact" and "effort" are each one of Low / Medium / High.
If there are no issues, say so plainly and return empty lists."""

# Retry-worthy failures when talking to Ollama.
_LLM_ERRORS = (ollama.ResponseError, ConnectionError, TimeoutError, httpx.HTTPError, ValueError, OSError)

_IMPACT_BY_SEVERITY = {"Critical": "High", "High": "High", "Medium": "Medium", "Low": "Low", "Info": "Low"}


class AnalysisAgent:
    name = "Agent3-Analyzer"

    def __init__(self, config: Config, use_llm: bool = True) -> None:
        self.cfg = config
        self.use_llm = use_llm
        self.log = logging.getLogger(self.name)
        self._client: ollama.Client | None = None
        self._llm_ready = False
        self._llm_disabled_reason: str | None = None if use_llm else "LLM disabled with --no-llm"

    # ------------------------------------------------------------------ public
    def preflight(self) -> bool:
        """Check that Ollama is reachable and the model is pulled.

        Returns True when the LLM can be used. Never raises: if the LLM is not
        available the agent falls back to rule-based analysis (when enabled).
        """
        if not self.use_llm:
            self.log.info("LLM analysis disabled; using rule-based analysis only")
            return False

        model = self.cfg.ollama_model
        try:
            self._client = ollama.Client(
                host=self.cfg.ollama_host, timeout=self.cfg.ollama_timeout_seconds
            )
            available = self._model_names(self._client.list())
        except Exception as exc:  # noqa: BLE001 - any client/network failure means "not usable"
            self._llm_disabled_reason = f"Ollama not reachable at {self.cfg.ollama_host}: {exc}"
            self.log.warning("%s", self._llm_disabled_reason)
            self.log.warning("Start it with 'ollama serve' (or open the Ollama app).")
            return False

        if not any(self._same_model(model, name) for name in available):
            self._llm_disabled_reason = f"Model '{model}' is not installed in Ollama"
            self.log.warning("%s. Run: ollama pull %s", self._llm_disabled_reason, model)
            return False

        self._llm_ready = True
        self.log.info("Ollama ready at %s (model %s)", self.cfg.ollama_host, model)
        return True

    def warm_up(self) -> None:
        """Send a trivial request so Ollama loads the model into RAM ahead of time.

        Loading a multi-GB model from disk is the slowest part of the *first*
        request, and it happens again if the model was unloaded (Ollama's default
        keep_alive is 5 minutes - shorter than a slow crawl). Calling this while
        Agent 2 is still crawling lets the model load overlap with the crawl
        instead of eating into the analysis timeout later. Safe to call from a
        background thread; failures are logged and swallowed, since analyze()
        will still try to load the model itself if this didn't finish.
        """
        if not self._llm_ready or self._client is None:
            return
        try:
            started = time.monotonic()
            self._client.generate(
                model=self.cfg.ollama_model,
                prompt="Reply with the single word OK.",
                keep_alive=self.cfg.ollama_keep_alive,
                options={"num_predict": 8, "num_ctx": self.cfg.ollama_num_ctx},
            )
            self.log.info(
                "[warm-up] Model loaded and kept in memory in %.1fs (keep_alive=%s)",
                time.monotonic() - started, self.cfg.ollama_keep_alive,
            )
        except _LLM_ERRORS as exc:
            self.log.warning("[warm-up] Failed (analysis will load the model itself): %s", exc)

    def analyze(self, target: WebsiteTarget, issues_csv: Path) -> AnalysisResult:
        """Analyse one website's issues.csv."""
        issues = self._read_issues(issues_csv)
        total = len(issues)
        pages = int(issues["url"].nunique()) if total else 0
        breakdown = self._severity_breakdown(issues)
        groups = self._group_issues(issues)

        base = {
            "severity_breakdown": breakdown,
            "issue_groups": groups,
            "total_issues": total,
            "pages_affected": pages,
        }

        # Nothing to analyse - no need to bother the model.
        if total == 0:
            return AnalysisResult(
                summary="The crawl did not record any SEO issues for the pages it visited. "
                        "This does not prove the site is flawless: only the crawled sample was checked.",
                severity_notes="No issues were recorded.",
                recommendations=[], priority_actions=[],
                source="rule-based", **base,
            )

        if self._llm_ready:
            try:
                llm = self._ask_llm(target, base)
                self.log.info("[%s] LLM analysis complete", target.slug)
                return AnalysisResult(source=f"ollama:{self.cfg.ollama_model}", **llm, **base)
            except _LLM_ERRORS as exc:
                self._llm_disabled_reason = f"LLM analysis failed: {exc}"
                self.log.error("[%s] %s", target.slug, self._llm_disabled_reason)
                if not self.cfg.llm_fallback:
                    raise
        elif self.use_llm and not self.cfg.llm_fallback:
            raise RuntimeError(self._llm_disabled_reason or "LLM unavailable and llm_fallback is off")

        self.log.warning("[%s] Using rule-based analysis (%s)", target.slug, self._llm_disabled_reason)
        return AnalysisResult(
            source="rule-based", llm_error=self._llm_disabled_reason,
            **self._rule_based(groups, total, pages, breakdown), **base,
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
        counts = issues["severity"].value_counts().to_dict()
        return {sev: int(counts[sev]) for sev in SEVERITY_ORDER if counts.get(sev)}

    def _group_issues(self, issues: pd.DataFrame) -> list[dict]:
        """Aggregate rows by (severity, code): count, pages and examples."""
        groups: list[dict] = []
        for (severity, code), rows in issues.groupby(["severity", "code"], sort=False):
            urls = [u for u in rows["url"].unique().tolist() if u]
            evidence = next((e for e in rows["evidence"] if e), "")
            action = next((a for a in rows["action"] if a), "")
            groups.append({
                "severity": severity,
                "code": code or "(unspecified)",
                "count": int(len(rows)),
                "pages_affected": len(urls),
                "example_urls": urls[: self.cfg.llm_max_examples],
                "sample_evidence": truncate(evidence, 120),
                "crawler_action": truncate(action, 120),
            })
        groups.sort(key=lambda g: (SEVERITY_RANK[g["severity"]], -g["count"], g["code"]))
        return groups

    # -------------------------------------------------------------------- LLM
    def _ask_llm(self, target: WebsiteTarget, base: dict) -> dict:
        digest = {
            "website": target.url,
            "total_issues": base["total_issues"],
            "pages_with_issues": base["pages_affected"],
            "severity_counts": base["severity_breakdown"],
            "issue_groups": base["issue_groups"][: self.cfg.llm_max_issue_groups],
        }
        if len(base["issue_groups"]) > self.cfg.llm_max_issue_groups:
            digest["note"] = (
                f"Only the {self.cfg.llm_max_issue_groups} most severe/frequent of "
                f"{len(base['issue_groups'])} issue groups are shown."
            )
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": USER_PROMPT_TEMPLATE.format(
                url=target.url, digest=json.dumps(digest, indent=1, ensure_ascii=False))},
        ]

        attempts = 1 + self.cfg.llm_max_retries
        last_error: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
                self.log.info("[%s] Asking %s (attempt %d/%d)", target.slug,
                              self.cfg.ollama_model, attempt, attempts)
                content = self._chat(messages)
                self.log.debug("[%s] Raw LLM reply: %s", target.slug, content)
                return self._parse_reply(content)
            except _LLM_ERRORS as exc:
                last_error = exc
                self.log.warning("[%s] Attempt %d failed: %s", target.slug, attempt, exc)
                if attempt < attempts:
                    time.sleep(2 * attempt)
        assert last_error is not None
        raise last_error

    def _chat(self, messages: list[dict]) -> str:
        assert self._client is not None
        options = {
            "temperature": self.cfg.ollama_temperature,
            "num_ctx": self.cfg.ollama_num_ctx,
            "num_predict": self.cfg.ollama_num_predict,
        }
        if self.cfg.ollama_num_thread:
            options["num_thread"] = self.cfg.ollama_num_thread
        kwargs = {
            "model": self.cfg.ollama_model,
            "messages": messages,
            "format": ANALYSIS_SCHEMA,
            "keep_alive": self.cfg.ollama_keep_alive,
            "options": options,
        }
        try:
            # Qwen3 supports a "thinking" mode; it is off by default for speed.
            response = self._client.chat(**kwargs, think=self.cfg.ollama_think)
        except TypeError:  # very old ollama-python without the `think` argument
            response = self._client.chat(**kwargs)

        # Ollama reports its own timing/token counters; logging them turns "it's
        # slow" into "N prompt tokens, M output tokens, X s/token" so a future
        # slowdown can be diagnosed instead of guessed at.
        prompt_tokens, output_tokens = response.get("prompt_eval_count"), response.get("eval_count")
        eval_ns, load_ns = response.get("eval_duration"), response.get("load_duration")
        rate = f"{output_tokens / (eval_ns / 1e9):.1f} tok/s" if eval_ns and output_tokens else "n/a"
        self.log.info(
            "[Ollama] prompt=%s tokens, output=%s tokens, model_load=%.1fs, generation=%.1fs (%s)",
            prompt_tokens, output_tokens, (load_ns or 0) / 1e9, (eval_ns or 0) / 1e9, rate,
        )
        return response["message"]["content"] or ""

    @staticmethod
    def _parse_reply(content: str) -> dict:
        """Extract, validate and clean the JSON object from the model's reply."""
        text = re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL)  # Qwen3 reasoning
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())        # code fences
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            start, end = text.find("{"), text.rfind("}")
            if start == -1 or end <= start:
                raise ValueError("LLM reply did not contain a JSON object") from None
            data = json.loads(text[start : end + 1])  # JSONDecodeError is a ValueError
        if not isinstance(data, dict):
            raise ValueError("LLM reply JSON was not an object")

        summary = str(data.get("summary", "")).strip()
        if not summary:
            raise ValueError("LLM reply had an empty summary")

        recommendations = []
        for item in data.get("recommendations") or []:
            if isinstance(item, str):
                item = {"detail": item}  # bare string: the PDF falls back to a generic heading
            if not isinstance(item, dict):
                continue
            rec = {
                "title": str(item.get("title", "")).strip(),
                "detail": str(item.get("detail", "")).strip(),
                "related_issue": str(item.get("related_issue", "")).strip(),
                "severity": str(item.get("severity", "")).strip(),
            }
            if rec["title"] or rec["detail"]:
                recommendations.append(rec)

        actions = []
        for index, item in enumerate(data.get("priority_actions") or [], start=1):
            if isinstance(item, str):
                item = {"action": item}
            if not isinstance(item, dict):
                continue
            try:
                rank = int(item.get("rank", index))
            except (TypeError, ValueError):
                rank = index
            action = {
                "rank": rank,
                "action": str(item.get("action", "")).strip(),
                "reason": str(item.get("reason", "")).strip(),
                "impact": str(item.get("impact", "")).strip(),
                "effort": str(item.get("effort", "")).strip(),
            }
            if action["action"]:
                actions.append(action)
        actions.sort(key=lambda a: a["rank"])

        return {
            "summary": summary,
            "severity_notes": str(data.get("severity_notes", "")).strip(),
            "recommendations": recommendations[:10],
            "priority_actions": actions[:8],
        }

    # ------------------------------------------------------ Ollama utilities
    @staticmethod
    def _model_names(list_response: object) -> list[str]:
        """Extract model names from ollama.list() across client versions."""
        models = getattr(list_response, "models", None)
        if models is None and isinstance(list_response, dict):
            models = list_response.get("models")
        names: list[str] = []
        for entry in models or []:
            if isinstance(entry, dict):
                name = entry.get("model") or entry.get("name")
            else:
                name = getattr(entry, "model", None) or getattr(entry, "name", None)
            if name:
                names.append(str(name))
        return names

    @staticmethod
    def _same_model(wanted: str, installed: str) -> bool:
        """'qwen3' matches 'qwen3:latest'; 'qwen3:8b' must match exactly."""
        if wanted == installed:
            return True
        return ":" not in wanted and installed.split(":")[0] == wanted

    # ------------------------------------------------------ fallback analysis
    @staticmethod
    def _rule_based(groups: list[dict], total: int, pages: int, breakdown: dict[str, int]) -> dict:
        """Deterministic analysis used when the LLM is unavailable."""
        sev_text = ", ".join(f"{count} {sev}" for sev, count in breakdown.items())
        top = groups[0]
        summary = (
            f"The crawl recorded {total} issue(s) across {pages} page(s) ({sev_text}). "
            f"The most serious group is '{top['code']}' ({top['severity']}, "
            f"{top['count']} occurrence(s) on {top['pages_affected']} page(s)). "
            "This summary was generated from the crawl data without an LLM."
        )
        recommendations = [
            {
                "title": g["code"].replace("_", " ").capitalize(),
                "detail": g["crawler_action"] or "Review the affected pages and fix this issue.",
                "related_issue": g["code"],
                "severity": g["severity"],
            }
            for g in groups[:8]
        ]
        actions = [
            {
                "rank": rank,
                "action": g["crawler_action"] or f"Review and fix '{g['code']}' on the affected pages.",
                "reason": f"{g['count']} occurrence(s) on {g['pages_affected']} page(s); severity {g['severity']}.",
                "impact": _IMPACT_BY_SEVERITY.get(g["severity"], ""),
                "effort": "",
            }
            for rank, g in enumerate(groups[:5], start=1)
        ]
        return {
            "summary": summary,
            "severity_notes": "Severity labels are assigned by the crawler; counts above are exact.",
            "recommendations": recommendations,
            "priority_actions": actions,
        }