"""Phase 11: order runtime, ambiguity, restart recovery, reconciliation and safety controls."""

from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from crypto_trader.events import (
    EXECUTION_FILL_EVENT_TYPE,
    ORDER_STATE_EVENT_TYPE,
    ORDER_SUBMISSION_ATTEMPTED_EVENT_TYPE,
)
from crypto_trader.execution import (
    ExecutionOrderType,
    ExecutionRequest,
    ExecutionRuntimeState,
    ExecutionSide,
    InstrumentSpec,
)
from crypto_trader.execution.binance_testnet import (
    AmbiguousOrderError,
    RateLimitBanError,
    ScriptedTransport,
    TransportTimeout,
)
from crypto_trader.execution.binance_testnet import (
    TestnetOrderRuntime as OrderRuntime,
)
from crypto_trader.execution.binance_testnet import BinanceTestnetError as AdapterError
from crypto_trader.execution.binance_testnet import (
    RateLimitError as RateLimited,
    TestnetRejectedError as RejectedError,
)
from crypto_trader.persistence import SQLiteEventJournal
from crypto_trader.recovery import (
    ReconciliationEngine,
    ReconciliationOutcome,
    load_policy,
)
from crypto_trader.safety import SafetyController, SafetyMode
from crypto_trader.tests import testnet_fixtures as fx

INSTRUMENT = InstrumentSpec(symbol="BTCUSDT", base_currency="BTC", starting_balance="100000 USDT")
INTENT_ID = UUID("66666666-6666-4666-8666-666666666666")
CORRELATION_ID = UUID("77777777-7777-4777-8777-777777777777")
CLIENT_ORDER_ID = f"ct-{INTENT_ID.hex[:20]}"


@pytest.fixture
def journal(tmp_path: Path):
    with SQLiteEventJournal(tmp_path / "journal.db") as instance:
        yield instance


def execution_request(*, quantity: str = "0.00100000", price: str = "27000.00") -> ExecutionRequest:
    return ExecutionRequest.create(
        correlation_id=CORRELATION_ID,
        symbol="BTCUSDT",
        side=ExecutionSide.BUY,
        order_type=ExecutionOrderType.LIMIT,
        quantity=Decimal(quantity),
        limit_price=Decimal(price),
        intent_id=INTENT_ID,
    )


def runtime(transport: ScriptedTransport, *, journal=None, safety=None) -> OrderRuntime:
    client = fx.client(transport)
    client.clock.synchronize(fx.SERVER_TIME_MS)
    return OrderRuntime(
        client=client, instrument=INSTRUMENT, journal=journal, safety=safety
    )


def started_runtime(transport, *, journal=None, safety=None) -> OrderRuntime:
    instance = runtime(transport, journal=journal, safety=safety)
    instance.start()
    return instance


# --- lifecycle -------------------------------------------------------------

def test_start_verifies_the_testnet_environment():
    transport = ScriptedTransport(responses=fx.environment_responses())
    instance = runtime(transport)

    instance.start()

    assert instance.state() is ExecutionRuntimeState.RUNNING
    assert fx.request_paths(transport) == [
        "/api/v3/time",
        "/api/v3/exchangeInfo",
        "/api/v3/account",
    ]
    instance.stop()
    assert instance.state() is ExecutionRuntimeState.STOPPED


def test_submit_places_an_order_and_journals_lifecycle_events(journal):
    transport = ScriptedTransport(
        responses=[
            *fx.environment_responses(),
            fx.order_response(
                CLIENT_ORDER_ID,
                status="FILLED",
                executed_qty="0.00100000",
                fills=(fx.fill(qty="0.00100000"),),
            ),
        ]
    )
    instance = started_runtime(transport, journal=journal)

    reference = instance.submit(execution_request())

    assert reference == CLIENT_ORDER_ID
    assert instance.local_states()[0].intent_id == INTENT_ID
    observations = instance.observations()
    assert observations[-1].intent_id == INTENT_ID
    assert observations[-1].correlation_id == CORRELATION_ID
    assert observations[-1].state.value == "filled"
    types = {event.event_type for event in journal.read_range()}
    assert {ORDER_STATE_EVENT_TYPE, EXECUTION_FILL_EVENT_TYPE} <= types
    fill_events = [
        event for event in journal.read_range() if event.event_type == EXECUTION_FILL_EVENT_TYPE
    ]
    assert fill_events[0].order_id is not None
    assert fill_events[0].correlation_id == CORRELATION_ID
    assert fill_events[0].payload.quantity == Decimal("0.00100000")


