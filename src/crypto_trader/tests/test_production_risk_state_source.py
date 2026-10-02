"""G5b canonical live production risk-state source tests."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from crypto_trader.persistence import SQLiteEventJournal
from crypto_trader.production import ProductionStateError
from crypto_trader.production.equity import AccountEquityBaselineWriter, load_equity_chain
from crypto_trader.production.risk_state import ProductionRiskStateSource
from crypto_trader.tests import testnet_fixtures as fx
from crypto_trader.tests.production_fixtures import INSTRUMENT, control_for
from crypto_trader.validity import MarketObservation, ReplayMarketStateProvider

UTC = timezone.utc
NOW = datetime.fromtimestamp(fx.SERVER_TIME_MS / 1000, tz=UTC)
ACCOUNT = "123456789"
ENV = "binance-spot-testnet"


@pytest.fixture
def journal(tmp_path: Path):
    with SQLiteEventJournal(tmp_path / "journal.db") as active:
        yield active


@pytest.fixture
def state_dir(tmp_path: Path) -> Path:
    path = tmp_path / "state"
    path.mkdir(parents=True, exist_ok=True)
    return path


def seed_baseline(journal, state_dir):
    transport = fx.ScriptedTransport([
        fx.time_response(),
        fx.open_orders_response(),
        fx.account_response(),
        fx.open_orders_response(),
    ])
    client = fx.client(transport)
    control = control_for(journal, state_dir, when=NOW)
    market = ReplayMarketStateProvider([
        MarketObservation(
            symbol=INSTRUMENT.symbol,
            price=Decimal("27000"),
            market_event_ts=NOW,
            local_ingest_ts=NOW,
            market_reference="baseline-mark",
            sequence=1,
        )
    ])
    writer = AccountEquityBaselineWriter(
        journal=journal,
        control=control,
        client=client,
        market_state=market,
        instrument=INSTRUMENT,
        environment=ENV,
        venue_account_id=ACCOUNT,
        max_account_age_seconds=5,
        max_mark_age_seconds=5,
        clock=lambda: NOW,
    )
    report = writer.create()
    return control, report


def source(journal, control, responses):
    transport = fx.ScriptedTransport(responses)
    client = fx.client(transport)
    return ProductionRiskStateSource(
        journal=journal,
        control=control,
        client=client,
        instrument=INSTRUMENT,
        environment=ENV,
        venue_account_id=ACCOUNT,
        max_account_age_seconds=5,
        max_mark_age_seconds=5,
    ), transport


def refresh_responses(*, price="27000.00", account=None, before=None, after=None):
    return [
        fx.time_response(fx.SERVER_TIME_MS + 1),
        before or fx.open_orders_response(),
        account or fx.account_response(),
        fx.price_response(price),
        after or fx.open_orders_response(),
    ]


def test_source_returns_canonical_state_without_writing_when_peak_is_unchanged(journal, state_dir):
    control, report = seed_baseline(journal, state_dir)
    guard = control.claim_normal_writer(owner_id="normal:runtime", reason_code="test_runtime")
    risk_source, _ = source(journal, control, refresh_responses(price="27000.00"))

    state, evaluated_at = risk_source(NOW, guard=guard)

    assert evaluated_at == NOW + timedelta(milliseconds=1)
    assert journal.count() == 1
    assert state.equity_quote == report.equity_quote
    assert state.peak_equity_quote == report.equity_quote
    assert state.position_quantity[INSTRUMENT.symbol] == Decimal("1.0")


def test_no_peak_return_refuses_final_journal_revision_drift(
    journal, state_dir, monkeypatch
):
    control, _ = seed_baseline(journal, state_dir)
    guard = control.claim_normal_writer(owner_id="normal:runtime", reason_code="runtime")
    risk_source, _ = source(journal, control, refresh_responses(price="27000.00"))
    stable = journal.revision()
    calls = 0

    def drifting_revision():
        nonlocal calls
        calls += 1
        return stable if calls < 4 else object()

    monkeypatch.setattr(journal, "revision", drifting_revision)

    with pytest.raises(ProductionStateError, match="journal changed before canonical risk state"):
        risk_source(NOW, guard=guard)

    assert calls == 4
    assert journal.count() == 1


def test_new_high_water_is_durable_before_source_returns(journal, state_dir):
    control, report = seed_baseline(journal, state_dir)
    guard = control.claim_normal_writer(owner_id="normal:runtime", reason_code="test_runtime")
    risk_source, _ = source(journal, control, refresh_responses(price="28000.00"))

    state, evaluated_at = risk_source(NOW.replace(microsecond=1), guard=guard)

    assert evaluated_at == NOW + timedelta(milliseconds=1)
    assert journal.count() == 2
    chain = load_equity_chain(
        journal,
        symbol=INSTRUMENT.symbol,
        environment=ENV,
        venue_account_id=ACCOUNT,
        base_asset=INSTRUMENT.base_currency,
        quote_asset=INSTRUMENT.quote_currency,
    )
    assert state.peak_equity_quote == Decimal("128000")
    assert chain.peak_equity_quote == state.peak_equity_quote
    assert chain.baseline_event_id == report.event_id
    assert chain.last_kind == "peak"
    assert chain.fence_term == guard.fence.term


def test_stale_runtime_guard_refuses_before_network(journal, state_dir):
    control, _ = seed_baseline(journal, state_dir)
    stale = control.claim_normal_writer(owner_id="normal:old", reason_code="old")
    control.claim_normal_writer(owner_id="normal:new", reason_code="new")
    risk_source, transport = source(journal, control, [])

    with pytest.raises(ProductionStateError, match="writer fence is stale"):
        risk_source(NOW, guard=stale)

    assert transport.requests == []
    assert journal.count() == 1


def test_venue_open_orders_changing_during_refresh_fail_closed(journal, state_dir):
    control, _ = seed_baseline(journal, state_dir)
    guard = control.claim_normal_writer(owner_id="normal:runtime", reason_code="runtime")
    risk_source, _ = source(
        journal,
        control,
        refresh_responses(after=fx.open_orders_response("ct-raced-open")),
    )

    with pytest.raises(ProductionStateError, match="open orders changed"):
        risk_source(NOW, guard=guard)

    assert journal.count() == 1
