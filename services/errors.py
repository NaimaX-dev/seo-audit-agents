"""
services/errors.py - turn pipeline exceptions into messages a non-developer can act on.

The agents raise precise but technical errors (``CrawlerError: BeyondSEO exited
with code 1 ...``). ``explain()`` maps them to a title, a plain-English message
and a concrete next step, and the full technical text is still kept in the logs.
"""

from dataclasses import asdict, dataclass

import httpx
import ollama

from agents.agent1_reader import FileReaderError
from agents.agent2_crawler import CrawlerError
from config import ConfigError


@dataclass(frozen=True)
class FriendlyError:
    title: str
    message: str
    hint: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def explain(exc: BaseException) -> FriendlyError:
    """Best-effort translation of any exception raised while running an audit."""
    text = str(exc).strip() or type(exc).__name__
    low = text.lower()

    if isinstance(exc, FileReaderError):
        return FriendlyError(
            "Invalid website URL",
            "The URL could not be used for an audit.",
            "Enter a full address such as https://example.com and try again.",
        )

    if isinstance(exc, ConfigError):
        return FriendlyError(
            "Configuration problem",
            f"The audit settings are invalid: {text}",
            "Fix config.json (see README -> Configuration) and start the audit again.",
        )

    if isinstance(exc, CrawlerError):
        if "command not found" in low or "could not start beyondseo" in low or "could not run" in low:
            return FriendlyError(
                "BeyondSEO is not available",
                "The crawler (BeyondSEO) could not be started on this computer.",
                "Install BeyondSEO and set 'beyondseo_cmd' in config.json (README -> Install BeyondSEO).",
            )
        if "timed out" in low:
            return FriendlyError(
                "The crawl took too long",
                "The website crawl did not finish within the allowed time and was stopped.",
                "Try a lower page limit, or raise 'crawl_timeout_seconds' in config.json.",
            )
        if "code 2" in low or "setup" in low:
            return FriendlyError(
                "Crawler setup problem",
                "BeyondSEO started but reported a setup or configuration failure.",
                "Run BeyondSEO's own 'doctor' command, or use \"crawl_mode\": \"http\" in config.json.",
            )
        if "code 1" in low or "no html pages" in low:
            return FriendlyError(
                "The website could not be crawled",
                "No pages could be read. The site may be unreachable, blocking automated tools, "
                "or disallowing crawlers in robots.txt.",
                "Check the address opens in a browser, then retry. Details are in the site's crawl.log.",
            )
        if "no issues.csv" in low or "could not read crawl issues" in low:
            return FriendlyError(
                "Crawl output is missing",
                "The crawl finished but its results file could not be found or read.",
                "Run the audit again. If it keeps happening, check reports/<site>/crawl.log.",
            )
        return FriendlyError(
            "The crawl failed",
            f"The crawler reported a problem: {text[:300]}",
            "Retry the audit. Details are in reports/<site>/crawl.log.",
        )

    # ---- Ollama / LLM problems (Agent 3)
    if isinstance(exc, (httpx.TimeoutException, TimeoutError)) or "timed out" in low or "timeout" in low:
        return FriendlyError(
            "AI analysis timed out",
            "Ollama took too long to answer. The model may still be loading or the computer is busy.",
            "Retry, use a smaller model (e.g. qwen3:4b), or run the audit with the rule-based engine (Agent 4).",
        )
    if isinstance(exc, (ollama.ResponseError, httpx.HTTPError, ConnectionError)) or "ollama" in low:
        return FriendlyError(
            "Ollama is not reachable",
            "The AI model server (Ollama) could not be contacted or the model is not installed.",
            "Start Ollama, run 'ollama pull qwen3:8b', or choose the rule-based engine (Agent 4).",
        )
    if isinstance(exc, RuntimeError) and ("llm" in low or "model" in low):
        return FriendlyError(
            "AI analysis is unavailable",
            text[:300],
            "Enable 'llm_fallback' in config.json or choose the rule-based engine (Agent 4).",
        )

    if isinstance(exc, PermissionError):
        return FriendlyError(
            "A file could not be written",
            "The application is not allowed to write to the reports folder.",
            "Close any program that has the report open (e.g. a PDF viewer) and retry.",
        )
    if isinstance(exc, OSError):
        return FriendlyError(
            "A file problem occurred",
            f"A report file could not be read or written: {text[:250]}",
            "Check free disk space and that the reports folder is writable.",
        )

    return FriendlyError(
        "Something went wrong",
        f"An unexpected {type(exc).__name__} stopped the audit.",
        "Retry the audit. The technical details were written to logs/seo_audit.log.",
    )
