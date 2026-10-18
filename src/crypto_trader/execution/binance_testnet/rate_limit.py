"""Deterministic rate-limit and retry policy for the Spot Testnet API (Phase 11).

* a token bucket tracks request weight against an explicit budget;
* the backoff schedule is deterministic (no jitter) so runs stay reproducible;
* HTTP 429 backs off, HTTP 418 (IP ban) stops the run;
* retries are only ever applied to SAFE (read) requests - an ambiguous write is never retried.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Callable

from .errors import RateLimitAdmissionDeferredError, RateLimitBanError, RateLimitError

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
    _lock: threading.Lock = field(
        init=False, repr=False, compare=False, default_factory=threading.Lock
    )

    def __post_init__(self) -> None:
        if self.capacity <= 0:
            raise ValueError("capacity must be positive.")
        if self.refill_per_second <= 0:
            raise ValueError("refill_per_second must be positive.")
        self._tokens = float(self.capacity)
        self._updated_at = self.monotonic()

    def _replenish_unlocked(self) -> None:
        now = self.monotonic()
        elapsed = max(0.0, now - self._updated_at)
        if elapsed:
            self._tokens = min(float(self.capacity), self._tokens + elapsed * self.refill_per_second)
            self._updated_at = now

    def _wait_seconds_for_unlocked(self, weight: int) -> float:
        if weight <= 0:
            raise ValueError("weight must be positive.")
        if weight > self.capacity:
            raise RateLimitError(
                f"request weight {weight} exceeds the configured budget {self.capacity}."
            )
        self._replenish_unlocked()
        if self._tokens >= weight:
            return 0.0
        deficit = weight - self._tokens
        return deficit / self.refill_per_second

    def wait_seconds_for(self, weight: int) -> float:
        """Seconds to wait before ``weight`` can be consumed (0.0 when it fits now)."""
        with self._lock:
            return self._wait_seconds_for_unlocked(weight)

    def acquire_now(self, weight: int = 1) -> float:
        """Atomically consume tokens only when available; never sleep.

        Returns 0.0 on success, or the delay that would be required without consuming.
        """
        with self._lock:
            waited = self._wait_seconds_for_unlocked(weight)
            if waited > 0:
                return waited
            self._tokens -= weight
            return 0.0

    def acquire(self, weight: int = 1) -> float:
        """Consume ``weight`` tokens, sleeping when necessary. Returns the slept seconds."""
        waited = self.acquire_now(weight)
        if waited > 0:
            self.sleep(waited)
            with self._lock:
                # Preserve the existing conservative one-wait accounting semantics.
                self._tokens = 0.0
                self._updated_at = self.monotonic()
        return waited

    @property
    def available(self) -> float:
        with self._lock:
            self._replenish_unlocked()
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

    def admit_now(self, weight: int = 1) -> None:
        """Consume request weight only when no wait is required; otherwise fail immediately."""
        if self.banned:
            raise RateLimitBanError("this client is banned for the remainder of the run.")
        now = self.monotonic()
        if now < self.backoff_until:
            delay = self.backoff_until - now
            raise RateLimitAdmissionDeferredError(
                "venue backoff is active; pre-send admission is deferred.",
                retry_after_seconds=delay,
            )
        bucket_delay = self.bucket.acquire_now(weight)
        if bucket_delay > 0:
            raise RateLimitAdmissionDeferredError(
                "request-weight budget is not immediately available.",
                retry_after_seconds=bucket_delay,
            )

    def wait_for_admission(self, weight: int = 1) -> float:
        """Wait outside critical sections until a request is likely admissible; consume no token."""
        if self.banned:
            raise RateLimitBanError("this client is banned for the remainder of the run.")
        waited = 0.0
        now = self.monotonic()
        if now < self.backoff_until:
            delay = self.backoff_until - now
            self.sleep(delay)
            waited += delay
        if self.banned:
            raise RateLimitBanError("this client is banned for the remainder of the run.")
        bucket_delay = self.bucket.wait_seconds_for(weight)
        if bucket_delay > 0:
            self.sleep(bucket_delay)
            waited += bucket_delay
        return waited

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
