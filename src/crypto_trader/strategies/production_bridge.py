"""Strategy -> ProductionRuntime bridge (D29 item 7, owner option A).

The SAME strategy and the SAME sizing code used by backtest and paper produce a decision on each closed
bar; the bridge turns a trade decision into a `ProductionIntent` and hands it to the production ingress
(`ProductionAdmissionController.offer` -> `run_once`), i.e. the unchanged production order:
    intent -> decision validity -> deterministic hard risk -> safety / writer fence -> execution port

Scope and limits (owner decision D29 item 7, option A):
  * it is tested ONLY against the engine's non-writing execution port doubles; no venue is contacted;
  * the venue is whatever ExecutionPort the caller wired into ProductionRuntime. Mainnet is impossible:
    `production.environment.MAINNET_ENABLED` is a source constant set to False, and no mainnet
    order-submission client exists. Nothing here changes that;
  * the caller supplies the position snapshot (a production deployment would read it from the
    reconciled account state); orders are MARKET, spot, long only.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Callable, Sequence
from uuid import UUID, uuid5

from crypto_trader.execution.ports import ExecutionOrderType, ExecutionSide, ExecutionTimeInForce
from crypto_trader.paper import MarketObservation
from crypto_trader.production import ProductionAdmissionController
from crypto_trader.production.runtime import ProductionIntent

from .api import Bar, Strategy, StrategyError
from .runtime import MarketSpec, RunSettings, StrategyDecisionSource, _ref

__all__ = ["ProductionStrategyDriver", "BridgeResult"]


@dataclass
class BridgeResult:
    bar_open_time: str
    action: str
    rationale: str
    intent_id: str | None = None
    offered: tuple[bool, str] | None = None
    submitted: bool | None = None
    blocked_reason_code: str | None = None
    outcome: dict[str, Any] | None = field(default=None, repr=False)


class ProductionStrategyDriver:
    """Feeds closed bars to a Strategy and routes its trade decisions into production admission."""

    def __init__(self, admission: ProductionAdmissionController, strategy: Strategy, *, session_id: UUID,
                 strategy_id: str, market: MarketSpec, settings: RunSettings,
                 position: Callable[[MarketObservation], Any],
                 time_in_force: ExecutionTimeInForce = ExecutionTimeInForce.IOC) -> None:
        if not isinstance(admission, ProductionAdmissionController):
            raise StrategyError("the bridge only accepts the production admission controller as ingress.")
        self.admission = admission
        self.strategy = strategy
        self.session_id = session_id
        self.strategy_id = strategy_id
        self.market = market
        self.settings = settings
        self.position = position
        self.time_in_force = time_in_force
        self._bars: list[Bar] = []
        self._source: StrategyDecisionSource | None = None
        self.results: list[BridgeResult] = []

    def on_closed_bars(self, bars: Sequence[Bar], *, decided_at: datetime | None = None) -> BridgeResult | None:
        """Give the driver the full closed-bar history; it decides once per NEW newest bar."""
        if not bars:
            return None
        if self._bars and bars[-1].open_time <= self._bars[-1].open_time:
            return None                       # nothing new: never decide twice on the same bar
        if self._bars and tuple(bars[: len(self._bars)]) != tuple(self._bars):
            raise StrategyError("bar history changed under the driver (rewritten past bars).")
        self._bars = list(bars)
        previous = self._source
        self._source = StrategyDecisionSource(self.strategy, self._bars, market=self.market,
                                              settings=self.settings, position=self.position)
        if previous is not None:                # carry the sizing state across bars
            self._source._last_target = previous._last_target
        i = len(self._bars) - 1
        bar = self._bars[i]
        observation = MarketObservation(symbol=self.market.symbol, price=bar.close, observed_at=bar.close_time,
                                        market_reference=_ref(i, bar), volume=bar.volume)
        decision = self._source.decide(observation)
        result = BridgeResult(bar_open_time=bar.open_time.isoformat(), action=decision.action,
                              rationale=decision.rationale)
        if decision.action in {"buy", "sell"}:
            completed = decided_at or observation.observed_at
            ref = observation.market_reference
            intent = ProductionIntent(
                correlation_id=uuid5(self.session_id, f"corr:{ref}"),
                symbol=self.market.symbol,
                side=ExecutionSide.BUY if decision.action == "buy" else ExecutionSide.SELL,
                order_type=ExecutionOrderType.MARKET,
                quantity=decision.quantity,
                strategy_id=self.strategy_id,
                decision_started_at=observation.observed_at,
                decision_completed_at=completed,
                market_event_ts=observation.observed_at,
                reference_price=observation.price,
                market_reference=ref,
                intent_id=uuid5(self.session_id, f"intent:{ref}"),
                time_in_force=self.time_in_force,
            )
            result.intent_id = str(intent.intent_id)
            result.offered = self.admission.offer(intent)
            if result.offered[0]:
                outcome = self.admission.run_once()
                if outcome is not None:
                    result.submitted = outcome.submitted
                    result.blocked_reason_code = outcome.blocked_reason_code
                    result.outcome = outcome.to_record()
        self.results.append(result)
        return result
