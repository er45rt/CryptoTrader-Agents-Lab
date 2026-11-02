"""Opt-in CrewAI advisory -> Paper DecisionSource bridge (simulated execution only).

The plan is untrusted advice. A deterministic host mapping turns it into a StrategyDecision, or a
recorded Hold, and the unchanged TradingSession still applies validity, hard risk and safety.
This module has no venue, order or credential path and never calls the Production gateway.

Each decision is written durably BEFORE it reaches the session, keyed by session and market
reference, so a restart replays it without a second kickoff. Restart means a full deterministic
replay with start_index=0 on a fresh simulator; the temporal session refuses a mid-tape resume.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from crypto_trader.llm.schemas import StructuredOutputError, parse_trading_plan
from crypto_trader.paper import MarketObservation, PaperStateError, PositionLedger, StrategyDecision
from crypto_trader.paper.accounting import require_unambiguous_fill_order
from crypto_trader.paper.decisions import FrozenRunInputs, input_json
from crypto_trader.paper.protection import ImmediateDecision, ProtectedDecision, ProtectionPlan, TakeProfitLevel
from crypto_trader.paper.session import _stable_id

__all__ = [
    "CrewPaperBridgeConfig", "CrewPaperDecisionSource", "PortfolioSnapshot",
    "crew_plan_provider", "plan_to_decision", "portfolio_snapshot",
]

_JSON_BLOCK = re.compile(r"```json\s*.*?```", re.IGNORECASE | re.DOTALL)
_TERMINAL_ORDER_STATES = {"filled", "canceled", "rejected", "expired"}
_MAX_PLAN_BYTES = 2_000_000
_RECORD_KEYS = {
    "version", "session_id", "market_reference", "observation_sha256",
    "plan_sha256", "decision", "provenance", "seal",
}
_TTL = re.compile(r"^\s*(\d{1,4})\s*(m|min|mins|minutes?|h|hr|hrs|hours?|d|days?)\s*$", re.IGNORECASE)
_PROTECTION_KEYS = {"protection", "valid_until"}

PORTFOLIO_PROMPT = (
    "\n\nCURRENT SIMULATED PORTFOLIO (host-computed; base the plan on it): {portfolio_context}"
)


@dataclass(frozen=True)
class CrewPaperBridgeConfig:
    """Host-approved economics; nothing here is taken from model text."""

    session_id: UUID
    symbol: str
    starting_equity_quote: Decimal
    max_notional_quote: Decimal
    tick_size: Decimal
    quantity_step: Decimal
    kline_interval: str = "4h"
    kline_limit: int = 72
    max_plan_ttl: timedelta = timedelta(hours=1)
    fee_reserve_rate: Decimal = Decimal("0.001")
    immediate_orders: bool = False

    def __post_init__(self) -> None:
        for name in ("starting_equity_quote", "max_notional_quote", "tick_size", "quantity_step"):
            value = getattr(self, name)
            if not isinstance(value, Decimal) or not value.is_finite() or value <= 0:
                raise PaperStateError(f"{name} requires a finite positive Decimal.")
        if not isinstance(self.max_plan_ttl, timedelta) or self.max_plan_ttl <= timedelta(0):
            raise PaperStateError("max_plan_ttl requires a positive timedelta.")


@dataclass(frozen=True)
class PortfolioSnapshot:
    """Point-in-time simulated state rebuilt from persisted fills only."""

    position_quantity: Decimal
    average_cost: Decimal
    realized_pnl_quote: Decimal
    equity_quote: Decimal
    open_sell_quantity: Decimal
    quote_free: Decimal = Decimal("1E+30")
    stop_exits: int = 0
    target_exits: int = 0

    def context(self) -> str:
        # Plain notation: a model must never have to read "0E-8" as a flat position.
        return ";".join(
            f"{name}={value.normalize():f}" if isinstance(value, Decimal) else f"{name}={value}"
            for name, value in asdict(self).items()
        )


def portfolio_snapshot(
    journal: Any, *, config: CrewPaperBridgeConfig, mark: Decimal, as_of: datetime,
) -> PortfolioSnapshot:
    """Rebuild the session's average-cost ledger from its journal, never from model text."""
    events = sorted(
        (event for event in journal.read_by_correlation(config.session_id)
         if event.occurred_at <= as_of),
        key=lambda event: event.occurred_at,
    )
    sides = {event.event_id: event.payload.side for event in events
             if event.event_type == "trade.intent.created"}
    rationales = {event.event_id: event.payload.rationale or "" for event in events
                  if event.event_type == "trade.intent.created"}
    require_unambiguous_fill_order(
        (event.occurred_at, sides.get(event.causation_id)) for event in events
        if event.event_type == "execution.fill.recorded"
    )
    quote = config.starting_equity_quote
    ledger = PositionLedger(config.starting_equity_quote)
    filled: dict[UUID, Decimal] = {}
    terminal: set[UUID] = set()
    for event in events:
        if event.event_type == "order.state.changed" and event.payload.state in _TERMINAL_ORDER_STATES:
            terminal.add(event.causation_id)
        if event.event_type != "execution.fill.recorded":
            continue
        side = sides.get(event.causation_id)
        if side is None:
            raise PaperStateError("persisted fill has no durable intent side.")
        filled[event.causation_id] = filled.get(event.causation_id, Decimal("0")) + event.payload.quantity
        notional = event.payload.quantity * event.payload.price
        quote += (notional if side == "sell" else -notional) - (event.payload.fee or Decimal("0"))
        ledger.apply_fill(symbol=config.symbol, side=side, quantity=event.payload.quantity,
                          price=event.payload.price, fee=event.payload.fee or Decimal("0"))
    open_sell = open_buy = Decimal("0")
    for event in events:
        if event.event_type != "risk.evaluation.recorded" or event.payload.decision != "allowed":
            continue
        origin = _stable_id(str(event.payload.intent_id), "intent-event")
        if origin in terminal:
            continue
        leaves = event.payload.quantity - filled.get(origin, Decimal("0"))
        if sides.get(origin) == "sell":
            open_sell += leaves
        elif sides.get(origin) == "buy":
            open_buy += leaves * event.payload.price
    position = ledger.position(config.symbol)
    exits = [rationales[origin] for origin in set(filled) if origin in rationales]
    return PortfolioSnapshot(
        position_quantity=position.quantity, average_cost=position.average_cost,
        realized_pnl_quote=position.realized_pnl_quote,
        equity_quote=ledger.equity_quote({config.symbol: mark}), open_sell_quantity=open_sell,
        quote_free=quote - open_buy,
        stop_exits=sum(text.startswith("protective:stop_loss") for text in exits),
        target_exits=sum(text.startswith("protective:take_profit") for text in exits),
    )


