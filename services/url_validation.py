"""
services/url_validation.py - validate the URL typed into the web form.

Agent 1 remains the single source of truth for normalising a URL (it is what
the CLI uses). This module only adds the friendly, specific messages a web
form needs *before* a job is created, and rejects input Agent 1 would accept
but that makes no sense in a browser form (credentials, other schemes).
"""

from urllib.parse import urlparse

from agents.agent1_reader import FileReaderAgent

MAX_URL_LENGTH = 2048


class InvalidURLError(ValueError):
    """Raised with a user-facing message when the entered URL cannot be audited."""


def validate_url(raw: str | None) -> str:
    """Return the normalised absolute URL or raise InvalidURLError."""
    value = (raw or "").strip()
    if not value:
        raise InvalidURLError("Please enter a website URL, for example https://example.com.")
    if len(value) > MAX_URL_LENGTH:
        raise InvalidURLError("That URL is too long.")
    if any(ch.isspace() for ch in value):
        raise InvalidURLError("The URL must not contain spaces.")
    if any(ord(ch) < 32 for ch in value):
        raise InvalidURLError("The URL contains invalid characters.")

    if "://" in value:
        scheme = value.split("://", 1)[0].lower()
        if scheme not in {"http", "https"}:
            raise InvalidURLError(f"Only http:// and https:// addresses can be audited (got '{scheme}://').")

    normalized = FileReaderAgent._normalize_url(value)   # same rules as the CLI
    if normalized is None:
        raise InvalidURLError(
            "That does not look like a valid website address. "
            "Use a full domain such as https://example.com."
        )
    parsed = urlparse(normalized)
    if parsed.username or parsed.password:
        raise InvalidURLError("URLs containing a username or password are not supported.")
    try:
        parsed.port  # noqa: B018 - raises ValueError for a malformed port
    except ValueError:
        raise InvalidURLError("The port number in the URL is not valid.") from None
    return normalized
