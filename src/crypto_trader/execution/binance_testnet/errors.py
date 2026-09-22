"""Typed errors for the Binance SPOT TESTNET adapter (Phase 11).

Every failure mode is explicit: an ambiguous write never turns into a silent retry, and a mainnet
host never turns into a silent fallback.
"""

from __future__ import annotations

__all__ = [
    "AmbiguousOrderError",
    "BinanceTestnetError",
    "ClockDriftError",
    "MainnetForbiddenError",
    "RateLimitBanError",
    "RateLimitError",
    "TestnetCredentialsRequiredError",
    "TestnetEndpointError",
    "TestnetSymbolError",
    "TestnetRejectedError",
    "UnsafeTestnetAccountError",
]


class BinanceTestnetError(RuntimeError):
    """Base class for every Binance Spot Testnet adapter failure."""

class TestnetEndpointError(BinanceTestnetError):
    """The configured endpoint is not the documented Spot Testnet endpoint."""


class MainnetForbiddenError(TestnetEndpointError):
    """A production/mainnet endpoint was requested; Phase 11 forbids it outright."""


class TestnetCredentialsRequiredError(BinanceTestnetError):
    """The required testnet credentials are not present in the environment.

    The message NEVER contains a credential value - only the environment variable names.
    """


class AmbiguousOrderError(BinanceTestnetError):
    """A write had an unknown outcome.

    This is the ``UNKNOWN_PENDING_RECONCILIATION`` case: the order may or may not exist at the
    venue, so it is NEVER blindly retried and never locally marked failed/rejected.
    """


class RateLimitError(BinanceTestnetError):
    """A rate limit was hit and the caller must back off (retry-after honoured)."""


class RateLimitBanError(RateLimitError):
    """An IP ban was signalled; no further request may be attempted in this run."""


class ClockDriftError(BinanceTestnetError):
    """The local clock drifted beyond the configured tolerance; refuse to sign requests."""


class TestnetRejectedError(BinanceTestnetError):
    """The venue rejected the request deterministically (a real, non-ambiguous outcome)."""


class UnsafeTestnetAccountError(BinanceTestnetError):
    """The testnet account has unsafe permissions (for example withdrawals are enabled)."""


class TestnetSymbolError(BinanceTestnetError):
    """The instrument is missing or its trading rules cannot be satisfied without rounding."""
