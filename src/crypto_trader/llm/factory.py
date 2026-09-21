"""LLM factory: builds provider-agnostic, retry-wrapped LLM clients.

The factory is the single entry point agents use to obtain an LLM. It owns:

* profile resolution and credential validation (see :mod:`crypto_trader.llm.config`)
* a centralized timeout policy (from the profile)
* bounded retry with exponential backoff for transient provider failures
* provider-level rate limiting shared by every LLM of the same provider
* typed errors (:class:`LLMProviderError`, :class:`LLMConfigError`)

Dependency injection
--------------------
``get_llm(profile, builder=...)`` accepts a builder callable, and
``set_default_builder`` swaps the module default, so tests can substitute a fake
LLM without any provider-specific branching inside agents. ``reset_factory_state``
restores defaults and clears shared rate limiters between tests.

Statefulness
------------
The only state shared between agents is the provider rate limiter, which holds
nothing but throttle bookkeeping. No conversation, message, or response state is
shared: each call keeps its own locals, and ``last_call_metadata`` is per instance.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from dataclasses import replace
from typing import Any, Callable, Mapping

from crewai import LLM
from crewai.llms.base_llm import BaseLLM

from .config import LLMConfigError, LLMProfile, load_profile
from .console import (
    ascii_safe,
    is_console_encoding_error,
    recover_original_error,
    tolerant_console_writes,
)

__all__ = [
    "LLMProviderError",
    "ProviderRateLimiter",
    "ResilientLLM",
    "build_resilient_llm",
    "compute_backoff_delay",
    "get_llm",
    "get_provider_rate_limiter",
    "is_transient_provider_error",
    "reset_factory_state",
    "set_default_builder",
]

logger = logging.getLogger("crypto_trader.llm")

# HTTP-style statuses that are worth retrying (throttling and server-side faults).
TRANSIENT_STATUS_CODES = {408, 409, 425, 429, 500, 502, 503, 504}

# Exception class names that must never be retried: retrying cannot help and the
# failure is deterministic (bad key, bad request, unknown model, oversized prompt).
NON_TRANSIENT_EXCEPTION_NAMES = {
    "AuthenticationError",
    "PermissionDeniedError",
    "BadRequestError",
    "NotFoundError",
    "UnprocessableEntityError",
    "InvalidRequestError",
    "ContextWindowExceededError",
    "ContentPolicyViolationError",
}

# Exception class names (LiteLLM/OpenAI SDK) that indicate a transient fault.
TRANSIENT_EXCEPTION_NAMES = {
    "Timeout",
    "APITimeoutError",
    "APIConnectionError",
    "RateLimitError",
    "ServiceUnavailableError",
    "InternalServerError",
    "APIError",
    "ConnectError",
    "ReadTimeout",
}


class LLMProviderError(RuntimeError):
    """Typed failure raised when a provider call fails after the retry budget.

    Attributes:
        provider: Provider name from the profile (e.g. ``"deepseek"``).
        model: Model identifier from the profile.
        attempts: Number of attempts actually made (1 == no retry happened).
        transient: Whether the last failure was classified as transient.
        cause_type: Exception class name of the underlying cause, for triage.
    """

    def __init__(
        self,
        message: str,
        *,
        provider: str,
        model: str,
        attempts: int,
        transient: bool,
        cause_type: str | None = None,
    ) -> None:
        super().__init__(message)
        self.provider = provider
        self.model = model
        self.attempts = attempts
        self.transient = transient
        self.cause_type = cause_type


def is_transient_provider_error(exc: BaseException) -> bool:
    """Classify an exception as retryable (transient) or not.

    Classification is intentionally conservative: unknown exception types are
    treated as non-transient so programming errors surface immediately instead of
    being retried.
    """
    name = type(exc).__name__
    if name in NON_TRANSIENT_EXCEPTION_NAMES:
        return False

    status = getattr(exc, "status_code", None) or getattr(exc, "http_status", None)
    if isinstance(status, int):
        if status in TRANSIENT_STATUS_CODES:
            return True
        if 400 <= status < 500:
            return False

    if name in TRANSIENT_EXCEPTION_NAMES:
        return True
    if isinstance(exc, (TimeoutError, ConnectionError)):
        return True
    # OSError covers socket-level failures (DNS, reset, unreachable) that surface
    # as raw OS errors rather than provider SDK errors.
    if isinstance(exc, OSError):
        return True
    return False


def compute_backoff_delay(attempt: int, base_seconds: float, max_seconds: float) -> float:
    """Exponential backoff for the given 1-based attempt number, clamped to a cap."""
    if attempt < 1:
        attempt = 1
    return min(base_seconds * (2 ** (attempt - 1)), max_seconds)


class ProviderRateLimiter:
    """Provider-level throttle shared by every LLM instance of one provider.

    Enforces an optional minimum spacing between requests and an optional rolling
    requests-per-minute cap. Thread-safe; holds no conversation state.
    """

    def __init__(
        self,
        *,
        min_seconds_between_requests: float = 0.0,
        requests_per_minute: int | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self._min_interval = max(0.0, float(min_seconds_between_requests))
        self._max_rpm = requests_per_minute if (requests_per_minute or 0) > 0 else None
        self._clock = clock
        self._sleeper = sleeper
        self._lock = threading.Lock()
        self._last_request_at: float | None = None
        self._window: deque[float] = deque()

    @property
    def min_seconds_between_requests(self) -> float:
        return self._min_interval

    @property
    def requests_per_minute(self) -> int | None:
        return self._max_rpm

    def _required_wait(self, now: float) -> float:
        wait = 0.0
        if self._min_interval and self._last_request_at is not None:
            wait = max(wait, self._min_interval - (now - self._last_request_at))
        if self._max_rpm:
            cutoff = now - 60.0
            while self._window and self._window[0] <= cutoff:
                self._window.popleft()
            if len(self._window) >= self._max_rpm:
                wait = max(wait, 60.0 - (now - self._window[0]))
        return max(0.0, wait)

    def acquire(self) -> float:
        """Block until a request is permitted. Returns the total seconds slept."""
        slept = 0.0
        # Loop because the wait may be recomputed after sleeping.
        while True:
            with self._lock:
                now = self._clock()
                wait = self._required_wait(now)
                if wait <= 0:
                    self._last_request_at = now
                    if self._max_rpm:
                        self._window.append(now)
                    return slept
            self._sleeper(wait)
            slept += wait


_LIMITERS: dict[tuple[str, float, int | None], ProviderRateLimiter] = {}
_LIMITERS_LOCK = threading.Lock()


def get_provider_rate_limiter(profile: LLMProfile) -> ProviderRateLimiter:
    """Return the shared rate limiter for a provider configuration.

    Keyed by provider *and* its throttle policy, so all agents using the same
    provider share one limiter while differently configured providers do not.
    """
    key = (profile.provider, profile.min_seconds_between_requests, profile.requests_per_minute)
    with _LIMITERS_LOCK:
        limiter = _LIMITERS.get(key)
        if limiter is None:
            limiter = ProviderRateLimiter(
                min_seconds_between_requests=profile.min_seconds_between_requests,
                requests_per_minute=profile.requests_per_minute,
            )
            _LIMITERS[key] = limiter
        return limiter


class ResilientLLM(LLM):
    """CrewAI ``LLM`` with provider-level throttling and bounded retry.

    Provider specifics stay here (and in the profile), never in the agents.
    """

    def __init__(
        self,
        *,
        profile: LLMProfile,
        api_key: str,
        rate_limiter: ProviderRateLimiter | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        **kwargs: Any,
    ) -> None:
        super().__init__(
            model=profile.model,
            timeout=profile.timeout_seconds,
            temperature=profile.temperature,
            max_tokens=profile.max_tokens,
            base_url=profile.base_url,
            api_key=api_key,
            **kwargs,
        )
        self.profile = profile
        self._rate_limiter = rate_limiter or get_provider_rate_limiter(profile)
        self._sleeper = sleeper
        self._clock = clock
        # Per-instance observability; never shared between agents.
        self.last_call_metadata: dict[str, Any] = {}

    @property
    def rate_limiter(self) -> ProviderRateLimiter:
        return self._rate_limiter

    def call(
        self,
        messages: str | list[dict[str, str]],
        tools: list[dict] | None = None,
        callbacks: list[Any] | None = None,
        available_functions: dict[str, Any] | None = None,
        from_task: Any | None = None,
        from_agent: Any | None = None,
    ) -> str | Any:
        """Call the provider with throttling, bounded retry, and typed failures."""
        throttled_seconds = self._rate_limiter.acquire()
        started_at = self._clock()
        attempts = 0
        last_exc: BaseException | None = None

        while attempts < self.profile.max_attempts:
            attempts += 1
            try:
                # CrewAI's console formatter prints a panel containing non-ASCII
                # characters while reporting failures. On a strict, non-UTF-8
                # console (cp1252 on Windows) that print raises UnicodeEncodeError
                # and can mask the provider error. Tolerate unencodable output for
                # the duration of the call only; the previous handler is restored.
                with tolerant_console_writes():
                    result = super().call(
                        messages,
                        tools=tools,
                        callbacks=callbacks,
                        available_functions=available_functions,
                        from_task=from_task,
                        from_agent=from_agent,
                    )
            except Exception as exc:  # noqa: BLE001 - re-raised as a typed error below
                # An output-encoding failure must never be reported as the provider
                # failure. Recover the original provider error and report that,
                # keeping the encoding error in the exception chain as context.
                effective_error: BaseException = exc
                reporting_failure = False
                if is_console_encoding_error(exc):
                    reporting_failure = True
                    original = recover_original_error(exc)
                    if isinstance(original, BaseException):
                        logger.warning(
                            ascii_safe(
                                "Console output-encoding failure (%s) while reporting an LLM "
                                "provider error; surfacing the original provider error (%s) instead."
                            )
                            % (type(exc).__name__, type(original).__name__)
                        )
                        effective_error = original
                    else:
                        logger.warning(
                            ascii_safe(
                                "Console output-encoding failure (%s) during an LLM call and no "
                                "original provider error was attached; reporting the encoding failure."
                            )
                            % type(exc).__name__
                        )

                last_exc = effective_error
                transient = is_transient_provider_error(effective_error)
                if not transient or attempts >= self.profile.max_attempts:
                    self.last_call_metadata = self._build_metadata(
                        attempts=attempts,
                        started_at=started_at,
                        throttled_seconds=throttled_seconds,
                        success=False,
                        reporting_failure=reporting_failure,
                    )
                    raise LLMProviderError(
                        ascii_safe(
                            f"LLM provider '{self.profile.provider}' call failed for model "
                            f"'{self.profile.model}' after {attempts} attempt(s) "
                            f"(transient={transient}, cause={type(effective_error).__name__})."
                        ),
                        provider=self.profile.provider,
                        model=self.profile.model,
                        attempts=attempts,
                        transient=transient,
                        cause_type=type(effective_error).__name__,
                    ) from effective_error

                delay = compute_backoff_delay(
                    attempts,
                    self.profile.backoff_base_seconds,
                    self.profile.backoff_max_seconds,
                )
                logger.warning(
                    "Transient LLM failure (provider=%s, model=%s, attempt=%d/%d, cause=%s); "
                    "retrying in %.2fs.",
                    self.profile.provider,
                    self.profile.model,
                    attempts,
                    self.profile.max_attempts,
                    type(effective_error).__name__,
                    delay,
                )
                self._sleeper(delay)
                continue

            self.last_call_metadata = self._build_metadata(
                attempts=attempts,
                started_at=started_at,
                throttled_seconds=throttled_seconds,
                success=True,
            )
            return result

        # Defensive: the loop always returns or raises.
        raise LLMProviderError(
            f"LLM provider '{self.profile.provider}' call failed without a result.",
            provider=self.profile.provider,
            model=self.profile.model,
            attempts=attempts,
            transient=True,
            cause_type=type(last_exc).__name__ if last_exc else None,
        )

    def _build_metadata(
        self,
        *,
        attempts: int,
        started_at: float,
        throttled_seconds: float,
        success: bool,
        reporting_failure: bool = False,
    ) -> dict[str, Any]:
        return {
            "provider": self.profile.provider,
            "model": self.profile.model,
            "profile": self.profile.name,
            "attempts": attempts,
            "retries": attempts - 1,
            "success": success,
            "reporting_failure": reporting_failure,
            "latency_ms": round((self._clock() - started_at) * 1000.0, 3),
            "throttled_ms": round(throttled_seconds * 1000.0, 3),
            "timeout_seconds": self.profile.timeout_seconds,
        }


def build_resilient_llm(profile: LLMProfile, api_key: str) -> ResilientLLM:
    """Default builder: construct a :class:`ResilientLLM` for a resolved profile."""
    return ResilientLLM(profile=profile, api_key=api_key)


_default_builder: Callable[[LLMProfile, str], BaseLLM] = build_resilient_llm


def set_default_builder(builder: Callable[[LLMProfile, str], BaseLLM] | None) -> None:
    """Override (or with ``None`` restore) the module-level LLM builder."""
    global _default_builder
    _default_builder = builder or build_resilient_llm


def reset_factory_state() -> None:
    """Restore the default builder and drop shared rate limiters (test helper)."""
    global _default_builder
    _default_builder = build_resilient_llm
    with _LIMITERS_LOCK:
        _LIMITERS.clear()


def get_llm(
    profile: str | LLMProfile,
    *,
    env: Mapping[str, str] | None = None,
    builder: Callable[[LLMProfile, str], BaseLLM] | None = None,
    overrides: Mapping[str, Any] | None = None,
) -> BaseLLM:
    """Return an LLM client for a named profile.

    Args:
        profile: Profile name (e.g. ``"deepseek_canary"``) or a resolved
            :class:`LLMProfile`.
        env: Optional environment mapping used to resolve the profile and read the
            API key. Defaults to ``os.environ``.
        builder: Optional builder callable ``(profile, api_key) -> BaseLLM``. This is
            the dependency-injection seam: tests pass a fake builder and never touch
            a provider.
        overrides: Optional dataclass field overrides applied to the resolved profile
            (for example ``{"max_tokens": 400}``).

    Raises:
        LLMConfigError: If the profile is unknown, an override is invalid, or required
            configuration (such as the provider API key) is missing.
    """
    resolved = profile if isinstance(profile, LLMProfile) else load_profile(profile, env=env)
    if overrides:
        try:
            resolved = replace(resolved, **dict(overrides))
        except TypeError as exc:
            raise LLMConfigError(
                f"Invalid LLM profile override for '{resolved.name}': {exc}"
            ) from exc

    api_key = resolved.resolve_api_key(env)
    build = builder or _default_builder

    try:
        llm = build(resolved, api_key)
    except LLMConfigError:
        raise
    except Exception as exc:  # pragma: no cover - defensive
        raise LLMConfigError(
            f"Failed to build LLM for profile '{resolved.name}' "
            f"(provider '{resolved.provider}'): {type(exc).__name__}."
        ) from exc

    if not isinstance(llm, BaseLLM):
        raise LLMConfigError(
            f"LLM builder returned {type(llm).__name__}, which is not a CrewAI BaseLLM."
        )

    logger.debug(
        "LLM ready (profile=%s, provider=%s, model=%s, timeout=%ss, max_attempts=%s).",
        resolved.name,
        resolved.provider,
        resolved.model,
        resolved.timeout_seconds,
        resolved.max_attempts,
    )
    return llm
