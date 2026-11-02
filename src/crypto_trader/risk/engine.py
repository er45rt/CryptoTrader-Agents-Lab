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
from typing import Any
from uuid import UUID, uuid4

from crypto_trader.events import (
    RiskEvaluationPayloadV2, RiskEvaluationRecordedV2, RiskRuleResultPayloadV2,
)

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
        if not isinstance(self.quantity, Decimal) or self.quantity <= 0:
            raise RiskStateError("quantity must be a positive Decimal.")
        if not isinstance(self.price, Decimal) or self.price <= 0:
            raise RiskStateError("price must be a positive Decimal.")
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
        include_pending_reservations: bool = False,
        enforce_attributed_inventory: bool = False,
    ) -> None:
        if not isinstance(limits, RiskLimits):
            raise RiskConfigError("a resolved RiskLimits rule set is required (fail closed).")
        if max_state_age_seconds is not None and max_state_age_seconds <= 0:
            raise RiskConfigError("max_state_age_seconds must be > 0 when configured.")
        if type(include_pending_reservations) is not bool or type(enforce_attributed_inventory) is not bool:
            raise RiskConfigError("pending reservation and attributed inventory modes must be booleans.")
        self._limits = limits
        self._pending = pending if pending is not None else PendingIntentRegistry()
        self._max_state_age_seconds = max_state_age_seconds
        self._include_pending_reservations = include_pending_reservations
        self._enforce_attributed_inventory = enforce_attributed_inventory

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

        pending_count = len(self._pending) if self._include_pending_reservations else 0
        pending_gross = (
            self._pending.reserved_gross_notional()
            if self._include_pending_reservations else ZERO
        )
        pending_symbol = (
            self._pending.reserved_notional(symbol=request.symbol)
            if self._include_pending_reservations else ZERO
        )
        pending_strategy = (
            self._pending.reserved_notional(strategy_id=request.strategy_id)
            if self._include_pending_reservations else ZERO
        )

        # 4. Reduce-only behaviour. Reduction proof is intentionally stricter than
        # ordinary exposure projection: direction, quantity, open-order ambiguity, and every
        # pending intent must all be clear before any reduce-only exemption can apply.
        reduce_only_proven = False
        reduce_only_exemptions_enabled = False
        position = state.position(request.symbol)
        if limits.reduce_only_mode:
            reduces_direction = (
                (position > ZERO and request.side is Side.SELL)
                or (position < ZERO and request.side is Side.BUY)
            )
            rule(
                "reduce_only",
                reduces_direction,
                "reduce_only_violation" if not reduces_direction else "reduce_only_ok",
                ZERO if reduces_direction else Decimal(1),
                ZERO,
                "count",
            )

            quantity_within_position = reduces_direction and request.quantity <= abs(position)
            if reduces_direction:
                rule(
                    "reduce_only_quantity",
                    quantity_within_position,
                    (
                        "reduce_only_quantity_exceeded"
                        if not quantity_within_position
                        else "reduce_only_quantity_ok"
                    ),
                    request.quantity,
                    abs(position),
                    "base_quantity",
                )

            no_open_orders = state.open_orders == 0
            rule(
                "reduce_only_open_orders",
                no_open_orders,
                (
                    "reduce_only_open_orders_present"
                    if not no_open_orders
                    else "reduce_only_open_orders_clear"
                ),
                Decimal(state.open_orders),
                ZERO,
                "count",
            )

            no_pending_intents = len(self._pending) == 0
            rule(
                "reduce_only_pending_intents",
                no_pending_intents,
                (
                    "reduce_only_pending_intents_present"
                    if not no_pending_intents
                    else "reduce_only_pending_intents_clear"
                ),
                Decimal(len(self._pending)),
                ZERO,
                "count",
            )

            reduce_only_proven = (
                reduces_direction
                and quantity_within_position
                and no_open_orders
                and no_pending_intents
            )
            reduce_only_exemptions_enabled = reduce_only_proven and all(
                result.allowed for result in results
            )

        # 5/6. Open orders and positions. Approved-but-not-yet-journaled production intents
        # reserve one future order slot until submission resolves.
        projected_open_orders = state.open_orders + pending_count + 1
        rule("max_open_orders", projected_open_orders <= limits.max_open_orders,
             "max_open_orders_exceeded" if projected_open_orders > limits.max_open_orders else "open_orders_ok",
             Decimal(projected_open_orders), Decimal(limits.max_open_orders), "count")
        open_positions_ok = state.open_positions < limits.max_open_positions
        if reduce_only_exemptions_enabled and not open_positions_ok:
            rule(
                "max_open_positions",
                True,
                "reduce_only_non_expanding",
                Decimal(state.open_positions),
                Decimal(limits.max_open_positions),
                "count",
            )
        else:
            rule(
                "max_open_positions",
                open_positions_ok,
                (
                    "max_open_positions_exceeded"
                    if not open_positions_ok
                    else "open_positions_ok"
                ),
                Decimal(state.open_positions),
                Decimal(limits.max_open_positions),
                "count",
            )

        # 7. Order notional (quote currency).
        notional = request.notional
        rule("max_order_notional", notional <= limits.max_order_notional_quote,
             "max_order_notional_exceeded" if notional > limits.max_order_notional_quote else "order_notional_ok",
             notional, limits.max_order_notional_quote, "quote_currency")

        # 8/9. Projected symbol and strategy exposure (quote currency). Pending production
        # reservations are added conservatively by magnitude so an unjournaled approval can never
        # cancel a durable exposure and create a false allowance.
        current_symbol_exposure = state.symbol_exposure(request.symbol)
        projected_symbol_signed = current_symbol_exposure + request.signed_notional
        projected_symbol = (
            abs(current_symbol_exposure) + pending_symbol + notional
            if self._include_pending_reservations
            else abs(projected_symbol_signed)
        )
        symbol_nonexpanding = (
            reduce_only_exemptions_enabled
            and current_symbol_exposure != ZERO
            and (
                (position > ZERO and current_symbol_exposure > ZERO)
                or (position < ZERO and current_symbol_exposure < ZERO)
            )
            and abs(projected_symbol_signed) <= abs(current_symbol_exposure)
        )
        symbol_ok = projected_symbol <= limits.max_symbol_exposure_quote
        if symbol_nonexpanding and not symbol_ok:
            rule(
                "max_symbol_exposure",
                True,
                "reduce_only_non_expanding",
                projected_symbol,
                limits.max_symbol_exposure_quote,
                "quote_currency",
            )
        else:
            rule(
                "max_symbol_exposure",
                symbol_ok,
                (
                    "max_symbol_exposure_exceeded"
                    if not symbol_ok
                    else "symbol_exposure_ok"
                ),
                projected_symbol,
                limits.max_symbol_exposure_quote,
                "quote_currency",
            )

        current_strategy_exposure = state.strategy_exposure(request.strategy_id)
        projected_strategy_signed = current_strategy_exposure + request.signed_notional
        projected_strategy = (
            abs(current_strategy_exposure) + pending_strategy + notional
            if self._include_pending_reservations
            else abs(projected_strategy_signed)
        )
        strategy_nonexpanding = (
            reduce_only_exemptions_enabled
            and abs(projected_strategy_signed) <= abs(current_strategy_exposure)
        )
        strategy_ok = projected_strategy <= limits.max_strategy_exposure_quote
        if strategy_nonexpanding and not strategy_ok:
            rule(
                "max_strategy_exposure",
                True,
                "reduce_only_non_expanding",
                projected_strategy,
                limits.max_strategy_exposure_quote,
                "quote_currency",
            )
        else:
            rule(
                "max_strategy_exposure",
                strategy_ok,
                (
                    "max_strategy_exposure_exceeded"
                    if not strategy_ok
                    else "strategy_exposure_ok"
                ),
                projected_strategy,
                limits.max_strategy_exposure_quote,
                "quote_currency",
            )

        # 10/11. Gross and net exposure (conservative deterministic projections).
        projected_gross = state.gross_exposure_quote + pending_gross + notional
        rule("max_gross_exposure", projected_gross <= limits.max_gross_exposure_quote,
             "max_gross_exposure_exceeded" if projected_gross > limits.max_gross_exposure_quote else "gross_exposure_ok",
             projected_gross, limits.max_gross_exposure_quote, "quote_currency")
        current_net_exposure = state.net_exposure_quote
        projected_net_signed = current_net_exposure + request.signed_notional
        projected_net = (
            abs(current_net_exposure) + pending_gross + notional
            if self._include_pending_reservations
            else abs(projected_net_signed)
        )
        net_nonexpanding = (
            reduce_only_exemptions_enabled
            and abs(projected_net_signed) <= abs(current_net_exposure)
        )
        net_ok = projected_net <= limits.max_net_exposure_quote
        if net_nonexpanding and not net_ok:
            rule(
                "max_net_exposure",
                True,
                "reduce_only_non_expanding",
                projected_net,
                limits.max_net_exposure_quote,
                "quote_currency",
            )
        else:
            rule(
                "max_net_exposure",
                net_ok,
                "max_net_exposure_exceeded" if not net_ok else "net_exposure_ok",
                projected_net,
                limits.max_net_exposure_quote,
                "quote_currency",
            )

        if self._enforce_attributed_inventory:
            if request.side is Side.SELL:
                reserved_sell = (
                    self._pending.reserved_sell_quantity(
                        symbol=request.symbol, strategy_id=request.strategy_id
                    )
                    if self._include_pending_reservations else ZERO
                )
                available_quantity = state.strategy_position(request.strategy_id)
                projected_sell_quantity = (
                    state.strategy_open_sell(request.strategy_id)
                    + reserved_sell
                    + request.quantity
                )
                rule(
                    "attributed_inventory",
                    projected_sell_quantity <= available_quantity,
                    (
                        "insufficient_attributed_inventory"
                        if projected_sell_quantity > available_quantity
                        else "attributed_inventory_ok"
                    ),
                    projected_sell_quantity,
                    available_quantity,
                    "base_quantity",
                )
            elif limits.reduce_only_mode and request.side is Side.BUY:
                available_short_quantity = max(
                    ZERO,
                    -state.strategy_position(request.strategy_id),
                )
                rule(
                    "attributed_inventory",
                    request.quantity <= available_short_quantity,
                    (
                        "insufficient_attributed_inventory"
                        if request.quantity > available_short_quantity
                        else "attributed_inventory_ok"
                    ),
                    request.quantity,
                    available_short_quantity,
                    "base_quantity",
                )

        # 12. Loss limit (quote currency; loss expressed as a positive magnitude).
        loss = max(ZERO, -state.realized_pnl_quote)
        loss_breached = loss >= limits.loss_limit_quote
        if reduce_only_exemptions_enabled and loss_breached:
            rule(
                "loss_limit",
                True,
                "reduce_only_non_expanding",
                loss,
                limits.loss_limit_quote,
                "quote_currency",
            )
        else:
            rule(
                "loss_limit",
                not loss_breached,
                "loss_limit_breached" if loss_breached else "loss_within_limit",
                loss,
                limits.loss_limit_quote,
                "quote_currency",
            )

        # 13. Drawdown threshold (percent).
        if state.peak_equity_quote > 0:
            drawdown_pct = ((state.peak_equity_quote - state.equity_quote) / state.peak_equity_quote) * HUNDRED
        else:
            drawdown_pct = ZERO
        drawdown_breached = drawdown_pct >= limits.max_drawdown_pct
        if reduce_only_exemptions_enabled and drawdown_breached:
            rule(
                "max_drawdown",
                True,
                "reduce_only_non_expanding",
                drawdown_pct,
                limits.max_drawdown_pct,
                "percent",
            )
        else:
            rule(
                "max_drawdown",
                not drawdown_breached,
                "max_drawdown_exceeded" if drawdown_breached else "drawdown_ok",
                drawdown_pct,
                limits.max_drawdown_pct,
                "percent",
            )

        # 14. Concentration limit (percent of equity).
        if state.equity_quote > 0:
            concentration_pct = (projected_symbol / state.equity_quote) * HUNDRED
        else:
            concentration_pct = HUNDRED if projected_symbol > 0 else ZERO
        concentration_ok = concentration_pct <= limits.concentration_limit_pct
        concentration_nonexpanding = (
            symbol_nonexpanding
            and state.equity_quote > ZERO
        )
        if concentration_nonexpanding and not concentration_ok:
            rule(
                "concentration",
                True,
                "reduce_only_non_expanding",
                concentration_pct,
                limits.concentration_limit_pct,
                "percent",
            )
        else:
            rule(
                "concentration",
                concentration_ok,
                "concentration_exceeded" if not concentration_ok else "concentration_ok",
                concentration_pct,
                limits.concentration_limit_pct,
                "percent",
            )

        violations = [result for result in results if not result.allowed]
        allowed = not violations
        reason_codes = [result.reason_code for result in violations] or ["all_rules_passed"]

        risk_actions: list[str] = []
        if loss_breached or drawdown_breached:
            # Preserve the breach signal even when a proven reduction is admitted.
            # Halt new risk only. Never a liquidation instruction.
            risk_actions.append("halt_new_risk")

        if allowed:
            self._pending.register(
                PendingIntent(
                    intent_id=request.intent_id,
                    symbol=request.symbol,
                    strategy_id=request.strategy_id,
                    side=request.side,
                    quantity=request.quantity,
                    notional=request.notional,
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
    ) -> RiskEvaluationRecordedV2:
        """Translate the decision into explicit ``risk.evaluation.recorded`` V2.

        ``occurred_at`` is supplied by the caller so the engine itself never reads a clock and the
        recorded evaluation stays deterministic and replayable.
        """
        if occurred_at.tzinfo is None or occurred_at.utcoffset() is None:
            raise RiskConfigError("risk evaluation occurred_at must be timezone-aware.")
        return RiskEvaluationRecordedV2(
            event_id=event_id or uuid4(),
            occurred_at=occurred_at.astimezone(timezone.utc),
            correlation_id=decision.correlation_id,
            producer="application.risk",
            symbol=decision.symbol,
            payload=RiskEvaluationPayloadV2(
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
                    RiskRuleResultPayloadV2(
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
