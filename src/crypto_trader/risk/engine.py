"""Deterministic Hard Risk engine (Phase 7).

Non-LLM by construction: the engine evaluates typed deterministic inputs only. The LLM never
approves its own risk.

Unit policy (explicit, no implicit conversions):
* notional/exposure rules compare quote-currency Decimals (``quantity * price``).
* percentage rules carry unit ``percent`` (0-100).
* count rules carry unit ``count``.

A loss-limit or drawdown breach only HALT NEW RISK and request the deterministic
``halt_new_risk`` action. The engine never liquidates or unwinds: unwind policy belongs to the
safety phase.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
import math
from typing import Any
from uuid import UUID, uuid4

from crypto_trader.events import RiskEvaluationPayload, RiskEvaluationRecorded, RiskRuleResultPayload

from .errors import RiskConfigError, RiskStateError
from .limits import RiskLimits
from .state import PendingIntent, PendingIntentRegistry, RiskStateSnapshot, Side

__all__ = ["HardRiskEngine", "RiskDecision", "RiskRequest", "RiskRuleResult"]

ZERO = Decimal("0")
HUNDRED = Decimal("100")


@dataclass(frozen=True)
class RiskRequest:
    """The typed deterministic inputs for one intent."""

    correlation_id: UUID
    symbol: str
    side: Side
    quantity: Decimal
    price: Decimal
    strategy_id: str
    intent_id: UUID = field(default_factory=uuid4)
    order_type: str = "market"

    def __post_init__(self) -> None:
        if (
            not isinstance(self.quantity, Decimal)
            or not self.quantity.is_finite()
            or self.quantity <= 0
        ):
            raise RiskStateError("quantity must be a finite positive Decimal.")
        if (
            not isinstance(self.price, Decimal)
            or not self.price.is_finite()
            or self.price <= 0
        ):
            raise RiskStateError("price must be a finite positive Decimal.")
        if not isinstance(self.side, Side):
            raise RiskStateError("side must be a Side enum value.")
        if not self.symbol or not self.strategy_id:
            raise RiskStateError("symbol and strategy_id are required.")

    @property
    def signed_notional(self) -> Decimal:
        """Positive for buys, negative for sells (quote currency)."""
        notional = self.quantity * self.price
        return notional if self.side is Side.BUY else -notional

    @property
    def notional(self) -> Decimal:
        return self.quantity * self.price


@dataclass(frozen=True)
class RiskRuleResult:
    rule_id: str
    allowed: bool
    reason_code: str
    actual: Decimal
    limit: Decimal
    unit: str


@dataclass(frozen=True)
class RiskDecision:
    allowed: bool
    reason_codes: list[str]
    intent_id: UUID
    correlation_id: UUID
    symbol: str
    side: Side
    quantity: Decimal
    price: Decimal
    notional: Decimal
    limits_version: str
    rule_results: list[RiskRuleResult]
    approved_quantity: Decimal | None
    risk_actions: list[str] = field(default_factory=list)
    reduce_only_mode: bool = False


class HardRiskEngine:
    """Deterministic, configuration-driven Hard Risk."""

    def __init__(
        self,
        limits: RiskLimits,
        *,
        pending: PendingIntentRegistry | None = None,
        max_state_age_seconds: float | None = None,
    ) -> None:
        if not isinstance(limits, RiskLimits):
            raise RiskConfigError("a resolved RiskLimits rule set is required (fail closed).")
        if max_state_age_seconds is not None and (
            isinstance(max_state_age_seconds, bool)
            or not isinstance(max_state_age_seconds, (int, float))
            or not math.isfinite(float(max_state_age_seconds))
            or max_state_age_seconds <= 0
        ):
            raise RiskConfigError(
                "max_state_age_seconds must be a finite number > 0 when configured."
            )
        self._limits = limits
        self._pending = pending if pending is not None else PendingIntentRegistry()
        self._max_state_age_seconds = max_state_age_seconds

    @property
    def limits(self) -> RiskLimits:
        return self._limits

    @property
    def pending(self) -> PendingIntentRegistry:
        return self._pending

    def evaluate(
        self,
        request: RiskRequest,
        state: RiskStateSnapshot,
        *,
        now: datetime | None = None,
    ) -> RiskDecision:
        """Evaluate and, when allowed, admit the intent into the pending registry.

        This is intentionally an admission operation rather than a side-effect-free query:
        a successful evaluation reserves the intent identity so at-least-once redelivery of the
        same intent is rejected as "duplicate_intent" and an opposite pending intent is rejected
        as "conflicting_intent". Callers needing an independent what-if calculation must use an
        independent registry instead of evaluating the same live admission state twice.
        """
        limits = self._limits
        results: list[RiskRuleResult] = []

        def rule(rule_id: str, allowed: bool, reason_code: str, actual: Decimal, limit: Decimal, unit: str) -> None:
            results.append(RiskRuleResult(rule_id, allowed, reason_code, actual, limit, unit))

        # 0. Stale risk-state input rejection (fail closed when enabled and the state is too old,
        # or implausibly timestamped in the future).
        if self._max_state_age_seconds is not None:
            if now is None or state.snapshot_ts is None:
                rule("risk_state_freshness", False, "risk_state_timestamp_unavailable", Decimal(1), ZERO, "count")
            else:
                age = (now - state.snapshot_ts).total_seconds()
                if age < 0:
                    rule(
                        "risk_state_freshness",
                        False,
                        "risk_state_timestamp_in_future",
                        Decimal(str(round(age, 6))),
                        Decimal(str(self._max_state_age_seconds)),
                        "count",
                    )
                else:
                    stale = age > self._max_state_age_seconds
                    rule(
                        "risk_state_freshness",
                        not stale,
                        "stale_risk_state" if stale else "risk_state_fresh",
                        Decimal(str(round(age, 6))),
                        Decimal(str(self._max_state_age_seconds)),
                        "count",
                    )

        # 1. Trading halt state.
        rule(
            "trading_halt",
            not limits.trading_halted,
            "trading_halted" if limits.trading_halted else "trading_active",
            Decimal(1) if limits.trading_halted else ZERO,
            ZERO,
            "count",
        )

        # 2. Duplicate intent protection.
        duplicate = self._pending.has_intent(request.intent_id)
        rule("duplicate_intent", not duplicate, "duplicate_intent" if duplicate else "intent_unique",
             Decimal(1) if duplicate else ZERO, ZERO, "count")

        # 3. Conflicting intent protection (opposite side for the same symbol+strategy pending).
        conflict = self._pending.conflicting(symbol=request.symbol, strategy_id=request.strategy_id, side=request.side)
        rule("conflicting_intent", conflict is None, "conflicting_intent" if conflict else "no_conflict",
             Decimal(1) if conflict else ZERO, ZERO, "count")

        # 4. Reduce-only behaviour (when active, only exposure-reducing orders are allowed).
        if limits.reduce_only_mode:
            position = state.position(request.symbol)
            reduces = (position > 0 and request.side is Side.SELL) or (position < 0 and request.side is Side.BUY)
            rule("reduce_only", reduces, "reduce_only_violation" if not reduces else "reduce_only_ok",
                 ZERO if reduces else Decimal(1), ZERO, "count")

        # 5/6. Open orders and positions.
        rule("max_open_orders", state.open_orders < limits.max_open_orders,
             "max_open_orders_exceeded" if state.open_orders >= limits.max_open_orders else "open_orders_ok",
             Decimal(state.open_orders), Decimal(limits.max_open_orders), "count")
        rule("max_open_positions", state.open_positions < limits.max_open_positions,
             "max_open_positions_exceeded" if state.open_positions >= limits.max_open_positions else "open_positions_ok",
             Decimal(state.open_positions), Decimal(limits.max_open_positions), "count")

        # 7. Order notional (quote currency).
        notional = request.notional
        rule("max_order_notional", notional <= limits.max_order_notional_quote,
             "max_order_notional_exceeded" if notional > limits.max_order_notional_quote else "order_notional_ok",
             notional, limits.max_order_notional_quote, "quote_currency")

        # 8/9. Projected symbol and strategy exposure (quote currency).
        projected_symbol = abs(state.symbol_exposure(request.symbol) + request.signed_notional)
        rule("max_symbol_exposure", projected_symbol <= limits.max_symbol_exposure_quote,
             "max_symbol_exposure_exceeded" if projected_symbol > limits.max_symbol_exposure_quote else "symbol_exposure_ok",
             projected_symbol, limits.max_symbol_exposure_quote, "quote_currency")
        projected_strategy = abs(state.strategy_exposure(request.strategy_id) + request.signed_notional)
        rule("max_strategy_exposure", projected_strategy <= limits.max_strategy_exposure_quote,
             "max_strategy_exposure_exceeded" if projected_strategy > limits.max_strategy_exposure_quote else "strategy_exposure_ok",
             projected_strategy, limits.max_strategy_exposure_quote, "quote_currency")

        # 10/11. Gross and net exposure (conservative deterministic projections).
        projected_gross = state.gross_exposure_quote + notional
        rule("max_gross_exposure", projected_gross <= limits.max_gross_exposure_quote,
             "max_gross_exposure_exceeded" if projected_gross > limits.max_gross_exposure_quote else "gross_exposure_ok",
             projected_gross, limits.max_gross_exposure_quote, "quote_currency")
        projected_net = abs(state.net_exposure_quote + request.signed_notional)
        rule("max_net_exposure", projected_net <= limits.max_net_exposure_quote,
             "max_net_exposure_exceeded" if projected_net > limits.max_net_exposure_quote else "net_exposure_ok",
             projected_net, limits.max_net_exposure_quote, "quote_currency")

        # 12. Loss limit (quote currency; loss expressed as a positive magnitude).
        loss = max(ZERO, -state.realized_pnl_quote)
        rule("loss_limit", loss < limits.loss_limit_quote,
             "loss_limit_breached" if loss >= limits.loss_limit_quote else "loss_within_limit",
             loss, limits.loss_limit_quote, "quote_currency")

        # 13. Drawdown threshold (percent).
        if state.peak_equity_quote > 0:
            drawdown_pct = ((state.peak_equity_quote - state.equity_quote) / state.peak_equity_quote) * HUNDRED
        else:
            drawdown_pct = ZERO
        rule("max_drawdown", drawdown_pct < limits.max_drawdown_pct,
             "max_drawdown_exceeded" if drawdown_pct >= limits.max_drawdown_pct else "drawdown_ok",
             drawdown_pct, limits.max_drawdown_pct, "percent")

        # 14. Concentration limit (percent of equity).
        if state.equity_quote > 0:
            concentration_pct = (projected_symbol / state.equity_quote) * HUNDRED
        else:
            concentration_pct = HUNDRED if projected_symbol > 0 else ZERO
        rule("concentration", concentration_pct <= limits.concentration_limit_pct,
             "concentration_exceeded" if concentration_pct > limits.concentration_limit_pct else "concentration_ok",
             concentration_pct, limits.concentration_limit_pct, "percent")

        violations = [result for result in results if not result.allowed]
        allowed = not violations
        reason_codes = [result.reason_code for result in violations] or ["all_rules_passed"]

        risk_actions: list[str] = []
        if any(result.rule_id in {"loss_limit", "max_drawdown"} for result in violations):
            # Halt new risk only. Never a liquidation instruction.
            risk_actions.append("halt_new_risk")

        if allowed:
            self._pending.register(
                PendingIntent(
                    intent_id=request.intent_id,
                    symbol=request.symbol,
                    strategy_id=request.strategy_id,
                    side=request.side,
                )
            )

        return RiskDecision(
            allowed=allowed,
            reason_codes=reason_codes,
            intent_id=request.intent_id,
            correlation_id=request.correlation_id,
            symbol=request.symbol,
            side=request.side,
            quantity=request.quantity,
            price=request.price,
            notional=notional,
            limits_version=limits.limits_version,
            rule_results=results,
            approved_quantity=request.quantity if allowed else None,
            risk_actions=risk_actions,
            reduce_only_mode=limits.reduce_only_mode,
        )

    @staticmethod
    def to_phase3_evaluation(
        decision: RiskDecision,
        *,
        occurred_at: datetime,
        event_id: UUID | None = None,
    ) -> RiskEvaluationRecorded:
        """Translate the decision into the additive ``risk.evaluation.recorded`` contract.

        ``occurred_at`` is supplied by the caller so the engine itself never reads a clock and the
        recorded evaluation stays deterministic and replayable.
        """
        if occurred_at.tzinfo is None or occurred_at.utcoffset() is None:
            raise RiskConfigError("risk evaluation occurred_at must be timezone-aware.")
        return RiskEvaluationRecorded(
            event_id=event_id or uuid4(),
            occurred_at=occurred_at.astimezone(timezone.utc),
            correlation_id=decision.correlation_id,
            producer="application.risk",
            symbol=decision.symbol,
            payload=RiskEvaluationPayload(
                limits_version=decision.limits_version,
                intent_id=decision.intent_id,
                symbol=decision.symbol,
                side=decision.side.value,
                quantity=decision.quantity,
                price=decision.price,
                notional=decision.notional,
                decision="allowed" if decision.allowed else "rejected",
                reason_codes=list(decision.reason_codes),
                rule_results=[
                    RiskRuleResultPayload(
                        rule_id=result.rule_id,
                        allowed=result.allowed,
                        reason_code=result.reason_code,
                        actual=result.actual,
                        limit=result.limit,
                        unit=result.unit,
                    )
                    for result in decision.rule_results
                ],
                reduce_only_mode=decision.reduce_only_mode,
                risk_actions=list(decision.risk_actions),
            ),
        )
