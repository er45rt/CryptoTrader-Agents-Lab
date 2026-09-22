"""Shadow/paper trading modes and the queue-aware configuration guard (Phase 10).

Two explicit modes, and nothing else:

* ``shadow`` - the real decision path (public market data -> AI/strategy -> validity -> risk ->
  safety) runs, but execution STOPS before order placement. What *would* have been submitted is
  recorded.
* ``paper``  - the same decision path plus the simulated execution substrate. ZERO authenticated
  exchange trading: no credential, no signed request, no venue client.

Queue-aware simulation guard: the Phase 4/Spike B evidence requires
``queue_position=True``, ``liquidity_consumption=True`` and ``book_type=L2_MBP``. Asking for
queue-aware paper simulation without that exact combination fails closed instead of silently
degrading to unrealistic fills. The Nautilus adapter re-validates the same combination through
its own guard (defence in depth).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .errors import PaperConfigError

__all__ = [
    "QUEUE_AWARE_BOOK_TYPE",
    "QueueAwareSettings",
    "TradingMode",
    "require_tested_queue_aware_settings",
    "tested_queue_aware_settings",
]

#: The only L2 book type under which queue-ahead modelling was observed to work (1.231.0).
QUEUE_AWARE_BOOK_TYPE = "L2_MBP"


class TradingMode(str, Enum):
    """The fixed Phase 10 mode vocabulary (engine-independent)."""

    SHADOW = "shadow"
    PAPER = "paper"

    @property
    def places_simulated_orders(self) -> bool:
        """Shadow never places; paper places into the simulator only."""
        return self is TradingMode.PAPER

    @property
    def uses_simulation(self) -> bool:
        return self is TradingMode.PAPER


@dataclass(frozen=True)
class QueueAwareSettings:
    """The tested queue-aware simulation configuration (mirrors the Phase 4 guard)."""

    queue_position: bool
    liquidity_consumption: bool
    book_type: str


def tested_queue_aware_settings() -> QueueAwareSettings:
    return QueueAwareSettings(
        queue_position=True,
        liquidity_consumption=True,
        book_type=QUEUE_AWARE_BOOK_TYPE,
    )


def require_tested_queue_aware_settings(settings: QueueAwareSettings) -> QueueAwareSettings:
    """Fail closed unless the configuration is the tested queue-aware combination."""
    if not isinstance(settings, QueueAwareSettings):
        raise PaperConfigError("queue-aware settings must be a QueueAwareSettings value.")
    tested = tested_queue_aware_settings()
    mismatches = []
    if settings.queue_position is not True:
        mismatches.append(f"queue_position={settings.queue_position!r} (expected True)")
    if settings.liquidity_consumption is not True:
        mismatches.append(
            f"liquidity_consumption={settings.liquidity_consumption!r} (expected True)"
        )
    if settings.book_type != tested.book_type:
        mismatches.append(f"book_type={settings.book_type!r} (expected {tested.book_type!r})")
    if mismatches:
        raise PaperConfigError(
            "Queue-aware paper simulation requires the tested configuration "
            "(queue_position=True, liquidity_consumption=True, book_type=L2_MBP); refusing to run "
            "with " + "; ".join(mismatches) + "."
        )
    return settings
