"""G5b canonical live production risk-state source tests."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import pytest

from crypto_trader.persistence import SQLiteEventJournal
from crypto_trader.production import ProductionStateError
from crypto_trader.production.equity import AccountEquityBaselineWriter, load_equity_chain
from crypto_trader.production.risk_state import ProductionRiskStateSource
from crypto_trader.tests import testnet_fixtures as fx
from crypto_trader.tests import test_recovery_submission_attempt as attempts
from crypto_trader.tests.production_fixtures import (
    INSTRUMENT,
    admitted_prepare,
    admitted_submit,
    canonical_risk_refresh_responses,
    control_for,
    intent,
    seed_canonical_equity_root,
    startup_responses,
    testnet_runtime as production_on_testnet,
)
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


# --- Review A-2: the risk refresh admits new risk only on our journaled venue order id ----------

OUR_REFERENCE = attempts.CLIENT_ORDER_ID
OUR_INTENT = UUID("44444444-4444-4444-4444-444444444444")


def _listing_naming_our_reference(venue_order_id):
    response = fx.open_orders_response(OUR_REFERENCE)
    response.body[0]["orderId"] = venue_order_id
    return response


def _running_with_our_open_order(journal, tmp_path, *, identity_journaled=True):
    """Our open order: its write attempt, and (normally) the venue order id 100 that answered it."""
    seed_canonical_equity_root(journal)
    journal.append(
        attempts.attempt_event(quantity="0.001", limit_price="27000", intent_id=OUR_INTENT)
        .model_copy(update={"symbol": INSTRUMENT.symbol})
    )
    if identity_journaled:
        journal.append(
            attempts.identity_event("100", index=0, symbol=INSTRUMENT.symbol, intent_id=OUR_INTENT)
        )
    startup = startup_responses()
    startup[1] = _listing_naming_our_reference(100)
    transport = fx.ScriptedTransport(startup)
    runtime, port = production_on_testnet(
        journal=journal, state_dir=tmp_path / "state", client=fx.client(transport, retries=1)
    )
    runtime.start()
    return runtime, port, transport


def _risk_refresh_listing(before, after):
    responses = canonical_risk_refresh_responses()
    responses[1] = _listing_naming_our_reference(before)
    responses[4] = _listing_naming_our_reference(after)
    return responses


@pytest.mark.parametrize(
    ("before", "after", "refusal"),
    [
        (777, 777, "identity disagrees with durable venue identity"),
        (100, 777, "open orders changed"),
        (777, 100, "open orders changed"),
    ],
)
def test_a_listed_order_under_another_venue_id_admits_no_new_risk(
    tmp_path, before, after, refusal
):
    # Review A-2 (GPT-6 Astra; adjudicated High and blocking by the GPT-6.1 Sol High adjudication):
    # the venue lists our client order id under venue order 777 while the journal bound it to 100.
    # Matching on the client id, quantity, side and price alone admitted the new intent and POSTed it.
    # 777 then 100 is refused only by the before/after comparison: the listing the snapshot is derived
    # from shows our id, but another order held our client id while the evidence was read.
    with SQLiteEventJournal(tmp_path / "journal.db", venue_writes=True) as journal:
        runtime, _, transport = _running_with_our_open_order(journal, tmp_path)
        transport.responses.extend(_risk_refresh_listing(before, after))
        records = journal.count()

        with pytest.raises(ProductionStateError, match=refusal):
            admitted_prepare(runtime, intent("a2-new-risk", quantity=Decimal("0.001")))

        assert [request for request in transport.requests if request.method == "POST"] == []
        assert journal.count() == records


def test_our_listed_order_under_our_venue_id_still_admits_new_risk(tmp_path):
    # Review A-2 guard: our journaled venue order id in both listings is not a refusal.
    with SQLiteEventJournal(tmp_path / "journal.db", venue_writes=True) as journal:
        runtime, port, transport = _running_with_our_open_order(journal, tmp_path)
        transport.responses.extend(_risk_refresh_listing(100, 100))
        new = intent("a2-new-risk", quantity=Decimal("0.001"))

        prepared = admitted_prepare(runtime, new)
        assert prepared.approved
        new_reference = port.client_order_id_for(new.intent_id)
        transport.responses.append(
            fx.order_response(new_reference, order_id=888, transact_time=fx.SERVER_TIME_MS + 2)
        )

        assert admitted_submit(runtime, prepared).order_reference == new_reference
        assert len([request for request in transport.requests if request.method == "POST"]) == 1


def test_an_open_order_of_unknown_venue_identity_is_matched_as_before(tmp_path):
    # Review A-2, option A2-L (owner's decision; residual recorded with R-9): with no journaled venue
    # order id (a crash after the POST, or a journal written before item 11) there is nothing to
    # compare, so the listing is matched on client id, quantity, side and price as before. The strict
    # alternative refuses the protected post-crash restart flow (review R-03).
    with SQLiteEventJournal(tmp_path / "journal.db", venue_writes=True) as journal:
        runtime, _, transport = _running_with_our_open_order(
            journal, tmp_path, identity_journaled=False
        )
        transport.responses.extend(_risk_refresh_listing(777, 777))

        assert admitted_prepare(runtime, intent("a2-new-risk", quantity=Decimal("0.001"))).approved
