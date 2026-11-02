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
    # Historical A2-L test identity retained for traceability. R-9 supersedes the
    # former client-reference-only match: a legacy ct- key may have been reused
    # after a fill and cannot establish which venue order a lost-ACK attempt created.
    from crypto_trader.production import ProductionRecoveryRequiredError

    with SQLiteEventJournal(tmp_path / "journal.db", venue_writes=True) as journal:
        with pytest.raises(ProductionRecoveryRequiredError, match="legacy order identity"):
            _running_with_our_open_order(journal, tmp_path, identity_journaled=False)
        # Do not fabricate a recovered identity or release a reservation.
        assert not any(event.event_type == "recovery.state.changed" for event in journal.read_range())


# --- Review F4: new risk waits until our order's reported execution is proven by its fills ---------


def _running_after_a_journaled_execution(journal, tmp_path, *, state, fills, listing):
    """Our order with its attempt and identity, then a venue state journaled with `fills` only."""
    seed_canonical_equity_root(journal)
    journal.append(
        attempts.attempt_event(quantity="0.001", limit_price="27000", intent_id=OUR_INTENT)
        .model_copy(update={"symbol": INSTRUMENT.symbol})
    )
    journal.append(attempts.identity_event("100", index=0, symbol=INSTRUMENT.symbol, intent_id=OUR_INTENT))
    journal.append(attempts.state_event(state, index=1).model_copy(update={"symbol": INSTRUMENT.symbol}))
    for quantity in fills:
        journal.append(
            attempts.fill_event(quantity, index=1, remaining="0").model_copy(update={"symbol": INSTRUMENT.symbol})
        )
    startup = startup_responses()
    startup[1] = listing()
    transport = fx.ScriptedTransport(startup)
    runtime, port = production_on_testnet(
        journal=journal, state_dir=tmp_path / "state", client=fx.client(transport, retries=1)
    )
    runtime.start()
    return runtime, transport


def _our_partial_listing():
    response = fx.open_orders_response(OUR_REFERENCE, status="PARTIALLY_FILLED", executed_qty="0.0005")
    response.body[0]["orderId"] = 100
    return response


@pytest.mark.parametrize(
    ("state", "listing"),
    [
        pytest.param("filled", fx.open_orders_response, id="filled_without_its_fill"),
        pytest.param("partially_filled", _our_partial_listing, id="partially_filled_without_a_fill"),
    ],
)
def test_an_execution_not_proven_by_journaled_fills_admits_no_new_risk(tmp_path, state, listing):
    # Review F4 read guard: a journal (also one written before the write guard) holding our order as
    # filled, or partially filled, without the fills of that execution must not admit new risk even
    # when the venue balances match the journal.
    with SQLiteEventJournal(tmp_path / "journal.db", venue_writes=True) as journal:
        runtime, transport = _running_after_a_journaled_execution(
            journal, tmp_path, state=state, fills=(), listing=listing
        )
        responses = canonical_risk_refresh_responses()
        responses[1] = listing()
        responses[4] = listing()
        transport.responses.extend(responses)

        with pytest.raises(ProductionStateError, match="not proven by its journaled fills"):
            admitted_prepare(runtime, intent("f4-new-risk", quantity=Decimal("0.001")))

        assert [request for request in transport.requests if request.method == "POST"] == []


def test_an_execution_proven_by_its_journaled_fill_still_admits_new_risk(tmp_path):
    # Review F4 guard: the same order with the fill of its whole execution counts as before.
    with SQLiteEventJournal(tmp_path / "journal.db", venue_writes=True) as journal:
        runtime, transport = _running_after_a_journaled_execution(
            journal, tmp_path, state="filled", fills=("0.001",), listing=fx.open_orders_response
        )
        responses = canonical_risk_refresh_responses()
        for balance in responses[2].body["balances"]:
            if balance["asset"] == "BTC":
                balance["free"] = "0.001"
            elif balance["asset"] == "USDT":
                balance["free"] = "99999.9"
        transport.responses.extend(responses)

        assert admitted_prepare(runtime, intent("f4-new-risk", quantity=Decimal("0.001"))).approved


@pytest.mark.parametrize("age_field", ("max_account_age_seconds", "max_mark_age_seconds"))
@pytest.mark.parametrize(
    "invalid_age",
    [
        pytest.param(float("nan"), id="nan"),
        pytest.param(float("inf"), id="positive_infinity"),
        pytest.param(float("-inf"), id="negative_infinity"),
        pytest.param(0.0, id="zero"),
        pytest.param(-1.0, id="negative"),
        pytest.param(True, id="boolean"),
    ],
)
def test_risk_source_finite_age_limit_refuses_invalid_before_network(
    journal, state_dir, age_field, invalid_age,
):
    limits = {"max_account_age_seconds": 5.0, "max_mark_age_seconds": 5.0}
    limits[age_field] = invalid_age
    client = fx.client(fx.ScriptedTransport([]))
    control = control_for(journal, state_dir, when=NOW)
    with pytest.raises(ProductionStateError, match="must be a positive number"):
        ProductionRiskStateSource(
            journal=journal,
            control=control,
            client=client,
            instrument=INSTRUMENT,
            environment=ENV,
            venue_account_id=ACCOUNT,
            **limits,
        )
    assert journal.count() == 0


