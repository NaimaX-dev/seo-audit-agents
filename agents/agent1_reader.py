"""
agents/agent1_reader.py - Agent 1: File Reader Agent.

Responsibility
--------------
Read website URLs from a CSV file (column name: ``url``), clean them up and
hand them over - one ``WebsiteTarget`` per site - to Agent 2.

Cleaning rules
--------------
* blank rows are skipped
* a missing scheme gets ``https://`` (``example.com`` -> ``https://example.com/``)
* only http/https URLs with a hostname are accepted
* duplicates (after normalization) are dropped
* every site gets a unique, filesystem-safe folder name (slug)
"""

import logging
from urllib.parse import urlparse, urlunparse

import pandas as pd

from agents.models import WebsiteTarget
from config import Config
from utils.helpers import slugify_url


class FileReaderError(RuntimeError):
    """Raised when the input CSV cannot be read or contains no usable URLs."""


class FileReaderAgent:
    name = "Agent1-FileReader"

    def __init__(self, config: Config) -> None:
        self.cfg = config
        self.log = logging.getLogger(self.name)

    # ------------------------------------------------------------------ public
    def run(self) -> list[WebsiteTarget]:
        """Read the CSV and return the validated, de-duplicated website list."""
        path = self.cfg.input_csv
        self.log.info("Reading websites from %s", path)

        raw_urls = self._read_url_column(path)
        targets: list[WebsiteTarget] = []
        seen_urls: set[str] = set()
        used_slugs: set[str] = set()

        for entry_no, raw in enumerate(raw_urls, start=1):
            raw = raw.strip()
            if not raw:
                continue
            url = self._normalize_url(raw)
            if url is None:
                self.log.warning("Entry %d: skipping invalid URL %r", entry_no, raw)
                continue
            if url in seen_urls:
                self.log.warning("Entry %d: skipping duplicate URL %s", entry_no, url)
                continue

            seen_urls.add(url)
            slug = self._unique_slug(slugify_url(url), used_slugs)
            targets.append(WebsiteTarget(url=url, slug=slug))

        if not targets:
            raise FileReaderError(f"No valid URLs found in {path}")

        self.log.info("Agent 1 found %d website(s) to audit", len(targets))
        return targets

    # ----------------------------------------------------------------- helpers
    def _read_url_column(self, path) -> list[str]:
        if not path.is_file():
            raise FileReaderError(f"Input CSV not found: {path}")
        try:
            # utf-8-sig transparently strips the BOM that Excel adds to CSV files.
            df = pd.read_csv(path, dtype=str, encoding="utf-8-sig", keep_default_na=False)
        except pd.errors.EmptyDataError as exc:
            raise FileReaderError(f"Input CSV is empty: {path}") from exc
        except (pd.errors.ParserError, UnicodeDecodeError, OSError) as exc:
            raise FileReaderError(f"Could not parse {path}: {exc}") from exc

        columns = {str(col).strip().lower(): col for col in df.columns}
        if "url" not in columns:
            raise FileReaderError(
                f"{path} must contain a column named 'url' (found: {list(df.columns)})"
            )
        return df[columns["url"]].tolist()

    @staticmethod
    def _normalize_url(raw: str) -> str | None:
        """Return a clean absolute http(s) URL, or None if ``raw`` is unusable."""
        if any(ch.isspace() for ch in raw):
            return None
        candidate = raw if "://" in raw else f"https://{raw}"
        try:
            parsed = urlparse(candidate)
            hostname = parsed.hostname  # also validates the netloc syntax
        except ValueError:
            return None
        if parsed.scheme.lower() not in {"http", "https"} or not hostname:
            return None
        if "." not in hostname and hostname != "localhost":
            return None  # e.g. "abc" - almost certainly a typo, not a website

        netloc = parsed.netloc.lower()
        path = parsed.path or "/"
        return urlunparse((parsed.scheme.lower(), netloc, path, "", parsed.query, ""))

    @staticmethod
    def _unique_slug(slug: str, used: set[str]) -> str:
        candidate, counter = slug, 2
        while candidate in used:
            candidate = f"{slug}-{counter}"
            counter += 1
        used.add(candidate)
        return candidate
