"""Mainnet read-only endpoint policy (Phase 15).

The mainnet adapter may only reach a small, explicit allowlist of READ-ONLY paths on a documented
mainnet host. The allowlist is the primary control: a path that is not on it is refused before any
transport call, so no order-mutating endpoint is reachable even by mistake.

A separate, explicit mutation denylist exists as defence in depth so a caller that asks for an
order-mutating path receives a dedicated typed refusal. The order path literal is taken from the
Phase 11 adapter constant rather than re-typed here, so this module never re-declares an
order-mutating endpoint of its own.
"""

from __future__ import annotations

from urllib.parse import urlparse

from crypto_trader.execution.binance_testnet import MAINNET_HOST_MARKERS
from crypto_trader.execution.binance_testnet.client import PATH_OPEN_ORDERS, PATH_ORDER

from .errors import MainnetEndpointError, MainnetMutationForbiddenError

__all__ = [
    "MUTATION_PATH_MARKERS",
    "READ_ONLY_PATHS",
    "SPOT_MAINNET_REST_BASE",
    "refuse_mutation_path",
    "require_mainnet_base_url",
    "require_read_only_path",
]

#: The documented Binance SPOT mainnet REST base URL.
SPOT_MAINNET_REST_BASE = "https://api.binance.com"

#: Paths that stay reachable during a mainnet READ-ONLY dry-run (allowlist, exact match).
READ_ONLY_PATHS = frozenset(
    {
        "/api/v3/time",
        "/api/v3/account",
        PATH_OPEN_ORDERS,
        "/api/v3/myTrades",
        "/api/v3/exchangeInfo",
        "/api/v3/ticker/price",
        "/api/v3/ticker/bookTicker",
        "/api/v3/depth",
        "/sapi/v1/account/apiRestrictions",
    }
)

#: Path fragments that no read-only adapter may ever touch (defence in depth).
#:
#: ``PATH_ORDER`` is imported from the authorised Phase 11 adapter so this module never re-declares
#: an order-mutating endpoint literal of its own.
MUTATION_PATH_MARKERS: tuple[str, ...] = (
    PATH_ORDER,
    "/sapi/v1/capital/",
    "/sapi/v1/asset/",
    "/sapi/v1/sub-account/",
)


def require_mainnet_base_url(url: str) -> str:
    """Return the URL when it is a documented mainnet host, else fail closed."""
    if type(url) is not str or not url.strip():
        raise MainnetEndpointError("a mainnet REST base URL is required.")
    try:
        parsed = urlparse(url.strip())
    except ValueError:
        parsed = None
    if parsed is None:
        raise MainnetEndpointError("the mainnet read-only base URL is malformed.")
    if parsed.scheme != "https":
        raise MainnetEndpointError("the mainnet read-only base URL must use https.")
    host = (parsed.hostname or "").lower()
    if any(marker in host for marker in ("testnet", "binance.vision")):
        raise MainnetEndpointError(
            "a testnet host was supplied to the mainnet read-only client; refusing to use it."
        )
    if host not in MAINNET_HOST_MARKERS:
        raise MainnetEndpointError(
            f"{host!r} is not a documented Binance mainnet host; refusing to use it."
        )
    try:
        port = parsed.port
    except ValueError:
        malformed_port = True
    else:
        malformed_port = False
    if malformed_port:
        raise MainnetEndpointError("mainnet base URL has a malformed port.")
    if (
        parsed.username is not None or parsed.password is not None or port is not None
        or parsed.path not in ("", "/") or parsed.params or parsed.query or parsed.fragment
    ):
        raise MainnetEndpointError(
            "the mainnet read-only base URL must be a trusted HTTPS origin only."
        )
    # Empty query/fragment delimiters survive urlparse with empty values.
    # Require the complete supplied URL to be exactly the verified origin.
    origin = f"https://{host}"
    if url.strip().lower() not in (origin, origin + "/"):
        raise MainnetEndpointError("mainnet base URL must contain only a trusted origin.")
    return origin


def refuse_mutation_path(path: str) -> None:
    """Fail closed on any order-mutating or account-mutating path fragment."""
    lowered = (path or "").lower()
    for marker in MUTATION_PATH_MARKERS:
        if marker.lower() in lowered:
            raise MainnetMutationForbiddenError(
                "this path mutates mainnet state and is unreachable in a read-only dry-run."
            )


def require_read_only_path(path: str) -> str:
    """Return the path when it is on the read-only allowlist AND not a mutation path."""
    if type(path) is not str or not path.startswith("/"):
        raise MainnetEndpointError("a request path must be an absolute path beginning with '/'.")
    refuse_mutation_path(path)
    if path not in READ_ONLY_PATHS:
        raise MainnetEndpointError(
            f"{path!r} is not on the mainnet read-only allowlist; refusing to use it."
        )
    return path
