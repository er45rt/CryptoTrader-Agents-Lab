"""Typed errors for the Binance SPOT TESTNET adapter (Phase 11).

Every failure mode is explicit: an ambiguous write never turns into a silent retry, and a mainnet
host never turns into a silent fallback.
"""

from __future__ import annotations

__all__ = [
    "AmbiguousOrderError",
    "AttemptJournalRequiredError",
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
    "UnsupportedOrderTypeError",
    "VenueProtocolRejectionError",
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


class VenueProtocolRejectionError(ClockDriftError):
    """The venue mechanically rejected the request before any order could be created.

    This is a transport/venue fact (``VENUE_RESPONDED``), not a local rejection and not an ordinary
    venue order rejection: Binance ``-1021`` means the signed timestamp was outside ``recvWindow``,
    so the order was never processed. The write is never retried; the durable submission attempt
    stays as historical proof that a submission was attempted.
    """

    reason_code = "venue_protocol_rejection_before_order_creation"
    evidence_code = "venue_timestamp_outside_recv_window"


class TestnetRejectedError(BinanceTestnetError):
    """The venue rejected the request deterministically (a real, non-ambiguous outcome)."""


class UnsafeTestnetAccountError(BinanceTestnetError):
    """The testnet account has unsafe permissions (for example withdrawals are enabled)."""


class UnsupportedOrderTypeError(BinanceTestnetError):
    """A venue-writing order type has no approved fail-closed validation contract."""


class AttemptJournalRequiredError(BinanceTestnetError):
    """A venue write was refused because no durable attempt journal is available.

    Stage 2 invariant: a new venue-writing path must fail closed *before* any POST unless it can
    commit the durable ``order.submission.attempted`` marker first. This error is never raised for
    a successful write, and it never follows a POST.
    """


class TestnetSymbolError(BinanceTestnetError):
    """The instrument is missing or its trading rules cannot be satisfied without rounding."""
