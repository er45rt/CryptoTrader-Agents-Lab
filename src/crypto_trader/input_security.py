"""Strict operator-controlled configuration validators (S-03).

These checks reject malformed or downgraded URL overrides before any transport
can be constructed. They never print credentials or environment values.
"""
from __future__ import annotations

import re
from urllib.parse import urlsplit

_SYMBOL_RE = re.compile(r"[A-Z0-9]{5,20}", flags=re.ASCII)


def valid_market_symbol(value: object) -> bool:
    return isinstance(value, str) and _SYMBOL_RE.fullmatch(value) is not None


def require_https_base_url(value: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError("base URL must be a valid HTTPS URL")
    if any(ord(char) <= 32 or ord(char) == 127 for char in value):
        raise ValueError("base URL contains forbidden control or space characters")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("base URL is malformed") from exc
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
            or parsed.password is not None or parsed.query or parsed.fragment):
        raise ValueError("base URL must use HTTPS without credentials, query or fragment")
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("base URL port out of range")
    return value.rstrip("/")
