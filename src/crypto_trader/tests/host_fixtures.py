"""Deterministic offline market and plans for the Paper host tests (not collected)."""
from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from crypto_trader.paper_host.host import HostConfig, create_session
from crypto_trader.paper_host.public_market import FeedUnavailable, MarketBatch
from crypto_trader.paper_host.tape import PublicBook, PublicTrade
from crypto_trader.tests import paper_fixtures as fx

START_NS = 1_800_000_000 * 1_000_000_000
STEP_NS = 5 * 1_000_000_000
D = Decimal


def plan(**overrides) -> str:
    body = {
        "symbol": "ETHUSDT", "bias": "bullish", "confidence": "high",
        "human_readable_plan": "offline plan", "position_size_pct": 2,
        "entry_low": 2660, "entry_high": 2740, "stop_loss": 2650,
        "take_profit_targets": [{"level": 2750, "allocation_pct": 100}],
        "max_loss_pct": 1, "risk_reward_ratio": 2, "plan_validity": "1h",
        "invalidation_conditions": ["trend breaks"],
    }
    body.update(overrides)
    return "Plan\n```json\n" + json.dumps(body) + "\n```"


NEUTRAL = dict(bias="neutral", confidence="medium")


def config(**overrides) -> HostConfig:
    values = dict(
        session_id=str(fx.SESSION_ID), symbol="ETHUSDT", base_currency="ETH", quote_currency="USDT",
        starting_quote="100000", max_notional_quote="3000", decision_interval_seconds=20,
        max_plan_ttl_seconds=3600, tick_size="0.01", quantity_step="0.0001", price_precision=2,
        size_precision=8, maker_fee="0.0002", taker_fee="0.0004", provider_kind="stub",
        limits=dict(fx.LIMIT_CONFIG),
        validity={"policy_id": "validity-v1", "max_age_seconds": 120, "max_market_move_bps": "200"},
        max_feed_age_seconds=10**9,
    )
    values.update(overrides)
    return HostConfig(**values)


@dataclass
class ScriptedMarket:
    """Stateless by design: the next step is derived from the durable trade cursor alone."""

    prices: tuple[str, ...]
    per_step: int = 3
    size: str = "200"

    def wall_clock(self) -> int:
        return START_NS + len(self.prices) * STEP_NS

    def __call__(self, after: int | None) -> MarketBatch:
        step = 0 if after is None else (after + 1) // self.per_step
        if step >= len(self.prices):
            raise FeedUnavailable("scripted market exhausted")
        price = D(self.prices[step])
        ts = START_NS + step * STEP_NS
        trades = tuple(
            PublicTrade(trade_id=step * self.per_step + index, price=price, size=D(self.size),
                        ts_ns=ts + (index + 1) * 1_000_000, seller_aggressor=bool(index % 2))
            for index in range(self.per_step)
        )
        book = PublicBook(bid=price, bid_size=D(self.size), ask=price + D("1"), ask_size=D(self.size),
                          received_ns=ts + 500_000_000)
        return MarketBatch(book=book, trades=trades)


class Plans:
    """Maps observation index to a plan and records each prompt's portfolio context."""

    def __init__(self, *plans, log: Path | None = None):
        self.plans = plans
        self.contexts: list[str] = []
        self.log = log

    def __call__(self, inputs) -> str:
        index = int(inputs["market_reference"].rsplit("#", 1)[1])
        self.contexts.append(inputs["portfolio_context"])
        if self.log is not None:
            with self.log.open("a", encoding="utf-8") as stream:
                stream.write(inputs["market_reference"] + "\n")
        reply = self.plans[index] if index < len(self.plans) else plan(**NEUTRAL)
        if isinstance(reply, Exception):
            raise reply
        return reply


# One lifecycle: buy -> stop-out -> a smaller re-entry shaped by the loss -> target -> flat.
LIFECYCLE_PRICES = (
    "2700", "2700", "2700", "2700", "2700", "2700",
    "2640", "2640",
    "2700", "2700", "2700", "2700", "2700", "2700", "2700", "2700",
    "2760", "2760", "2760", "2760", "2760", "2760", "2760", "2760",
)
LIFECYCLE_PLANS = (plan(), plan(**NEUTRAL), plan(), plan(**NEUTRAL), plan(**NEUTRAL), plan(**NEUTRAL))


def new_session(root: Path, **overrides) -> Path:
    return create_session(root / "session", config(**overrides))
