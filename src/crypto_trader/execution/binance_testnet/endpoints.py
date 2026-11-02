"""Testnet-only endpoint policy (Phase 11).

Phase 11 targets **Binance Spot Testnet** and nothing else. Every endpoint used by this adapter is
declared here and validated against an explicit mainnet blocklist, so a misconfigured base URL
fails closed instead of silently reaching production. Futures, options and margin hostnames are
blocked as well (out of scope for this phase).

The public read-only Phase 1 data layer keeps its own (production, public, no-credential) endpoint;
it is a different code path and is NOT involved in any authenticated testnet request.
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

from .errors import MainnetForbiddenError, TestnetEndpointError

__all__ = [
    "MAINNET_HOST_MARKERS",
    "SPOT_TESTNET_REST_BASE",
    "TESTNET_HOST_SUFFIXES",
    "assert_no_mainnet_reference",
    "is_testnet_host",
    "require_testnet_base_url",
]

#: The one documented Binance SPOT Testnet REST base URL used by this phase.
SPOT_TESTNET_REST_BASE = "https://testnet.binance.vision"

#: Hosts that must never be reachable from this adapter.
MAINNET_HOST_MARKERS = (
    "api.binance.com",
    "api1.binance.com",
    "api2.binance.com",
    "api3.binance.com",
    "api4.binance.com",
    "api-gcp.binance.com",
    "fapi.binance.com",
    "dapi.binance.com",
    "api.binance.us",
    "binance.us",
)

#: Only hosts ending with one of these suffixes are accepted.
TESTNET_HOST_SUFFIXES = ("binance.vision", "testnet.binance.vision")

_HOST_RE = re.compile(r"^[a-z0-9.-]+$")


def is_testnet_host(url: str) -> bool:
    """True only for a documented Binance Spot Testnet host."""
    if not isinstance(url, str) or not url:
        return False
    host = urlparse(url).hostname or ""
    host = host.lower()
    if not host or not _HOST_RE.match(host):
        return False
    if host in MAINNET_HOST_MARKERS:
        return False
    return any(host == suffix or host.endswith("." + suffix) for suffix in TESTNET_HOST_SUFFIXES)


def require_testnet_base_url(url: str) -> str:
    """Return the URL when it is a documented testnet host, else fail closed."""
    if not isinstance(url, str) or not url:
        raise TestnetEndpointError("a REST base URL is required.")
    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise TestnetEndpointError("the testnet REST base URL must use https.")
    host = (parsed.hostname or "").lower()
    if host in MAINNET_HOST_MARKERS or "binance.com" in host:
        raise MainnetForbiddenError(
            "Binance mainnet endpoints are forbidden in Phase 11; refusing to use "
            f"{host!r}. Only the Spot Testnet endpoint is permitted."
        )
    documented_host = urlparse(SPOT_TESTNET_REST_BASE).hostname
    if host != documented_host:
        raise TestnetEndpointError(
            f"{host!r} is not the documented Binance Spot Testnet host; refusing to use it."
        )
    return url.rstrip("/")


def assert_no_mainnet_reference(text: str) -> None:
    """Fail if any mainnet host marker appears in ``text`` (used by scope tests/guards)."""
    lowered = (text or "").lower()
    for marker in MAINNET_HOST_MARKERS:
        if marker in lowered:
            raise MainnetForbiddenError(f"mainnet host marker {marker!r} must never appear here.")
