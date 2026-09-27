"""Deterministic fixtures for the Phase 15 mainnet read-only dry-run tests (not a test module).

Every value here is synthetic. The scripted transport never reaches the network, and the credential
placeholders are not real keys.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid5

from crypto_trader.breakglass import BreakGlassControl, parse_operator_command
from crypto_trader.execution import (
    ExecutionOrderType,
    ExecutionSide,
    ExecutionTimeInForce,
    InstrumentSpec,
)
from crypto_trader.execution.binance_testnet import HttpResponse, ScriptedTransport
# Aliased so pytest never mistakes the adapter's classes for test classes.
from crypto_trader.execution.binance_testnet import TestnetClock as HttpClock
from crypto_trader.mainnet import (
    LOCKED_PILOT_ENVELOPE,
    SPOT_MAINNET_REST_BASE,
    HardNoSubmitGate,
    MainnetDryRunPipeline,
    MainnetReadOnlyClient,
    MainnetReadOnlyCredentials,
    SoakRecorder,
    SoakStateStore,
    SymbolFilters,
    parse_symbol_filters,
)
from crypto_trader.persistence import EventJournal
from crypto_trader.production import ProductionIntent
from crypto_trader.recovery import ReconciliationPolicy, load_policy as load_recovery_policy
from crypto_trader.risk import (
    PendingIntentRegistry,
    RiskLimits,
    RiskStateSnapshot,
    load_limits,
)
from crypto_trader.validity import (
    DecisionValidityGate,
    MarketObservation,
    ReplayMarketStateProvider,
)
from crypto_trader.validity import load_policy as load_validity_policy

BASE = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
SYMBOL = "BTCUSDT"
PRICE = Decimal("27000.00")
SERVER_TIME_MS = 1_767_225_600_000
NAMESPACE = UUID("7a1c9e30-4b62-4f18-9d7a-2c5e8b0f4d11")

#: Synthetic placeholders used only against the scripted transport.
FAKE_API_KEY = "mainnet-readonly-key-placeholder"
FAKE_API_SECRET = "mainnet-readonly-secret-placeholder"

INSTRUMENT = InstrumentSpec(
    symbol=SYMBOL,
    venue="BINANCE_SPOT_MAINNET_READONLY",
    base_currency="BTC",
    quote_currency="USDT",
    price_precision=2,
    size_precision=5,
    starting_balance="0 USDT",
)

#: Synthetic limit fixtures (TEST values; production limits remain an owner decision).
LIMIT_CONFIG = {
    "limits_version": "risk-mainnet-dryrun-test-v1",
    "max_order_notional_quote": "5000",
    "max_symbol_exposure_quote": "20000",
    "max_gross_exposure_quote": "50000",
    "max_net_exposure_quote": "30000",
    "max_strategy_exposure_quote": "40000",
    "loss_limit_quote": "1000",
    "max_drawdown_pct": "10",
    "concentration_limit_pct": "50",
    "max_open_orders": 5,
    "max_open_positions": 3,
}

POLICY_ID = "mainnet-dryrun-test-v1"


def credentials() -> MainnetReadOnlyCredentials:
    return MainnetReadOnlyCredentials(api_key=FAKE_API_KEY, api_secret=FAKE_API_SECRET)


def clock() -> HttpClock:
    """A clock already synchronized with the (scripted) server, offset zero."""
    instance = HttpClock(time_source=lambda: SERVER_TIME_MS / 1000, recv_window_ms=5_000)
    instance.synchronize(SERVER_TIME_MS, local_time_ms=SERVER_TIME_MS)
    return instance


def client(transport: ScriptedTransport, **overrides) -> MainnetReadOnlyClient:
    return MainnetReadOnlyClient(
        credentials=overrides.pop("credentials", None) or credentials(),
        transport=transport,
        clock=overrides.pop("clock", None) or clock(),
        base_url=overrides.pop("base_url", SPOT_MAINNET_REST_BASE),
        symbol=overrides.pop("symbol", SYMBOL),
    )


# --- response builders -----------------------------------------------------

def response(status: int, body) -> HttpResponse:
    return HttpResponse(status_code=status, body=body)


def restrictions_body(**overrides) -> dict:
    body = {
        "enableReading": True,
        "enableWithdrawals": False,
        "enableSpotAndMarginTrading": False,
        "enableMargin": False,
        "enableFutures": False,
        "enableVanillaOptions": False,
        "enableFixApiTrade": False,
        "ipRestrict": False,
        "createTime": 1_700_000_000_000,
    }
    body.update(overrides)
    return body


def exchange_info_body(
    *,
    symbol: str = SYMBOL,
    tick: str = "0.01",
    step: str = "0.00001",
    min_qty: str = "0.00001",
    max_qty: str = "9000.00000000",
    min_notional: str = "5.00000000",
    status: str = "TRADING",
    base: str = "BTC",
    quote: str = "USDT",
) -> dict:
    return {
        "symbols": [
            {
                "symbol": symbol,
                "status": status,
                "baseAsset": base,
                "quoteAsset": quote,
                "filters": [
                    {"filterType": "PRICE_FILTER", "tickSize": tick},
                    {"filterType": "LOT_SIZE", "stepSize": step, "minQty": min_qty, "maxQty": max_qty},
                    {"filterType": "NOTIONAL", "minNotional": min_notional},
                ],
            }
        ]
    }


def book_ticker_body(bid: str = "26999.99", ask: str = "27000.01") -> dict:
    return {"symbol": SYMBOL, "bidPrice": bid, "askPrice": ask}


def price_body(price: str = "27000.00") -> dict:
    return {"symbol": SYMBOL, "price": price}


def time_body(server_time_ms: int = SERVER_TIME_MS) -> dict:
    return {"serverTime": server_time_ms}


def account_body(*, can_trade: bool = False, can_withdraw: bool = False) -> dict:
    return {
        "makerCommission": 10,
        "takerCommission": 10,
        "canTrade": can_trade,
        "canWithdraw": can_withdraw,
        "canDeposit": False,
        "accountType": "SPOT",
        "balances": [{"asset": "USDT", "free": "0.00000000", "locked": "0.00000000"}],
        "permissions": ["SPOT"],
    }


def open_orders_body(*client_order_ids: str) -> list:
    return [
        {
            "symbol": SYMBOL,
            "orderId": 1000 + index,
            "clientOrderId": client_order_id,
            "price": "27000.00",
            "origQty": "0.00100000",
            "executedQty": "0.00000000",
            "status": "NEW",
            "type": "LIMIT",
            "side": "BUY",
            "time": SERVER_TIME_MS,
        }
        for index, client_order_id in enumerate(client_order_ids)
    ]


# --- domain fixtures -------------------------------------------------------

def filters(**overrides) -> SymbolFilters:
    return parse_symbol_filters(exchange_info_body(**overrides), overrides.get("symbol", SYMBOL))


def limits() -> RiskLimits:
    return load_limits(LIMIT_CONFIG)


def recovery_policy() -> ReconciliationPolicy:
    return load_recovery_policy({"policy_id": POLICY_ID})


def validity_gate() -> DecisionValidityGate:
    return DecisionValidityGate(
        load_validity_policy(
            {
                "policy_id": POLICY_ID,
                "max_age_seconds": 120,
                "max_market_move_bps": "200",
            }
        )
    )


def market_state(*, price: Decimal = PRICE, when: datetime = BASE, reference: str = "mkt-mainnet-1"):
    return ReplayMarketStateProvider(
        [
            MarketObservation(
                symbol=SYMBOL,
                price=price,
                market_event_ts=when,
                local_ingest_ts=when,
                market_reference=reference,
                sequence=1,
            )
        ]
    )


def risk_state(*, now: datetime | None = None) -> RiskStateSnapshot:
    return RiskStateSnapshot(
        equity_quote=Decimal("1000"),
        peak_equity_quote=Decimal("1000"),
        realized_pnl_quote=Decimal("0"),
        gross_exposure_quote=Decimal("0"),
        net_exposure_quote=Decimal("0"),
        open_orders=0,
        open_positions=0,
        snapshot_ts=now or BASE,
    )


def intent(seed: str = "mainnet-intent-1", **overrides) -> ProductionIntent:
    values: dict[str, object] = {
        "correlation_id": uuid5(NAMESPACE, f"corr:{seed}"),
        "symbol": SYMBOL,
        "side": ExecutionSide.BUY,
        "order_type": ExecutionOrderType.LIMIT,
        "quantity": Decimal("0.01000"),
        "strategy_id": "strategy.mainnet-dryrun-test",
        "decision_started_at": BASE - timedelta(seconds=1),
        "decision_completed_at": BASE,
        "market_event_ts": BASE,
        "reference_price": PRICE,
        "market_reference": "mkt-mainnet-1",
        "intent_id": uuid5(NAMESPACE, seed),
        "limit_price": PRICE,
        "time_in_force": ExecutionTimeInForce.FOK,
    }
    values.update(overrides)
    return ProductionIntent(**values)  # type: ignore[arg-type]


def gate(**overrides) -> HardNoSubmitGate:
    return HardNoSubmitGate(instrument=overrides.pop("instrument", INSTRUMENT))


def soak(tmp_path: Path, *, when: datetime = BASE) -> SoakRecorder:
    return SoakRecorder(
        SoakStateStore(tmp_path / "soak.json"), clock=lambda: when
    )


def control_for(
    journal: EventJournal, state_dir: Path, *, when: datetime = BASE, venue=None
) -> BreakGlassControl:
    return BreakGlassControl(
        journal=journal,
        state_dir=state_dir,
        venue=venue,
        reconciliation_policy=recovery_policy(),
        clock=lambda: when,
    )


def operator_command(
    command_type: str, *, seed: str, requested_at: datetime = BASE, **overrides
):
    payload = {
        "command_type": command_type,
        "command_id": str(uuid5(NAMESPACE, f"command:{seed}")),
        "operator_label": "jamal",
        "reason": "phase 15 mainnet dry-run test",
        "source": "operator-cli",
        "requested_at": requested_at.isoformat(),
    }
    payload.update(overrides)
    return parse_operator_command(payload, delivered_at=requested_at)


def pipeline(
    *,
    journal: EventJournal,
    state_dir: Path,
    tmp_path: Path,
    gate_instance: HardNoSubmitGate | None = None,
    decision_source=None,
    market=None,
    permission_check=None,
    filters_provider=None,
    quote_provider=None,
    client_instance: MainnetReadOnlyClient | None = None,
    control: BreakGlassControl | None = None,
    soak_instance: SoakRecorder | None = None,
    when: datetime = BASE,
    clock_fn=None,
) -> MainnetDryRunPipeline:
    # A client-backed pipeline reads its filters/top-of-book from the client unless overridden.
    if filters_provider is None and client_instance is None:
        filters_provider = lambda symbol: filters()
    if quote_provider is None and client_instance is None:
        quote_provider = lambda symbol: (Decimal("26999.99"), Decimal("27000.01"))
    return MainnetDryRunPipeline(
        journal=journal,
        state_dir=state_dir,
        instrument=INSTRUMENT,
        market_state=market or market_state(),
        decision_source=decision_source or (lambda snapshot: intent()),
        limits=limits(),
        risk_state_provider=lambda now: risk_state(now=now),
        risk_state_max_age_seconds=5.0,
        validity_gate=validity_gate(),
        reconciliation_policy=recovery_policy(),
        permission_check=permission_check or (lambda: restrictions()),
        gate=gate_instance or gate(),
        client=client_instance,
        filters_provider=filters_provider,
        quote_provider=quote_provider,
        soak=soak_instance or soak(tmp_path, when=when),
        control=control,
        clock=clock_fn or (lambda: when),
    )


def restrictions():
    """A validated read-only profile (bypasses the transport for unit-level pipeline tests)."""
    from crypto_trader.mainnet import parse_api_restrictions

    return parse_api_restrictions(restrictions_body())


__all__ = [
    "BASE",
    "FAKE_API_KEY",
    "FAKE_API_SECRET",
    "INSTRUMENT",
    "LIMIT_CONFIG",
    "NAMESPACE",
    "POLICY_ID",
    "PRICE",
    "SERVER_TIME_MS",
    "SYMBOL",
    "LOCKED_PILOT_ENVELOPE",
    "account_body",
    "book_ticker_body",
    "client",
    "clock",
    "control_for",
    "credentials",
    "exchange_info_body",
    "filters",
    "gate",
    "intent",
    "limits",
    "market_state",
    "open_orders_body",
    "operator_command",
    "pipeline",
    "price_body",
    "recovery_policy",
    "response",
    "restrictions",
    "restrictions_body",
    "risk_state",
    "soak",
    "time_body",
    "validity_gate",
]