def test_client_order_id_is_deterministic_across_restarts():
    first = runtime(ScriptedTransport(responses=[]))
    second = runtime(ScriptedTransport(responses=[]))

    assert first.client_order_id_for(INTENT_ID) == second.client_order_id_for(INTENT_ID)
    assert first.client_order_id_for(INTENT_ID) == CLIENT_ORDER_ID


def test_submit_requires_a_started_runtime():
    instance = runtime(ScriptedTransport(responses=[fx.exchange_info_response()]))

    with pytest.raises(Exception):
        instance.submit(execution_request())


def test_submit_validates_the_request_before_touching_the_network():
    instance = started_runtime(ScriptedTransport(responses=fx.environment_responses()))

    with pytest.raises(Exception):
        instance.submit(execution_request(quantity="0"))


# --- safety ---------------------------------------------------------------

def test_halt_blocks_new_orders_without_contacting_the_venue():
    safety = SafetyController(correlation_id=CORRELATION_ID)
    safety.set_mode(SafetyMode.HALT_NEW_RISK, reason_code="operator_halt", reason="halted")
    transport = ScriptedTransport(responses=fx.environment_responses())
    instance = started_runtime(transport, safety=safety)

    with pytest.raises(AdapterError):
        instance.submit(execution_request())

    assert fx.request_paths(transport) == ["/api/v3/time", "/api/v3/exchangeInfo", "/api/v3/account"]


def test_cancel_all_is_the_safety_lane_action(journal):
    transport = ScriptedTransport(
        responses=[
            *fx.environment_responses(),
            fx.open_orders_response("ct-a", "ct-b"),
            fx.cancel_response("ct-a", order_id=100),
            fx.cancel_response("ct-b", order_id=101),
        ]
    )
    instance = started_runtime(transport, journal=journal)

    results = instance.cancel_all()

    assert [order.state.value for order in results] == ["canceled", "canceled"]
    cancel_events = [
        event
        for event in journal.read_range()
        if event.event_type == ORDER_STATE_EVENT_TYPE and event.payload.state == "canceled"
    ]
    assert len(cancel_events) == 2


# --- ambiguity ------------------------------------------------------------

def test_ambiguous_submission_is_marked_pending_and_not_marked_failed(journal):
    transport = ScriptedTransport(
        responses=[*fx.environment_responses(), TransportTimeout("no response")]
    )
    instance = started_runtime(transport, journal=journal)

    with pytest.raises(AmbiguousOrderError):
        instance.submit(execution_request())

    states = instance.local_states()
    assert len(states) == 1
    assert states[0].order_reference == CLIENT_ORDER_ID
    assert states[0].submission_attempted is True
    assert states[0].state.value == "submitted"
    assert instance.observations() == ()
    # The durable attempt marker is committed before the write, not merely held in memory.
    attempts = [
        event
        for event in journal.read_range()
        if event.event_type == ORDER_SUBMISSION_ATTEMPTED_EVENT_TYPE
    ]
    assert len(attempts) == 1
    assert attempts[0].payload.venue_reference == CLIENT_ORDER_ID


def test_reconciliation_resolves_an_ambiguous_submission_from_venue_evidence(journal):
    transport = ScriptedTransport(
        responses=[
            *fx.environment_responses(),
            TransportTimeout("no response"),  # the ambiguous submit
            fx.order_response(CLIENT_ORDER_ID, status="NEW"),  # the venue told us the truth
        ]
    )
    instance = started_runtime(transport, journal=journal)
    with pytest.raises(AmbiguousOrderError):
        instance.submit(execution_request())

    engine = ReconciliationEngine(load_policy({"policy_id": "recovery-v1"}))
    report = engine.reconcile(
        instance.local_states(), instance.venue_state_source().reports()
    )

    # The venue accepted the order, so the local SUBMITTED view is corrected from evidence.
    assert report.outcome_for(CLIENT_ORDER_ID) is ReconciliationOutcome.RESOLVED_FROM_VENUE
    assert report.requires_halt_new_risk is False
    assert report.adjustments[0].after.state.value == "accepted"


