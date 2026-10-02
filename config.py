"""
config.py - central configuration for seo-audit-agents.

Every tunable value lives in the ``Config`` dataclass below. Values are resolved
in this order (later sources win):

    1. Defaults defined in this file
    2. Optional ``config.json`` next to this file (or a file passed via --config)
    3. Environment variables named ``SEO_<FIELD_NAME_IN_UPPER_CASE>``
       e.g. SEO_OLLAMA_MODEL=qwen3:14b, SEO_CRAWL_MAX_PAGES=200
    4. Command-line overrides passed in by main.py

Example config.json:

    {
      "ollama_model": "qwen3:8b",
      "crawl_max_pages": 50,
      "beyondseo_cmd": ["python3", "/home/me/beyondseo/scripts/run.py"]
    }

Relative paths are resolved against the project root (the folder of this file).
"""

import json
import os
import shlex
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

import sys

if getattr(sys, "frozen", False):
    PROJECT_ROOT = Path(sys.executable).resolve().parent
else:
    PROJECT_ROOT = Path(__file__).resolve().parent
    
DEFAULT_CONFIG_FILE = PROJECT_ROOT / "config.json"
ENV_PREFIX = "SEO_"

VALID_CRAWL_MODES = {"auto", "http", "browser"}
VALID_LOG_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
VALID_PAGE_SIZES = {"A4", "LETTER"}


class ConfigError(ValueError):
    """Raised when the configuration is missing, malformed or invalid."""


@dataclass(frozen=True)
class Config:
    # ------------------------------------------------------------------ paths
    input_csv: Path = PROJECT_ROOT / "data" / "websites.csv"
    leads_csv: Path = PROJECT_ROOT / "data" / "leads.csv"   # Agent 5 lead store
    reports_dir: Path = PROJECT_ROOT / "reports"
    logs_dir: Path = PROJECT_ROOT / "logs"
    log_level: str = "INFO"

    # -------------------------------------------------------- analysis agents
    # Which analysis agent(s) may run between Agent 2 (crawler) and the PDF
    # report. At least one must be True.
    #   enable_agent3 -> Agent 3, Ollama/Qwen3 LLM analysis (with a rule-based
    #                    fallback of its own if the LLM is unavailable)
    #   enable_agent4 -> Agent 4, deterministic rule-based SEO analysis
    #                    (no LLM, no network)
    # Pipelines:
    #   Agent1 -> Agent2 -> Agent3 -> PDF   (enable_agent3=True,  enable_agent4=False)
    #   Agent1 -> Agent2 -> Agent4 -> PDF   (enable_agent3=False, enable_agent4=True)
    # If both are True, Agent 4 runs (it is faster and needs no LLM); Agent 3
    # is simply skipped for that run. Use --engine on the command line, or set
    # only one of these to True, to make the choice explicit.
    enable_agent3: bool = True
    enable_agent4: bool = False

    # -------------------------------------------------- BeyondSEO (Agent 2)
    # How to launch the BeyondSEO CLI. Either the installed console script:
    #     ["beyondseo"]
    # or the launcher inside a cloned checkout (no venv activation needed):
    #     ["python3", "/path/to/beyondseo/scripts/run.py"]
    beyondseo_cmd: list[str] = field(default_factory=lambda: ["beyondseo"])
    crawl_max_pages: int = 100          # BeyondSEO --max-pages
    # "http": fetch only, fast. "auto": also renders pages that LOOK like JS shells
    # with a real headless browser - much slower and the usual cause of very long
    # crawls on real sites. "browser": renders every page (slowest). Start with
    # "http"; move to "auto"/"browser" only for sites you know are JS-rendered.
    crawl_mode: str = "http"
    crawl_workers: int = 8              # BeyondSEO --workers (concurrent HTTP fetches)
    crawl_request_timeout: float = 12   # BeyondSEO --timeout, seconds, PER REQUEST
    crawl_retries: int = 1              # BeyondSEO --retries; each retry re-pays the full timeout
    crawl_delay: float = 0.2            # BeyondSEO --delay, seconds of politeness spacing per host
    crawl_use_sitemaps: bool = False    # BeyondSEO sitemap discovery; off by default = fewer requests, faster start
    crawl_include_www: bool = True      # BeyondSEO --include-www
    crawl_timeout_seconds: int = 600    # hard wall-clock limit for the whole crawl subprocess
    crawl_extra_args: list[str] = field(default_factory=list)  # passed verbatim, e.g. ["--allow-private"]
    # True  -> delete the previous crawl folder and crawl from scratch
    # False -> pass --resume so BeyondSEO continues the previous snapshot
    crawl_fresh: bool = True

    # ------------------------------------------------------ Ollama (Agent 3)
    ollama_host: str = "http://localhost:11434"
    ollama_model: str = "qwen3:8b"
    ollama_timeout_seconds: int = 600
    ollama_temperature: float = 0.2
    ollama_num_ctx: int = 8192
    ollama_num_predict: int = 700       # cap output tokens; stops a runaway generation from eating the timeout
    ollama_num_thread: int = 0          # 0 = let Ollama auto-detect; set explicitly if CPU detection is wrong
    ollama_keep_alive: str = "30m"      # keep the model loaded in RAM between calls (Ollama default is "5m")
    ollama_think: bool = False          # Qwen3 "thinking" mode (slower, off by default)
    llm_max_retries: int = 1            # extra attempts after the first failure (each retry re-pays the full timeout)
    llm_fallback: bool = True           # produce a rule-based analysis if the LLM fails
    llm_max_issue_groups: int = 20      # issue groups sent to the model (fewer = smaller prompt = faster on CPU)
    llm_max_examples: int = 2           # example URLs per issue group

    # --------------------------------------- Google Maps lead discovery (Agent 5)
    # Agent 5 drives a real browser with Playwright - no Google API key, Google Cloud
    # account or billing is needed. One-time setup: pip install playwright, then
    # "playwright install chromium".
    maps_headless: bool = False         # False = you can watch the browser; True = invisible
    maps_timeout_seconds: float = 30    # how long to wait for each Google Maps page/element
    maps_browser_channel: str = ""      # "" = Playwright's Chromium; "chrome" / "msedge" = installed browser

    # ------------------------------------------------------------ PDF report
    pdf_page_size: str = "A4"           # A4 | LETTER
    pdf_max_appendix_rows: int = 50     # rows of issues.csv echoed in the PDF


