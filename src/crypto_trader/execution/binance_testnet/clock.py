"""Timestamp and clock handling for the Spot Testnet API (Phase 11).

Binance rejects signed requests whose ``timestamp`` is outside ``recvWindow`` of the server clock
(error -1021). This module makes that behaviour explicit and testable:

* the clock never reads the wall clock at import time; a time source is injected;
* ``synchronize`` applies the measured server offset;
* ``recv_window_ms`` is clamped to the documented maximum;
* the timestamp sent is local time PLUS the measured offset, so a large-but-measured offset is
  harmless as long as it stays inside the receive window.

Two guards stay fail-closed:

* signing before any synchronization raises (an unknown offset cannot be compensated);
* an absurd offset (a broken local time source rather than ordinary clock skew) raises instead of
  being trusted.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from .errors import ClockDriftError, TestnetEndpointError

__all__ = ["MAX_ABS_CLOCK_OFFSET_MS", "MAX_RECV_WINDOW_MS", "TestnetClock"]

#: Documented maximum for ``recvWindow``.
MAX_RECV_WINDOW_MS = 60_000

#: Beyond this the local time source is treated as broken rather than merely skewed.
MAX_ABS_CLOCK_OFFSET_MS = 600_000


@dataclass
class TestnetClock:
    """A testnet clock with an explicit server offset and tolerance."""

    time_source: Callable[[], float]
    offset_ms: int = 0
    recv_window_ms: int = 5_000
    max_drift_ms: int | None = None

    def __post_init__(self) -> None:
        if not callable(self.time_source):
            raise TestnetEndpointError("a time source callable is required.")
        self._set_recv_window(self.recv_window_ms)
        if self.max_drift_ms is None:
            # The compensated timestamp only has to remain inside the receive window.
            self.max_drift_ms = self.recv_window_ms
        if self.max_drift_ms <= 0:
            raise TestnetEndpointError("max_drift_ms must be positive when configured.")

    # --- configuration ---

    def _set_recv_window(self, value: int) -> None:
        if not isinstance(value, int) or value <= 0:
            raise TestnetEndpointError("recv_window_ms must be a positive integer.")
        if value > MAX_RECV_WINDOW_MS:
            raise TestnetEndpointError(
                f"recv_window_ms {value} exceeds the documented maximum {MAX_RECV_WINDOW_MS}."
            )
        self.recv_window_ms = value

    @property
    def is_synchronized(self) -> bool:
        return bool(getattr(self, "_synchronized", False))

    # --- synchronization ---

    def local_time_ms(self) -> int:
        return int(self.time_source() * 1000)

    def timestamp_ms(self) -> int:
        """The timestamp to send: local time plus the measured server offset."""
        return self.local_time_ms() + self.offset_ms

    def synchronize(self, server_time_ms: int, *, local_time_ms: int | None = None) -> int:
        """Apply the measured server offset and return it (milliseconds)."""
        if not isinstance(server_time_ms, int) or server_time_ms <= 0:
            raise TestnetEndpointError("server_time_ms must be a positive integer.")
        local = self.local_time_ms() if local_time_ms is None else int(local_time_ms)
        self.offset_ms = server_time_ms - local
        self._synchronized = True
        return self.offset_ms

    def require_acceptable_drift(self, *, local_time_ms: int | None = None) -> int:
        """Fail closed when the clock is unsynchronized, absurd or beyond tolerance."""
        if not self.is_synchronized:
            raise ClockDriftError(
                "the testnet clock has not been synchronized with the server yet; refusing to "
                "sign requests with an unknown offset."
            )
        drift = abs(self.offset_ms)
        if drift > MAX_ABS_CLOCK_OFFSET_MS:
            raise ClockDriftError(
                f"the measured server offset ({drift} ms) is implausibly large; treating the local "
                "time source as broken and refusing to sign requests."
            )
        assert self.max_drift_ms is not None
        if drift > self.max_drift_ms:
            raise ClockDriftError(
                f"clock drift {drift} ms exceeds the receive window {self.max_drift_ms} ms; refusing "
                "to send a request that would be rejected as out of recvWindow."
            )
        del local_time_ms
        return drift

    def signed_timestamp_params(self, params: dict[str, object]) -> dict[str, object]:
        """Add ``timestamp`` and ``recvWindow`` to a parameter mapping."""
        self.require_acceptable_drift()
        enriched = dict(params)
        enriched["recvWindow"] = self.recv_window_ms
        enriched["timestamp"] = self.timestamp_ms()
        return enriched
