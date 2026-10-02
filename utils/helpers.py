"""
utils/helpers.py - small filesystem / text helpers shared by the agents.
"""

import re
from pathlib import Path
from urllib.parse import urlparse


def slugify_url(url: str, max_length: int = 80) -> str:
    """Turn a URL into a safe folder name, e.g. https://www.a.com/b -> a-com-b."""
    parsed = urlparse(url)
    host = parsed.netloc.lower().removeprefix("www.")
    raw = f"{host}{parsed.path}"
    slug = re.sub(r"[^a-z0-9]+", "-", raw.lower()).strip("-")
    return (slug or "site")[:max_length].strip("-") or "site"


def ensure_within(path: Path, parent: Path) -> None:
    """Raise ValueError unless ``path`` is inside ``parent`` (guards rmtree)."""
    resolved_path = path.resolve()
    resolved_parent = parent.resolve()
    if resolved_parent != resolved_path and resolved_parent not in resolved_path.parents:
        raise ValueError(f"Refusing to touch {resolved_path}: outside {resolved_parent}")


def truncate(text: object, limit: int) -> str:
    """Collapse whitespace and cut ``text`` to ``limit`` characters with an ellipsis."""
    cleaned = " ".join(str(text or "").split())
    return cleaned if len(cleaned) <= limit else cleaned[: limit - 3].rstrip() + "..."
