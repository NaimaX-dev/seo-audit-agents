"""
utils/logging_setup.py - console + rotating-file logging for the whole project.

The console shows INFO (or whatever --log-level says); the log file always keeps
DEBUG detail so a failed run can be diagnosed after the fact.
Messages are kept ASCII-only so they print safely on any Windows console.
"""

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)-18s | %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def setup_logging(logs_dir: Path, level: str = "INFO") -> Path:
    """Configure the root logger and return the log file path."""
    logs_dir.mkdir(parents=True, exist_ok=True)
    log_file = logs_dir / "seo_audit.log"

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    root.handlers.clear()  # avoid duplicate lines if called twice

    formatter = logging.Formatter(LOG_FORMAT, DATE_FORMAT)

    console = logging.StreamHandler()
    console.setLevel(getattr(logging, level.upper(), logging.INFO))
    console.setFormatter(formatter)
    root.addHandler(console)

    file_handler = RotatingFileHandler(
        log_file, maxBytes=1_000_000, backupCount=5, encoding="utf-8"
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    # Third-party libraries are chatty at DEBUG; keep the log readable.
    for noisy in ("httpx", "httpcore", "PIL", "matplotlib"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    return log_file
