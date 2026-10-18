"""G5b fenced pre-start account-equity baseline writer tests."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import pytest

from crypto_trader.events import OrderSubmissionAttemptPayload, OrderSubmissionAttempted
from crypto_trader.persistence import SQLiteEventJournal
from crypto_trader.production import ProductionStateError
from crypto_trader.production.equity import AccountEquityBaselineWriter, load_equity_chain
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


def market(*, when: datetime = NOW, price="27000") -> ReplayMarketStateProvider:
    return ReplayMarketStateProvider(
        [
            MarketObservation(
                symbol=INSTRUMENT.symbol,
                price=Decimal(price),
                market_event_ts=when,
                local_ingest_ts=when,
                market_reference=f"mkt:{when.isoformat()}",
                sequence=1,
            )
        ]
    )


def make_writer(
    journal,
    state_dir,
    responses,
    *,
    now: datetime = NOW,
    market_provider=None,
    account_id: str = ACCOUNT,
    max_account_age=5.0,
    max_mark_age=5.0,
):
    transport = fx.ScriptedTransport(responses)
    client = fx.client(transport)
    control = control_for(journal, state_dir, when=now)
    writer = AccountEquityBaselineWriter(
        journal=journal,
        control=control,
        client=client,
        market_state=market_provider or market(when=now),
        instrument=INSTRUMENT,
        environment=ENV,
        venue_account_id=account_id,
        max_account_age_seconds=max_account_age,
        max_mark_age_seconds=max_mark_age,
        clock=lambda: now,
    )
    return writer, transport, control


def happy_responses(*, account=None, after_open=None):
    return [
        fx.time_response(),
        fx.open_orders_response(),
        account or fx.account_response(),
        after_open or fx.open_orders_response(),
    ]


def open_attempt() -> OrderSubmissionAttempted:
    return OrderSubmissionAttempted(
        event_id=UUID("a0000000-0000-4000-8000-000000000001"),
        occurred_at=NOW - timedelta(seconds=1),
        correlation_id=UUID("a0000000-0000-4000-8000-000000000002"),
        producer="application.production",
        strategy_id="strategy.baseline-test",
        order_id=UUID("a0000000-0000-4000-8000-000000000003"),
        symbol=INSTRUMENT.symbol,
        payload=OrderSubmissionAttemptPayload(
            venue_reference="ct-baseline-open",
            intent_id=UUID("a0000000-0000-4000-8000-000000000004"),
            side="buy",
            order_type="limit",
            quantity=Decimal("0.001"),
            limit_price=Decimal("27000"),
            time_in_force="gtc",
            venue_producer="infrastructure.binance_testnet",
        ),
    )


def test_baseline_writer_brackets_account_snapshot_and_commits_one_root(
    journal, state_dir,
):
    writer, transport, control = make_writer(journal, state_dir, happy_responses())

    report = writer.create()

    assert journal.count() == 1
    assert report.fence_term == 1
    assert report.venue_account_id == ACCOUNT
    assert report.equity_quote == Decimal("127000")
    chain = load_equity_chain(
        journal,
        symbol=INSTRUMENT.symbol,
        environment=ENV,
        venue_account_id=ACCOUNT,
        base_asset=INSTRUMENT.base_currency,
        quote_asset=INSTRUMENT.quote_currency,
    )
    assert chain.root_base_balance == Decimal("1.0")
    assert chain.root_quote_balance == Decimal("100000")
    assert chain.root_mark_price == Decimal("27000")
    assert chain.baseline_event_id == report.event_id
    paths = [request.url for request in transport.requests]
    assert sum("/api/v3/openOrders" in url for url in paths) == 2
    assert sum("/api/v3/account" in url for url in paths) == 1
    assert control.ownership_store.require_fence().owner_id == "normal:equity-baseline-maintenance"


def test_existing_equity_chain_refuses_before_any_network_call(journal, state_dir):
    first, _, _ = make_writer(journal, state_dir, happy_responses())
    first.create()
    second, transport, _ = make_writer(journal, state_dir, [])

    with pytest.raises(ProductionStateError, match="already exists"):
        second.create()

    assert transport.requests == []
    assert journal.count() == 1


def test_local_open_order_refuses_before_claim_or_network(journal, state_dir):
    journal.append(open_attempt())
    writer, transport, control = make_writer(journal, state_dir, [])

    with pytest.raises(ProductionStateError, match="zero open orders"):
        writer.create()

    assert transport.requests == []
    assert control.ownership_store.read() is None
    assert journal.count() == 1


def test_venue_open_order_before_snapshot_refuses_without_account_read(journal, state_dir):
    responses = [
        fx.time_response(),
        fx.open_orders_response("ct-external-open"),
    ]
    writer, transport, _ = make_writer(journal, state_dir, responses)

    with pytest.raises(ProductionStateError, match="zero authoritative venue open orders"):
        writer.create()

    assert not any("/api/v3/account" in request.url for request in transport.requests)
    assert journal.count() == 0


def test_venue_open_order_appearing_after_snapshot_refuses(journal, state_dir):
    responses = happy_responses(after_open=fx.open_orders_response("ct-raced-open"))
    writer, _, _ = make_writer(journal, state_dir, responses)

    with pytest.raises(ProductionStateError, match="after snapshot"):
        writer.create()

    assert journal.count() == 0


def test_configured_account_identity_is_required_and_must_match(journal, state_dir):
    response = fx.account_response()
    response.body["uid"] = 987654321
    writer, _, _ = make_writer(journal, state_dir, happy_responses(account=response))

    with pytest.raises(ProductionStateError, match="account id does not match"):
        writer.create()

    assert journal.count() == 0


def test_stale_account_evidence_refuses(journal, state_dir):
    now = NOW + timedelta(seconds=10)
    writer, _, _ = make_writer(
        journal,
        state_dir,
        happy_responses(),
        now=now,
        market_provider=market(when=now),
        max_account_age=5,
    )

    with pytest.raises(ProductionStateError, match="account evidence is stale"):
        writer.create()

    assert journal.count() == 0


def test_stale_and_future_market_evidence_refuse(journal, state_dir):
    stale_writer, _, _ = make_writer(
        journal,
        state_dir,
        happy_responses(),
        now=NOW + timedelta(seconds=10),
        market_provider=market(when=NOW),
        max_account_age=20,
        max_mark_age=5,
    )
    with pytest.raises(ProductionStateError, match="mark evidence is stale"):
        stale_writer.create()
    assert journal.count() == 0

    # A failed post-claim maintenance attempt intentionally leaves its fence durable. Exercise
    # the independent future-mark case with a separate ownership-store fixture.
    future_state_dir = state_dir.parent / "future-state"
    future_state_dir.mkdir(parents=True, exist_ok=True)

    future_writer, _, _ = make_writer(
        journal,
        future_state_dir,
        happy_responses(),
        now=NOW,
        market_provider=market(when=NOW + timedelta(seconds=1)),
    )
    with pytest.raises(ProductionStateError, match="mark timestamp is in the future"):
        future_writer.create()
    assert journal.count() == 0