def _hold(reason: str, digest: str, decided_at: datetime) -> StrategyDecision:
    return StrategyDecision(
        action="hold", confidence="low", rationale=f"crew_refused:{reason}:sha256:{digest}",
        decided_at=decided_at,
    )


def _floor(value: Decimal, step: Decimal) -> Decimal:
    return (value / step).to_integral_value(rounding=ROUND_FLOOR) * step


def _ceil(value: Decimal, step: Decimal) -> Decimal:
    return (value / step).to_integral_value(rounding=ROUND_CEILING) * step


def plan_ttl(text: str, cap: timedelta) -> timedelta:
    """A readable duration in plan_validity shortens the host cap; prose never extends it."""
    match = _TTL.match(text)
    if match is None:
        return cap
    unit = match.group(2)[0].lower()
    span = timedelta(**{{"m": "minutes", "h": "hours", "d": "days"}[unit]: int(match.group(1))})
    return min(span, cap) if span > timedelta(0) else cap


def plan_to_decision(
    raw_plan: str, *, observation: MarketObservation, portfolio: PortfolioSnapshot,
    config: CrewPaperBridgeConfig, decided_at: datetime,
) -> StrategyDecision:
    """Deterministically map one plan; any unusable plan is a Hold, never a guess."""
    digest = hashlib.sha256(raw_plan.encode("utf-8")).hexdigest()
    if len(_JSON_BLOCK.findall(raw_plan)) != 1:
        return _hold("plan_unstructured", digest, decided_at)
    try:
        plan = parse_trading_plan(raw_plan)
    except StructuredOutputError:
        return _hold("plan_invalid", digest, decided_at)
    if plan.symbol != config.symbol:
        return _hold("plan_wrong_symbol", digest, decided_at)
    if plan.bias == "neutral":
        return _hold("plan_neutral", digest, decided_at)
    if plan.confidence == "low":
        return _hold("plan_low_confidence", digest, decided_at)

    buying = plan.bias == "bullish"
    low, high = Decimal(str(plan.entry_low)), Decimal(str(plan.entry_high))
    stop = Decimal(str(plan.stop_loss))
    if (buying and stop >= low) or (not buying and stop <= high):
        return _hold("stop_on_wrong_side", digest, decided_at)
    # Rounding must never widen the order: BUY floors and SELL ceils the observed price.
    rounding = ROUND_FLOOR if buying else ROUND_CEILING
    price = (observation.price / config.tick_size).to_integral_value(rounding=rounding) * config.tick_size
    if not low <= price <= high:
        return _hold("price_outside_entry_zone", digest, decided_at)

    budget = config.max_notional_quote
    valid_until = observation.observed_at + plan_ttl(plan.plan_validity, config.max_plan_ttl)
    if decided_at >= valid_until:
        return _hold("plan_expired", digest, decided_at)
    if buying:
        requested = portfolio.equity_quote * Decimal(str(plan.position_size_pct)) / Decimal("100")
        spendable = portfolio.quote_free / (1 + config.fee_reserve_rate)
        quantity = _floor(min(requested, budget, spendable) / price, config.quantity_step)
        # The stop distance caps the size: a full stop-out may lose max_loss_pct of equity at most.
        stop = _ceil(stop, config.tick_size)
        if stop >= price:
            return _hold("stop_on_wrong_side", digest, decided_at)
        allowed_loss = portfolio.equity_quote * Decimal(str(plan.max_loss_pct)) / Decimal("100")
        quantity = min(quantity, _floor(allowed_loss / (price - stop), config.quantity_step))
    else:
        # Spot-style exit: a bearish plan reduces an existing long and never opens a short.
        available = portfolio.position_quantity - portfolio.open_sell_quantity
        if available <= 0:
            return _hold("no_long_position", digest, decided_at)
        quantity = _floor(min(available, budget / price), config.quantity_step)
    if quantity <= 0:
        return _hold("quantity_below_step", digest, decided_at)
    if buying:
        levels = sorted(plan.take_profit_targets, key=lambda target: target.level)
        rounded_targets = tuple(
            (_floor(Decimal(str(target.level)), config.tick_size),
             Decimal(str(target.allocation_pct)))
            for target in levels
        )
        # Reject invalid quantized prices *before* constructing a validated level.
        # A model's sub-tick target must yield a durable Hold, not crash the Host.
        if (not rounded_targets or rounded_targets[0][0] <= price
                or len({level_price for level_price, _ in rounded_targets}) != len(rounded_targets)):
            return _hold("target_on_wrong_side", digest, decided_at)
        targets = tuple(
            TakeProfitLevel(level_price, allocation_pct)
            for level_price, allocation_pct in rounded_targets
        )
        return ProtectedDecision(
            action="buy", confidence=plan.confidence,
            rationale=f"crew_plan:{plan.bias}:sha256:{digest}", quantity=quantity,
            order_type="limit", limit_price=price, decided_at=decided_at,
            protection=ProtectionPlan(stop, targets), valid_until=valid_until,
            immediate=config.immediate_orders,
        )
    if config.immediate_orders:
        return ImmediateDecision(
            action="sell", confidence=plan.confidence, order_type="market",
            rationale=f"crew_plan:{plan.bias}:sha256:{digest}", quantity=quantity, decided_at=decided_at,
        )
    return StrategyDecision(
        action="buy" if buying else "sell", confidence=plan.confidence,
        rationale=f"crew_plan:{plan.bias}:sha256:{digest}", quantity=quantity,
        order_type="limit", limit_price=price, decided_at=decided_at,
    )


