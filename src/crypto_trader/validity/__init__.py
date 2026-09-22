"""Deterministic decision validity / staleness gate (Phase 6).

Engine-independent: this package never imports Nautilus, CrewAI, or the LLM layer. Freshness is
measured from the ORIGINAL market observation and never resets when a model finishes.
"""

from .errors import MarketStateError, ValidityConfigError, ValidityError
from .gate import DecisionValidityGate, Disposition, ValidityDecision, ValidityRequest, to_phase3_event
from .journaling import record_validity_decision
from .market_state import (
    LiveMarketStateProvider,
    MarketObservation,
    MarketStateProvider,
    ReplayMarketStateProvider,
)
from .pending import BoundedPendingIntents, PendingIntent
from .policy import ValidityPolicy, load_policy

__all__ = [
    "BoundedPendingIntents",
    "DecisionValidityGate",
    "Disposition",
    "LiveMarketStateProvider",
    "MarketObservation",
    "MarketStateError",
    "MarketStateProvider",
    "PendingIntent",
    "ReplayMarketStateProvider",
    "ValidityConfigError",
    "ValidityDecision",
    "ValidityError",
    "ValidityPolicy",
    "ValidityRequest",
    "load_policy",
    "record_validity_decision",
    "to_phase3_event",
]
