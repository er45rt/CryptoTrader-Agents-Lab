"""HMAC-SHA256 request signing for the Spot Testnet API (Phase 11).

Signing is deliberately narrow: it produces a signature string and never stores or returns the
secret. Redaction helpers exist so a request can be logged safely (key/signature masked).
"""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Mapping, Sequence
from urllib.parse import urlencode

__all__ = [
    "build_query_string",
    "redact_query_string",
    "sign_query_string",
    "signed_query_string",
]

#: Query parameters whose values must never appear in a log, report or journal.
SENSITIVE_PARAMS = ("signature", "apiKey", "api_key", "secret", "token")


def build_query_string(params: Mapping[str, object] | Sequence[tuple[str, object]]) -> str:
    """Build the canonical query string used for signing (insertion order preserved)."""
    items = params.items() if isinstance(params, Mapping) else params
    return urlencode([(key, value) for key, value in items], doseq=True)


def sign_query_string(query_string: str, api_secret: str) -> str:
    """Return the lowercase hex HMAC-SHA256 signature of ``query_string``."""
    return hmac.new(
        api_secret.encode("utf-8"), query_string.encode("utf-8"), hashlib.sha256
    ).hexdigest()


def signed_query_string(params: Mapping[str, object], api_secret: str) -> str:
    """Return ``params`` plus ``signature``, URL-encoded."""
    query = build_query_string(params)
    return f"{query}&signature={sign_query_string(query, api_secret)}"


def redact_query_string(query_string: str) -> str:
    """Replace sensitive parameter values with a placeholder (safe for logs/reports)."""
    if not query_string:
        return ""
    parts: list[str] = []
    for chunk in query_string.split("&"):
        key, separator, _value = chunk.partition("=")
        if key in SENSITIVE_PARAMS:
            parts.append(f"{key}{separator}<redacted>" if separator else key)
        else:
            parts.append(chunk)
    return "&".join(parts)