def _coerce(name: str, tp: Any, value: Any) -> Any:
    """Convert a raw value (JSON / env string / CLI) to the field's declared type."""
    try:
        if tp is bool:
            if isinstance(value, str):
                return value.strip().lower() in {"1", "true", "yes", "on"}
            return bool(value)
        if tp is Path:
            path = Path(str(value)).expanduser()
            return path if path.is_absolute() else (PROJECT_ROOT / path).resolve()
        if tp in (int, float, str):
            return tp(value)
        if tp == list[str]:
            if isinstance(value, str):
                return shlex.split(value)
            return [str(item) for item in value]
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"Invalid value for '{name}': {value!r} ({exc})") from exc
    raise ConfigError(f"Unsupported config type for '{name}': {tp}")


def _validate(cfg: Config) -> None:
    """Cross-field sanity checks so problems surface at start-up, not mid-run."""
    if not cfg.enable_agent3 and not cfg.enable_agent4:
        raise ConfigError("At least one of 'enable_agent3' / 'enable_agent4' must be true.")
    if not cfg.beyondseo_cmd:
        raise ConfigError("'beyondseo_cmd' must not be empty.")
    if cfg.crawl_mode not in VALID_CRAWL_MODES:
        raise ConfigError(f"'crawl_mode' must be one of {sorted(VALID_CRAWL_MODES)}.")
    if cfg.log_level.upper() not in VALID_LOG_LEVELS:
        raise ConfigError(f"'log_level' must be one of {sorted(VALID_LOG_LEVELS)}.")
    if cfg.pdf_page_size.upper() not in VALID_PAGE_SIZES:
        raise ConfigError(f"'pdf_page_size' must be one of {sorted(VALID_PAGE_SIZES)}.")
    if cfg.crawl_max_pages < 1:
        raise ConfigError("'crawl_max_pages' must be at least 1.")
    if cfg.crawl_workers < 1:
        raise ConfigError("'crawl_workers' must be at least 1.")
    if cfg.crawl_request_timeout <= 0:
        raise ConfigError("'crawl_request_timeout' must be greater than 0.")
    if cfg.crawl_retries < 0:
        raise ConfigError("'crawl_retries' cannot be negative.")
    if cfg.crawl_delay < 0:
        raise ConfigError("'crawl_delay' cannot be negative.")
    if cfg.llm_max_retries < 0:
        raise ConfigError("'llm_max_retries' cannot be negative.")
    if cfg.ollama_num_ctx < 512:
        raise ConfigError("'ollama_num_ctx' must be at least 512.")
    if cfg.ollama_num_predict < 1:
        raise ConfigError("'ollama_num_predict' must be at least 1.")
    if cfg.ollama_num_thread < 0:
        raise ConfigError("'ollama_num_thread' cannot be negative.")
    if cfg.maps_timeout_seconds <= 0:
        raise ConfigError("'maps_timeout_seconds' must be greater than 0.")


def load_config(
    config_file: Path | None = None,
    overrides: dict[str, Any] | None = None,
) -> Config:
    """Build the effective Config from defaults, file, environment and overrides."""
    values: dict[str, Any] = {}

    # 2. JSON config file
    path = Path(config_file) if config_file else DEFAULT_CONFIG_FILE
    if path.is_file():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ConfigError(f"Could not read config file {path}: {exc}") from exc
        if not isinstance(loaded, dict):
            raise ConfigError(f"Config file {path} must contain a JSON object.")
        values.update(loaded)
    elif config_file is not None:
        raise ConfigError(f"Config file not found: {config_file}")

    # 3. Environment variables
    field_types = {f.name: f.type for f in fields(Config)}
    for name in field_types:
        env_value = os.environ.get(ENV_PREFIX + name.upper())
        if env_value is not None:
            values[name] = env_value

    # 4. Explicit overrides (ignore None so unset CLI flags don't clobber anything)
    if overrides:
        values.update({k: v for k, v in overrides.items() if v is not None})

    unknown = set(values) - set(field_types)
    if unknown:
        raise ConfigError(f"Unknown config option(s): {', '.join(sorted(unknown))}")

    cfg = Config(**{name: _coerce(name, field_types[name], val) for name, val in values.items()})
    _validate(cfg)
    return cfg