@pytest.mark.parametrize("age_field", ("max_account_age_seconds", "max_mark_age_seconds"))
@pytest.mark.parametrize(
    "invalid_age",
    [
        pytest.param(float("nan"), id="nan"),
        pytest.param(float("inf"), id="positive_infinity"),
    ],
)
def test_derived_risk_finite_age_limit_rejects_before_evidence_dereference(
    age_field, invalid_age,
):
    from crypto_trader.production.risk_state import derive_risk_state

    limits = {"max_account_age_seconds": 5.0, "max_mark_age_seconds": 5.0}
    limits[age_field] = invalid_age
    with pytest.raises(ProductionStateError, match="must be a positive number"):
        derive_risk_state(
            (), chain=object(), account=object(), mark=object(),
            instrument=INSTRUMENT, now=NOW, **limits,
        )


# Item 5: venue-E offline source assertions (SENSITIVE, approved)
from crypto_trader.execution.binance_testnet.market_data import BinanceTestnetTickerQuoteSource
from crypto_trader.tests.test_r03a1b_testnet_ticker_quote import FakeSocket, ticker


def _venue_e_risk_source(journal, control, responses, *, mark_source):
    transport = fx.ScriptedTransport(responses)
    client = fx.client(transport)
    return ProductionRiskStateSource(
        journal=journal, control=control, client=client,
        instrument=INSTRUMENT, environment=ENV, venue_account_id=ACCOUNT,
        max_account_age_seconds=5, max_mark_age_seconds=5,
        mark_source=mark_source,
    ), transport


def event_ticker(*, at=NOW, symbol="BTCUSDT", bid="26999.00", ask="27001.00"):
    frame = ticker(E=int(at.timestamp() * 1000), s=symbol, b=bid, a=ask)
    sock = FakeSocket(frame)
    trusted = BinanceTestnetTickerQuoteSource(
        symbol=INSTRUMENT.symbol, clock=lambda: NOW,
        max_event_age=timedelta(seconds=5), max_spread_bps=Decimal("100"),
        connect=lambda _url, _timeout: sock,
    )
    return trusted, sock


def refresh_without_untimed_price():
    return [
        fx.time_response(fx.SERVER_TIME_MS + 1),
        fx.open_orders_response(), fx.account_response(), fx.open_orders_response(),
    ]


def test_canonical_risk_accepts_actual_venue_E_and_refuses_REST_mark_fallback(journal, state_dir):
    control, root = seed_baseline(journal, state_dir)
    guard = control.claim_normal_writer(owner_id="normal:runtime", reason_code="test_runtime")
    mark_source, socket = event_ticker()
    risk_source, transport = _venue_e_risk_source(
        journal, control, refresh_without_untimed_price(), mark_source=mark_source,
    )

    snapshot, checked_at = risk_source(NOW, guard=guard)

    assert checked_at == NOW + timedelta(milliseconds=1)
    assert snapshot.equity_quote == root.equity_quote
    assert socket.closed and socket.read_count == 1
    assert len(transport.requests) == 4
    assert all(request.method == "GET" for request in transport.requests)
    assert journal.count() == 1


def test_canonical_risk_peak_persists_market_venue_E_not_receipt_clock(journal, state_dir):
    control, root = seed_baseline(journal, state_dir)
    guard = control.claim_normal_writer(owner_id="normal:runtime", reason_code="test_runtime")
    mark_source, socket = event_ticker(bid="27999.00", ask="28001.00")
    risk_source, _transport = _venue_e_risk_source(
        journal, control, refresh_without_untimed_price(), mark_source=mark_source,
    )

    snapshot, _checked_at = risk_source(NOW, guard=guard)

    assert snapshot.equity_quote == Decimal("128000")
    events = journal.read_range()
    assert len(events) == 2 and events[-1].payload.kind == "peak"
    assert events[-1].payload.mark_event_ts == NOW
    assert events[-1].payload.mark_reference.startswith(
        "binance-spot-testnet:ticker:BTCUSDT:"
    )
    assert socket.closed and socket.read_count == 1


def test_failed_venue_E_risk_mark_fails_closed_without_REST_retry(journal, state_dir):
    control, _root = seed_baseline(journal, state_dir)
    guard = control.claim_normal_writer(owner_id="normal:runtime", reason_code="test_runtime")
    mark_source, socket = event_ticker(at=NOW - timedelta(seconds=6))
    risk_source, transport = _venue_e_risk_source(
        journal, control, refresh_without_untimed_price(), mark_source=mark_source,
    )

    with pytest.raises(ProductionStateError, match="venue risk evidence refresh failed"):
        risk_source(NOW, guard=guard)
    assert socket.closed and socket.read_count == 1
    assert len(transport.requests) == 3
    assert journal.count() == 1


def test_mismatched_or_untrusted_risk_mark_source_rejected_before_network(journal, state_dir):
    control, _root = seed_baseline(journal, state_dir)
    bad = BinanceTestnetTickerQuoteSource(
        symbol="ETHUSDT", clock=lambda: NOW,
        max_event_age=timedelta(seconds=5), max_spread_bps=Decimal("100"),
        connect=lambda _url, _timeout: FakeSocket(ticker()),
    )
    for untrusted in (bad, object()):
        with pytest.raises(ProductionStateError, match="exact symbol-pinned"):
            _venue_e_risk_source(journal, control, [], mark_source=untrusted)
