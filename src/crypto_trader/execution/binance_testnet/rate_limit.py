"""Deterministic rate-limit and retry policy for the Spot Testnet API (Phase 11).

* a token bucket tracks request weight against an explicit budget;
* the backoff schedule is deterministic (no jitter) so runs stay reproducible;
* HTTP 429 backs off, HTTP 418 (IP ban) stops the run;
* retries are only ever applied to SAFE (read) requests - an ambiguous write is never retried.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable

from .errors import RateLimitBanError, RateLimitError

__all__ = ["RateLimitBudget", "RetryPolicy", "TokenBucket"]


@dataclass
class TokenBucket:
    """A deterministic weight budget replenished at a fixed rate."""

    capacity: int
    refill_per_second: float
    monotonic: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep
    _tokens: float = field(init=False, default=0.0)
    _updated_at: float = field(init=False, default=0.0)

    def __post_init__(self) -> None:
        if self.capacity <= 0:
            raise ValueError("capacity must be positive.")
        if self.refill_per_second <= 0:
            raise ValueError("refill_per_second must be positive.")
        self._tokens = float(self.capacity)
        self._updated_at = self.monotonic()

    def _replenish(self) -> None:
        now = self.monotonic()
        elapsed = max(0.0, now - self._updated_at)
        if elapsed:
            self._tokens = min(float(self.capacity), self._tokens + elapsed * self.refill_per_second)
            self._updated_at = now

    def wait_seconds_for(self, weight: int) -> float:
        """Seconds to wait before ``weight`` can be consumed (0.0 when it fits now)."""
        if weight <= 0:
            raise ValueError("weight must be positive.")
        if weight > self.capacity:
            raise RateLimitError(
                f"request weight {weight} exceeds the configured budget {self.capacity}."
            )
        self._replenish()
        if self._tokens >= weight:
            return 0.0
        deficit = weight - self._tokens
        return deficit / self.refill_per_second

    def acquire(self, weight: int = 1) -> float:
        """Consume ``weight`` tokens, sleeping when necessary. Returns the slept seconds."""
        waited = self.wait_seconds_for(weight)
        if waited > 0:
            self.sleep(waited)
            self._tokens = 0.0
            self._updated_at = self.monotonic()
        else:
            self._tokens -= weight
        return waited

    @property
    def available(self) -> float:
        self._replenish()
        return self._tokens


@dataclass(frozen=True)
class RetryPolicy:
    """Deterministic exponential backoff for SAFE (read-only) requests."""

    max_attempts: int = 3
    base_delay_seconds: float = 0.5
    max_delay_seconds: float = 8.0

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be at least 1.")
        if self.base_delay_seconds <= 0 or self.max_delay_seconds <= 0:
            raise ValueError("delays must be positive.")

    def delay_for_attempt(self, attempt: int) -> float:
        """Delay before retry number ``attempt`` (1-based). Deterministic, no jitter."""
        if attempt < 1:
            raise ValueError("attempt must be >= 1.")
        return min(self.max_delay_seconds, self.base_delay_seconds * (2 ** (attempt - 1)))


@dataclass
class RateLimitBudget:
    """Tracks the venue's rate-limit signalling for one client instance."""

    bucket: TokenBucket
    backoff_until: float = 0.0
    banned_until: float = 0.0
    violations: int = 0
    banned: bool = False
    sleep: Callable[[float], None] = time.sleep
    monotonic: Callable[[], float] = time.monotonic

    def record_429(self, retry_after_seconds: float | None) -> float:
        """Record an HTTP 429 and return how long the caller should back off."""
        self.violations += 1
        delay = float(retry_after_seconds) if retry_after_seconds else 1.0
        self.backoff_until = max(self.backoff_until, self.monotonic() + delay)
        return delay

    def record_418(self, retry_after_seconds: float | None) -> None:
        """Record an HTTP 418 IP ban: stop the run entirely."""
        self.violations += 1
        self.banned = True
        self.banned_until = self.monotonic() + float(retry_after_seconds or 60.0)
        raise RateLimitBanError(
            "the venue signalled an IP ban (HTTP 418); no further request is attempted in this run."
        )

    def enforce_backoff(self) -> float:
        """Sleep out any pending backoff window before the next request."""
        now = self.monotonic()
        if self.banned:
            raise RateLimitBanError("this client is banned for the remainder of the run.")
        if now < self.backoff_until:
            delay = self.backoff_until - now
            self.sleep(delay)
            return delay
        return 0.0
