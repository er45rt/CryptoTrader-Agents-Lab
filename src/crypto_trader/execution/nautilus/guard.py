"""Version and configuration guards for the Nautilus adapter (Phase 4).

Phase 4 supports exactly one tested engine release and exactly one tested queue-aware
configuration. Anything else fails fast rather than silently degrading to unrealistic fills.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..errors import EngineVersionError, QueueAwareConfigurationError

__all__ = [
    "SUPPORTED_ENGINE_VERSION",
    "TESTED_BOOK_TYPE_NAME",
    "QueueAwareSettings",
    "installed_engine_version",
    "require_queue_aware_settings",
    "require_supported_engine_version",
    "tested_queue_aware_settings",
]

#: The only tested engine release (Spike A + Spike B evidence).
SUPPORTED_ENGINE_VERSION = "1.231.0"

#: The only L2 book type under which queue-ahead modelling was observed to work.
TESTED_BOOK_TYPE_NAME = "L2_MBP"


@dataclass(frozen=True)
class QueueAwareSettings:
    """The tested queue-aware simulation configuration."""

    queue_position: bool
    liquidity_consumption: bool
    book_type: str


def tested_queue_aware_settings() -> QueueAwareSettings:
    return QueueAwareSettings(
        queue_position=True,
        liquidity_consumption=True,
        book_type=TESTED_BOOK_TYPE_NAME,
    )


def installed_engine_version() -> str:
    """Return the installed engine version (imports the engine lazily)."""
    import nautilus_trader  # noqa: PLC0415 - lazy so the guard stays import-light

    return str(nautilus_trader.__version__)


def require_supported_engine_version(version: str | None = None) -> str:
    """Fail fast unless the installed engine is the supported, tested version."""
    actual = version if version is not None else installed_engine_version()
    if actual != SUPPORTED_ENGINE_VERSION:
        raise EngineVersionError(
            f"Unsupported NautilusTrader version {actual!r}; Phase 4 supports exactly "
            f"{SUPPORTED_ENGINE_VERSION!r}. Upgrade policy: pin, Spike/regression rerun, review."
        )
    return actual


def require_queue_aware_settings(settings: QueueAwareSettings) -> None:
    """Fail fast unless the configuration is the tested queue-aware combination.

    Omitting ``book_type=L2_MBP`` silently disables queue-ahead modelling in 1.231.0, so a
    caller that asks for queue-aware behaviour must supply all three settings.
    """
    tested = tested_queue_aware_settings()
    mismatches = []
    if settings.queue_position is not tested.queue_position:
        mismatches.append(f"queue_position={settings.queue_position!r} (expected True)")
    if settings.liquidity_consumption is not tested.liquidity_consumption:
        mismatches.append(f"liquidity_consumption={settings.liquidity_consumption!r} (expected True)")
    if settings.book_type != tested.book_type:
        mismatches.append(f"book_type={settings.book_type!r} (expected {tested.book_type!r})")
    if mismatches:
        raise QueueAwareConfigurationError(
            "Queue-aware simulation requires the tested configuration (queue_position=True, "
            "liquidity_consumption=True, book_type=L2_MBP); refusing to run with: "
            + "; ".join(mismatches)
            + ". Queue-ahead modelling is silently disabled otherwise."
        )