def _decision_from_row(row: dict[str, Any]) -> StrategyDecision:
    """Rebuild a stored decision, protected or plain, from its sealed record."""
    values = dict(row)
    protection = values.pop("protection", None)
    valid_until = values.pop("valid_until", None)
    immediate = values.pop("immediate", None)
    decision = FrozenRunInputs._decision(values)
    plain = {name: getattr(decision, name) for name in StrategyDecision.__dataclass_fields__}
    if protection is None:
        return decision if immediate is None else ImmediateDecision(**plain, immediate=immediate)
    plan = ProtectionPlan(
        Decimal(protection["stop_price"]),
        tuple(TakeProfitLevel(Decimal(item["price"]), Decimal(item["allocation_pct"]))
              for item in protection["take_profits"]),
    )
    return ProtectedDecision(**plain, immediate=immediate, protection=plan,
                             valid_until=datetime.fromisoformat(valid_until))


def _seal(record: dict[str, Any]) -> str:
    return hashlib.sha256(input_json({k: v for k, v in record.items() if k != "seal"})).hexdigest()


@dataclass
class CrewPaperDecisionSource:
    """TradingSession DecisionSource backed by one injected plan provider per observation."""

    config: CrewPaperBridgeConfig
    journal: Any
    store_dir: Path
    clock: Callable[[MarketObservation], datetime]
    plan_provider: Callable[[dict[str, Any]], str]
    provenance: dict[str, str] | None = None
    prepared_only: bool = False

    def __post_init__(self) -> None:
        path = Path(self.store_dir)
        if path.is_symlink() or not path.is_dir():
            raise PaperStateError("decision store must exist and not be a symlink.")
        self.store_dir = path.resolve(strict=True)

    def _path(self, market_reference: str) -> Path:
        key = f"{self.config.session_id}|{market_reference}".encode("utf-8")
        # Short names: a deep Windows store must not hit the legacy path-length limit.
        return self.store_dir / f"crew_{hashlib.sha256(key).hexdigest()[:32]}.json"

    def _load(self, path: Path, observation: MarketObservation) -> StrategyDecision:
        try:
            record = json.loads(path.read_bytes())
            decision = _decision_from_row(record["decision"])
            valid = (
                set(record) == _RECORD_KEYS and record["version"] == 1
                and record["seal"] == _seal(record)
                and record["session_id"] == str(self.config.session_id)
                and record["market_reference"] == observation.market_reference
                and record["observation_sha256"] == hashlib.sha256(input_json(observation)).hexdigest()
            )
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
            raise PaperStateError("unreadable durable Crew decision record.") from exc
        if not valid:
            raise PaperStateError("durable Crew decision record does not match this observation.")
        return decision

    def _persist(self, path: Path, observation: MarketObservation, decision: StrategyDecision,
                 digest: str) -> None:
        row = asdict(decision)
        row["decided_at"] = decision.decided_at.astimezone(timezone.utc)
        if isinstance(decision, ProtectedDecision):
            row["valid_until"] = decision.valid_until.astimezone(timezone.utc)
        record = {
            "version": 1, "session_id": str(self.config.session_id),
            "market_reference": observation.market_reference,
            "observation_sha256": hashlib.sha256(input_json(observation)).hexdigest(),
            "plan_sha256": digest, "decision": json.loads(input_json(row)),
            "provenance": dict(self.provenance or {"provider_kind": "unspecified"}),
        }
        record["seal"] = _seal(record)
        partial = path.with_name(f"{path.stem}.{uuid4().hex[:8]}.partial")
        with partial.open("xb") as stream:
            stream.write(input_json(record))
            stream.flush()
            os.fsync(stream.fileno())
        partial.rename(path)  # Windows refuses to replace an existing record.

    def has_decision(self, observation: MarketObservation) -> bool:
        return self._path(observation.market_reference).exists()

    def decide(self, observation: MarketObservation) -> StrategyDecision:
        if observation.symbol != self.config.symbol:
            raise PaperStateError("observation symbol differs from the bridge symbol.")
        path = self._path(observation.market_reference)
        if path.exists():
            return self._load(path, observation)
        if self.prepared_only:
            raise PaperStateError("a replayed observation has no durable decision.")
        return self.prepare(observation)

    def prepare(self, observation: MarketObservation) -> StrategyDecision:
        """Run the model once and persist; ``clock`` stamps completion, after the model returns."""
        path = self._path(observation.market_reference)
        if path.exists():
            return self._load(path, observation)
        portfolio = portfolio_snapshot(
            self.journal, config=self.config, mark=observation.price, as_of=observation.observed_at,
        )
        stamp = observation.observed_at.astimezone(timezone.utc)
        inputs = {
            "target_symbol": self.config.symbol, "kline_interval": self.config.kline_interval,
            "kline_limit": self.config.kline_limit, "market_reference": observation.market_reference,
            "current_datetime": stamp.strftime("%Y-%m-%d %H:%M:%S %Z"),
            "filename_datetime": stamp.strftime("%Y%m%d_%H%M%S"),
            "portfolio_context": portfolio.context(),
        }
        try:
            raw_plan = str(self.plan_provider(inputs))
            if len(raw_plan.encode("utf-8")) > _MAX_PLAN_BYTES:
                raise ValueError("oversized plan")
        except Exception as exc:
            digest = hashlib.sha256(b"").hexdigest()
            decision = _hold(f"crew_unavailable:{type(exc).__name__}", digest, self.clock(observation))
        else:
            digest = hashlib.sha256(raw_plan.encode("utf-8")).hexdigest()
            decided_at = self.clock(observation)
            decision = plan_to_decision(
                raw_plan, observation=observation, portfolio=portfolio,
                config=self.config, decided_at=decided_at,
            )
        self._persist(path, observation, decision, digest)
        return decision


def crew_plan_provider(inputs: dict[str, Any]) -> str:
    """One real CrewAI kickoff with the portfolio added to the strategist's task only."""
    from crypto_trader.crew import CryptoTraderCrew

    crew = CryptoTraderCrew(verbose=False).crew()
    crew.tasks[-1].description += PORTFOLIO_PROMPT
    for task in crew.tasks:
        task.output_file = None  # The sealed decision record is the artifact, not a cwd file.
    return str(crew.kickoff(inputs=inputs))