def test_ambiguous_submission_stays_pending_when_the_venue_has_no_record(journal):
    transport = ScriptedTransport(
        responses=[
            *fx.environment_responses(),
            TransportTimeout("no response"),
            fx.rejected(code=-2013, msg="Order does not exist."),  # the venue view: nothing there
        ]
    )
    instance = started_runtime(transport, journal=journal)
    with pytest.raises(AmbiguousOrderError):
        instance.submit(execution_request())

    engine = ReconciliationEngine(load_policy({"policy_id": "recovery-v1"}))
    venue_reports = instance.venue_state_source().reports()
    report = engine.reconcile(instance.local_states(), venue_reports)

    # No venue evidence exists, so the submission stays ambiguous - never silently failed.
    assert venue_reports == ()
    assert report.outcome_for(CLIENT_ORDER_ID) is (
        ReconciliationOutcome.AMBIGUOUS_PENDING_RECONCILIATION
    )
    assert report.adjustments == []
    assert report.requires_halt_new_risk is True


# --- restart --------------------------------------------------------------

def test_restart_adopts_an_existing_order_without_resending_it(journal):
    """A crash after a successful submit must not place a second order."""
    transport = ScriptedTransport(
        responses=[
            *fx.environment_responses(),
            fx.order_response(CLIENT_ORDER_ID, status="NEW"),  # the recovery lookup
        ]
    )
    restarted = started_runtime(transport, journal=journal)

    adopted = restarted.recover_order(INTENT_ID, correlation_id=CORRELATION_ID)

    assert adopted is not None
    assert adopted.client_order_id == CLIENT_ORDER_ID
    assert adopted.state.value == "accepted"
    posts = [item for item in transport.requests if item.method == "POST"]
    assert posts == [], "recovery must never re-send the order"
    assert restarted.observations()[-1].intent_id == INTENT_ID


def test_restart_leaves_a_truly_ambiguous_order_pending():
    transport = ScriptedTransport(
        responses=[
            *fx.environment_responses(),
            fx.rejected(code=-2013, msg="Order does not exist."),
            fx.rejected(code=-2013, msg="Order does not exist."),
        ]
    )
    restarted = started_runtime(transport)

    recovered = restarted.recover_order(INTENT_ID, correlation_id=CORRELATION_ID)

    assert recovered is None
    states = restarted.local_states()
    assert states[0].submission_attempted is True
    assert states[0].state.value == "submitted"
    assert restarted.venue_state_source().reports() == ()


def test_submitting_twice_with_the_same_intent_never_creates_two_orders(journal):
    transport = ScriptedTransport(
        responses=[
            *fx.environment_responses(),
            fx.order_response(CLIENT_ORDER_ID, status="NEW"),
            fx.order_response(CLIENT_ORDER_ID, status="NEW"),  # the idempotent re-read
        ]
    )
    instance = started_runtime(transport, journal=journal)

    first = instance.submit(execution_request())
    second = instance.submit(execution_request())

    assert first == second == CLIENT_ORDER_ID
    posts = [item for item in transport.requests if item.method == "POST"]
    assert len(posts) == 1


# --- reconnect / rate limits ----------------------------------------------

def test_reconnect_after_an_outage_reverifies_the_environment():
    transport = ScriptedTransport(
        responses=[
            *fx.environment_responses(),
            fx.time_response(),
            fx.exchange_info_response(),
            fx.account_response(),
        ]
    )
    instance = started_runtime(transport)

    environment = instance.client.reconnect()

    assert environment.account.can_trade is True
    assert fx.request_paths(transport).count("/api/v3/time") == 2


def test_ip_ban_during_a_submit_surfaces_and_does_not_retry(journal):
    transport = ScriptedTransport(
        responses=[*fx.environment_responses(), fx.banned("120")]
    )
    instance = started_runtime(transport, journal=journal)

    with pytest.raises(RateLimitBanError):
        instance.submit(execution_request())

    assert len([item for item in transport.requests if item.method == "POST"]) == 1


def test_rate_limited_write_is_reported_without_retry(journal):
    transport = ScriptedTransport(
        responses=[*fx.environment_responses(), fx.rate_limited("1")]
    )
    instance = started_runtime(transport, journal=journal)

    with pytest.raises(RateLimited):
        instance.submit(execution_request())

    assert len([item for item in transport.requests if item.method == "POST"]) == 1


def test_rejected_order_is_not_recorded_as_pending(journal):
    transport = ScriptedTransport(
        responses=[*fx.environment_responses(), fx.rejected(code=-2010, msg="insufficient balance")]
    )
    instance = started_runtime(transport, journal=journal)

    with pytest.raises(RejectedError):
        instance.submit(execution_request())

    assert instance.local_states() == ()
    # A deterministic venue rejection is recorded as a terminal resolution for the durable attempt.
    rejected = [
        event
        for event in journal.read_range()
        if event.event_type == ORDER_STATE_EVENT_TYPE and event.payload.state == "rejected"
    ]
    assert len(rejected) == 1
