"""Strategy/decision source for shadow/paper sessions (Phase 10).

The AI/strategy layer is INJECTED, never embedded: the session calls a `DecisionSource` outside
the execution substrate, which keeps model inference off any engine event-processing thread.

The Agent/Strategist schema draft remains locked and out of scope; `StrategyDecision` here is the
minimal engine-independent shape the paper session needs (action, confidence, rationale, size).
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Protocol, runtime_checkable

from .errors import PaperConfigError, PaperStateError
from .feed import MarketObservation

__all__ = [
    "DecisionSource",
    "ScriptedDecisionSequence",
    "ScriptedDecisionSource",
    "StrategyDecision",
]

_ACTION_LEVELS = {"buy", "sell", "hold"}
_CONFIDENCES = {"low", "medium", "high"}
_SYMBOL_PATTERN = re.compile(r"^[A-Z0-9]{5,20}$")


@dataclass(frozen=True)
class StrategyDecision:
    """A decision produced by the injected AI/strategy layer."""

    action: str
    confidence: str
    rationale: str
    quantity: Decimal = Decimal("0")
    order_type: str = "limit"
    limit_price: Decimal | None = None
    decided_at: datetime | None = None
    decision_tolerance_bps: Decimal | None = None

    def __post_init__(self) -> None:
        if self.action not in _ACTION_LEVELS:
            raise PaperStateError(f"unknown decision action {self.action!r}.")
        if self.confidence not in _CONFIDENCES:
            raise PaperStateError(f"unknown decision confidence {self.confidence!r}.")
        if not self.rationale:
            raise PaperStateError("a decision requires a rationale.")
        if self.action == "hold":
            return
        if not isinstance(self.quantity, Decimal) or self.quantity <= 0:
            raise PaperStateError("a trade decision requires a positive Decimal quantity.")
        if self.order_type not in {"market", "limit"}:
            raise PaperStateError(f"unknown order type {self.order_type!r}.")
        if self.order_type == "limit":
            if not isinstance(self.limit_price, Decimal) or self.limit_price <= 0:
                raise PaperStateError("limit decisions require a positive Decimal limit_price.")
        elif self.limit_price is not None:
            raise PaperStateError("market decisions must not carry a limit_price.")


@runtime_checkable
class DecisionSource(Protocol):
    """The injected AI/strategy decision layer."""

    def decide(self, observation: MarketObservation) -> StrategyDecision: ...


@dataclass(frozen=True)
class ScriptedDecisionSource:
    """A deterministic decision source keyed by market reference (offline/replay use)."""

    script: Mapping[str, StrategyDecision]
    default: StrategyDecision | None = None

    def decide(self, observation: MarketObservation) -> StrategyDecision:
        decision = self.script.get(observation.market_reference, self.default)
        if decision is None:
            raise PaperConfigError(
                f"no scripted decision for market reference {observation.market_reference!r}."
            )
        return decision


@dataclass
class ScriptedDecisionSequence:
    """A deterministic decision source that walks a fixed sequence in order."""

    decisions: Sequence[StrategyDecision]

    def __post_init__(self) -> None:
        self._index = 0

    def decide(self, observation: MarketObservation) -> StrategyDecision:
        del observation
        if self._index >= len(self.decisions):
            raise PaperConfigError("scripted decision sequence exhausted.")
        decision = self.decisions[self._index]
        self._index += 1
        return decision
