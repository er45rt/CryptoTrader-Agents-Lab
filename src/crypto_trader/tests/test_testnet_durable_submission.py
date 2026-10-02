"""Phase 15 durable ambiguity, Stage 2: durable attempt marker + transport fault semantics.

Every scenario drives the REAL runtime -> client -> transport submission path; faults are injected
at the transport boundary (or by a stub journal), never by a synthetic helper that bypasses the
path. No network, no credentials, no live order is involved.
"""

from __future__ import annotations

import ast
import tempfile
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from crypto_trader.events import (
    ORDER_STATE_EVENT_TYPE,
    ORDER_SUBMISSION_ATTEMPTED_EVENT_TYPE,
)
from crypto_trader.execution import (
    ExecutionOrderType,
    ExecutionRequest,
    ExecutionSide,
    InstrumentSpec,
)
from crypto_trader.execution.binance_testnet import (
    AmbiguousOrderError,
    AttemptJournalRequiredError,
    BinanceSpotTestnetClient,
    PreSendTransportError,
    RateLimitBanError,
    RateLimitError as RateLimited,
    RequestsTransport,
    ScriptedTransport,
    TransportError,
    TransportTimeout,
    VenueProtocolRejectionError,
    WithdrawalPolicy,
)
from crypto_trader.execution.binance_testnet import TestnetSymbolError as SymbolError
from crypto_trader.execution.binance_testnet import TestnetRejectedError as RejectedError
from crypto_trader.execution.binance_testnet import deterministic_order_uuid
from crypto_trader.execution.binance_testnet import TestnetOrderRuntime as OrderRuntime
from crypto_trader.execution.binance_testnet.errors import ClockDriftError
from crypto_trader.execution.binance_testnet import HttpRequest
from crypto_trader.persistence import SQLiteEventJournal
from crypto_trader.recovery import (
    ReconciliationEngine,
    ReconciliationOutcome,
    RestartRecovery,
    StaticVenueStateSource,
    load_policy,
    reconstruct_order_states,
)
from crypto_trader.tests import testnet_fixtures as fx

INSTRUMENT = InstrumentSpec(symbol="BTCUSDT", base_currency="BTC", starting_balance="100000 USDT")
INTENT_ID = UUID("88888888-8888-4888-8888-888888888888")
CORRELATION_ID = UUID("99999999-9999-4999-8999-999999999999")
CLIENT_ORDER_ID = f"ct-{INTENT_ID.hex[:20]}"


def execution_request(*, quantity: str = "0.00100000") -> ExecutionRequest:
    return ExecutionRequest.create(
        correlation_id=CORRELATION_ID,
        symbol="BTCUSDT",
        side=ExecutionSide.BUY,
        order_type=ExecutionOrderType.LIMIT,
        quantity=Decimal(quantity),
        limit_price=Decimal("27000.00"),
        intent_id=INTENT_ID,
    )


def client_for(transport, *, synchronize: bool = True) -> BinanceSpotTestnetClient:
    client = fx.client(transport)
    if synchronize:
        client.clock.synchronize(fx.SERVER_TIME_MS)
    return client


def started(transport, journal, *, synchronize: bool = True) -> OrderRuntime:
    instance = OrderRuntime(
        client=client_for(transport, synchronize=synchronize),
        instrument=INSTRUMENT,
        journal=journal,
    )
    instance.start()
    return instance


def posts(transport) -> list:
    return [item for item in transport.requests if item.method == "POST"]


def attempt_events(journal) -> list:
    return [
        event for event in journal.read_range() if event.event_type == ORDER_SUBMISSION_ATTEMPTED_EVENT_TYPE
    ]


class _FailingJournal:
    """A journal whose durable append fails (marker commit unavailable)."""

    def read_range(self, **_kwargs):
        return []

    def read_by_id(self, _event_id):
        return None

    def append(self, event):  # noqa: ANN001 - test double
        raise RuntimeError("durable journal unavailable")


class _UnreadableAttemptJournal:
    """Structural durable journal whose attempt history cannot be queried."""

    def append(self, _event):
        return True

    def read_by_id(self, _event_id):
        return None


class _MarkerVisibilityTransport:
    """Fault transport that proves the marker is committed at the instant of the POST."""

    def __init__(self, journal_path: Path, fault: Exception, inner) -> None:  # noqa: ANN001
        self._journal_path = journal_path
        self._fault = fault
        self._inner = inner
        self.seen_during_send: list = []
        self.requests: list[HttpRequest] = []

    def send(self, request: HttpRequest):
        self.requests.append(request)
        if not request.is_write:
            # Reads (environment verification) are delegated to the deterministic double.
            return self._inner.send(request)
        with SQLiteEventJournal(self._journal_path) as fresh:
            self.seen_during_send = [
                event
                for event in fresh.replay()
                if event.event_type == ORDER_SUBMISSION_ATTEMPTED_EVENT_TYPE
            ]
        raise self._fault


# --- A / B: locally rejectable work happens before the marker ---------------------

def test_a_prepare_validation_failure_writes_no_marker_and_no_post(tmp_path):
    transport = ScriptedTransport(responses=list(fx.environment_responses()))
    with SQLiteEventJournal(tmp_path / "journal.db") as journal:
        instance = started(transport, journal)
        # Valid for the ExecutionRequest, invalid for the venue step size (0.00001): rejected in
        # prepare, before any durable marker exists.
        with pytest.raises(SymbolError):
            instance.submit(execution_request(quantity="0.00001500"))

        assert attempt_events(journal) == []
        assert posts(transport) == []


def test_b_rate_or_clock_precondition_failure_writes_no_marker_and_no_post(tmp_path):
    transport = ScriptedTransport(responses=list(fx.environment_responses()))
    with SQLiteEventJournal(tmp_path / "journal.db") as journal:
        instance = started(transport, journal)
        # Drift beyond the receive window: the clock precondition rejects in prepare.
        instance.client.clock.offset_ms = 10_000

        with pytest.raises(ClockDriftError):
            instance.submit(execution_request())

        assert attempt_events(journal) == []
        assert posts(transport) == []


def test_b_a_banned_budget_is_admitted_before_the_marker(tmp_path):
    transport = ScriptedTransport(responses=list(fx.environment_responses()))
    with SQLiteEventJournal(tmp_path / "journal.db") as journal:
        instance = started(transport, journal)
        instance.client.budget.banned = True

        with pytest.raises(RateLimitBanError):
            instance.submit(execution_request())

        assert attempt_events(journal) == []
        assert posts(transport) == []


# --- C: marker commit failure -----------------------------------------------------

def test_c_a_marker_commit_failure_prevents_any_post():
    transport = ScriptedTransport(
        responses=[*fx.environment_responses(), fx.order_response(CLIENT_ORDER_ID)]
    )
    instance = OrderRuntime(
        client=client_for(transport), instrument=INSTRUMENT, journal=_FailingJournal()
    )
    instance.start()

    with pytest.raises(RuntimeError, match="durable journal unavailable"):
        instance.submit(execution_request())

    assert posts(transport) == []


def test_unreadable_attempt_history_fails_closed_before_any_post():
    transport = ScriptedTransport(responses=list(fx.environment_responses()))
    instance = OrderRuntime(
        client=client_for(transport), instrument=INSTRUMENT, journal=_UnreadableAttemptJournal()
    )
    instance.start()

    with pytest.raises(AttemptJournalRequiredError, match="cannot prove prior submission attempts"):
        instance.submit(execution_request())

    assert posts(transport) == []


# --- D / 13 / 14: durable marker committed before the exact POST ------------------

def test_d_marker_is_durably_committed_before_the_exact_prepared_post(tmp_path):
    journal_path = tmp_path / "journal.db"
    fault = TransportTimeout("post-send ambiguity")
    inner = ScriptedTransport(responses=list(fx.environment_responses()))
    transport = _MarkerVisibilityTransport(journal_path, fault, inner)
    with SQLiteEventJournal(journal_path) as journal:
        client = client_for(transport)
        captured: dict = {}
        original_prepare = client.prepare_order

        def spy_prepare(request):  # noqa: ANN001 - test spy
            prepared = original_prepare(request)
            captured["prepared"] = prepared
            captured["prepare_calls"] = captured.get("prepare_calls", 0) + 1
            return prepared

        client.prepare_order = spy_prepare  # type: ignore[method-assign]
        instance = OrderRuntime(client=client, instrument=INSTRUMENT, journal=journal)
        instance.start()

        with pytest.raises(AmbiguousOrderError):
            instance.submit(execution_request())

        markers = attempt_events(journal)

    prepared = captured["prepared"]
    assert captured["prepare_calls"] == 1
    assert len(markers) == 1
    marker = markers[0]
    # Same identity persisted, derived and sent.
    assert marker.payload.venue_reference == CLIENT_ORDER_ID
    assert prepared.client_order_id == CLIENT_ORDER_ID
    assert f"newClientOrderId={CLIENT_ORDER_ID}" in transport.requests[-1].query
    # The exact prepared request object was sent: no rebuild, no re-signing.
    assert transport.requests[-1] is prepared.request
    assert len(posts(transport)) == 1
    # Durable visibility at send time: a fresh journal read inside send already saw the marker.
    assert len(transport.seen_during_send) == 1
    assert transport.seen_during_send[0].payload.venue_reference == CLIENT_ORDER_ID


def test_a_successful_submit_sends_exactly_one_post_and_records_the_order(tmp_path):
    transport = ScriptedTransport(
        responses=[*fx.environment_responses(), fx.order_response(CLIENT_ORDER_ID, status="NEW")]
    )
    with SQLiteEventJournal(tmp_path / "journal.db") as journal:
        instance = started(transport, journal)
        reference = instance.submit(execution_request())

        assert reference == CLIENT_ORDER_ID
        assert len(posts(transport)) == 1
        assert len(attempt_events(journal)) == 1
        order_events = [
            event for event in journal.read_range() if event.event_type == ORDER_STATE_EVENT_TYPE
        ]
        # The durable marker is its own contract; the venue response supplies the lifecycle fact.
        assert [event.payload.state for event in order_events] == ["accepted"]


# --- E / F / G: pre-send failures are typed evidence, never a retry ----------------

@pytest.mark.parametrize(
    "fault",
    [
        PreSendTransportError("DNS resolution failure"),
        PreSendTransportError("connection refused before establishment"),
        PreSendTransportError("connect timeout before establishment"),
    ],
    ids=["dns", "refused", "connect_timeout"],
)
def test_e_f_g_pre_send_failures_are_typed_and_never_retried(tmp_path, fault):
    transport = ScriptedTransport(responses=[*fx.environment_responses(), fault])
    with SQLiteEventJournal(tmp_path / "journal.db") as journal:
        instance = started(transport, journal)

        with pytest.raises(PreSendTransportError):
            instance.submit(execution_request())

        assert len(attempt_events(journal)) == 1
        assert len(posts(transport)) == 1, "exactly one POST attempt, never an automatic retry"
        # Proven not transmitted: resolved as a pre-send failure, so the attempt is closed out.
        canceled = [
            event
            for event in journal.read_range()
            if event.event_type == ORDER_STATE_EVENT_TYPE and event.payload.state == "canceled"
        ]
        assert len(canceled) == 1
        assert canceled[0].payload.reason.startswith("pre_send_failure_proven")


# --- H: post-send ambiguity -------------------------------------------------------

@pytest.mark.parametrize(
    "fault",
    [
        TransportTimeout("read timeout"),
        TransportError("connection reset by peer"),
        TransportError("remote disconnected mid-response"),
    ],
    ids=["read_timeout", "reset", "disconnect"],
)
def test_h_post_send_ambiguity_keeps_the_marker_and_never_retries(tmp_path, fault):
    transport = ScriptedTransport(responses=[*fx.environment_responses(), fault])
    with SQLiteEventJournal(tmp_path / "journal.db") as journal:
        instance = started(transport, journal)

        with pytest.raises(AmbiguousOrderError):
            instance.submit(execution_request())

        assert len(attempt_events(journal)) == 1
        assert len(posts(transport)) == 1
        assert [
            event
            for event in journal.read_range()
            if event.event_type == ORDER_STATE_EVENT_TYPE
        ] == []


# --- I / J / K / L: redirects are never followed ---------------------------------

@pytest.mark.parametrize("status", [301, 302, 307, 308])
def test_i_j_k_l_write_redirect_is_not_followed_and_is_ambiguous(tmp_path, status):
    transport = ScriptedTransport(
        responses=[
            *fx.environment_responses(),
            fx.response(status, {}, Location="https://elsewhere.invalid/api/v3/order"),
            fx.order_response(CLIENT_ORDER_ID),  # would only be consumed by a second POST
        ]
    )
    with SQLiteEventJournal(tmp_path / "journal.db") as journal:
        instance = started(transport, journal)

        with pytest.raises(AmbiguousOrderError):
            instance.submit(execution_request())

        assert len(posts(transport)) == 1, "a redirect must never cause a second POST"
        assert len(transport.requests) == 4, "no request was replayed to the Location"
        assert len(attempt_events(journal)) == 1


# --- M: HTTP 200 is never success on its own -------------------------------------

def _malformed_success_cases() -> list:
    return [
        pytest.param(fx.response(200, None), id="empty_body"),
        pytest.param(fx.response(200, "<html>not json</html>"), id="non_json_body"),
        pytest.param(fx.response(200, {"clientOrderId": CLIENT_ORDER_ID, "status": "NEW"}), id="missing_order_id"),
        pytest.param(fx.order_response(CLIENT_ORDER_ID, status="SOMETHING_NEW"), id="unrecognized_status"),
    ]


@pytest.mark.parametrize("response", _malformed_success_cases())
def test_m_malformed_success_response_is_post_send_ambiguous(tmp_path, response):
    transport = ScriptedTransport(responses=[*fx.environment_responses(), response])
    with SQLiteEventJournal(tmp_path / "journal.db") as journal:
        instance = started(transport, journal)

        with pytest.raises(AmbiguousOrderError):
            instance.submit(execution_request())

        assert len(posts(transport)) == 1
        assert len(attempt_events(journal)) == 1


# --- N: -1021 is a venue protocol rejection, not an order outcome -----------------

def test_n_timestamp_rejection_records_protocol_evidence_and_never_retries(tmp_path):
    transport = ScriptedTransport(
        responses=[
            *fx.environment_responses(),
            fx.rejected(code=-1021, msg="Timestamp for this request is outside of the recvWindow"),
        ]
    )
    with SQLiteEventJournal(tmp_path / "journal.db") as journal:
        instance = started(transport, journal)

        with pytest.raises(VenueProtocolRejectionError) as error:
            instance.submit(execution_request())

        assert type(error.value).reason_code == "venue_protocol_rejection_before_order_creation"
        assert type(error.value).evidence_code == "venue_timestamp_outside_recv_window"
        assert len(posts(transport)) == 1
        assert len(attempt_events(journal)) == 1
        rejected = [
            event
            for event in journal.read_range()
            if event.event_type == ORDER_STATE_EVENT_TYPE and event.payload.state == "rejected"
        ]
        assert len(rejected) == 1
        assert rejected[0].payload.reason == (
            "venue_protocol_rejection_before_order_creation:venue_timestamp_outside_recv_window"
        )


# --- 4: fail closed without durable attempt storage -------------------------------

def test_new_submit_without_a_durable_journal_fails_closed_with_zero_posts():
    transport = ScriptedTransport(
        responses=[*fx.environment_responses(), fx.order_response(CLIENT_ORDER_ID)]
    )
    instance = OrderRuntime(client=client_for(transport), instrument=INSTRUMENT, journal=None)
    instance.start()

    with pytest.raises(AttemptJournalRequiredError):
        instance.submit(execution_request())

    assert posts(transport) == []
    assert transport.requests[-1].method != "POST"


# --- duplicate client order identity: reconcile, never terminalise ----------------

def test_duplicate_client_order_id_is_reconciled_by_identity_not_terminalised(tmp_path):
    """A duplicate-identity response may mean the SAME order already exists at the venue."""
    transport = ScriptedTransport(
        responses=[
            *fx.environment_responses(),
            fx.rejected(code=-2010, msg="Duplicate order sent."),
            fx.order_response(CLIENT_ORDER_ID, status="NEW", order_id=99),  # the identity lookup
        ]
    )
    with SQLiteEventJournal(tmp_path / "journal.db") as journal:
        instance = started(transport, journal)

        reference = instance.submit(execution_request())

        assert reference == CLIENT_ORDER_ID
        assert len(posts(transport)) == 1, "the write is never retried"
        adopted = instance.client.known_orders()
        assert adopted and adopted[0].venue_order_id == "99", "the venue identity was adopted"
        states = [
            event.payload.state
            for event in journal.read_range()
            if event.event_type == ORDER_STATE_EVENT_TYPE
        ]
        assert states == ["accepted"], "the venue truth was recorded, not a rejection"
        # No terminal rejection is journaled for a duplicate identity.
        rejected = [
            event
            for event in journal.read_range()
            if event.event_type == ORDER_STATE_EVENT_TYPE and event.payload.state == "rejected"
        ]
        assert rejected == []


@pytest.mark.parametrize(
    "read_failures",
    [
        pytest.param(
            [fx.rejected(code=-2013, msg="Order does not exist.")],
            id="not_found",
        ),
        pytest.param(
            [PreSendTransportError("identity read never connected")] * 3,
            id="pre_send_read_failure",
        ),
        pytest.param(
            [TransportTimeout("identity read timed out")] * 3,
            id="read_timeout",
        ),
    ],
)
def test_duplicate_identity_failed_reread_stays_ambiguous_and_pending(tmp_path, read_failures):
    transport = ScriptedTransport(
        responses=[
            *fx.environment_responses(),
            fx.rejected(code=-2010, msg="Duplicate order sent."),
            *read_failures,
        ]
    )
    with SQLiteEventJournal(tmp_path / "journal.db") as journal:
        instance = started(transport, journal)

        with pytest.raises(AmbiguousOrderError):
            instance.submit(execution_request())

        assert len(posts(transport)) == 1
        assert len(attempt_events(journal)) == 1
        assert [
            event
            for event in journal.read_range()
            if event.event_type == ORDER_STATE_EVENT_TYPE
        ] == []
        states = instance.local_states()
        assert len(states) == 1
        assert states[0].order_reference == CLIENT_ORDER_ID
        assert states[0].state.value == "submitted"
        assert states[0].submission_attempted is True


@pytest.mark.parametrize(
    "mutate_body",
    [
        pytest.param(
            lambda body: {**body, "clientOrderId": "ct-different-identity"},
            id="different_client_id",
        ),
        pytest.param(
            lambda body: {**body, "symbol": "ETHUSDT"},
            id="different_symbol",
        ),
        pytest.param(
            lambda body: {key: value for key, value in body.items() if key != "orderId"},
            id="missing_order_id",
        ),
    ],
)
def test_duplicate_identity_mismatched_reread_stays_ambiguous_and_pending(
    tmp_path, mutate_body
):
    reread = fx.order_response(CLIENT_ORDER_ID, status="NEW", order_id=99)
    transport = ScriptedTransport(
        responses=[
            *fx.environment_responses(),
            fx.rejected(code=-2010, msg="Duplicate order sent."),
            fx.response(200, mutate_body(dict(reread.body))),
        ]
    )
    with SQLiteEventJournal(tmp_path / "journal.db") as journal:
        instance = started(transport, journal)

        with pytest.raises(AmbiguousOrderError):
            instance.submit(execution_request())

        assert len(posts(transport)) == 1
        assert len(attempt_events(journal)) == 1
        assert [
            event
            for event in journal.read_range()
            if event.event_type == ORDER_STATE_EVENT_TYPE
        ] == []
        assert instance.client.known_orders() == ()
        assert instance.local_states()[0].state.value == "submitted"


@pytest.mark.parametrize("reread_exists", [True, False], ids=["adopt", "not_found"])
def test_ambiguous_submit_explicit_resubmit_never_posts_twice(tmp_path, reread_exists):
    reread = (
        fx.order_response(CLIENT_ORDER_ID, status="NEW", order_id=777)
        if reread_exists
        else fx.rejected(code=-2013, msg="Order does not exist.")
    )
    transport = ScriptedTransport(
        responses=[
            *fx.environment_responses(),
            TransportTimeout("post-send ambiguity"),
            reread,
        ]
    )
    with SQLiteEventJournal(tmp_path / "journal.db") as journal:
        instance = started(transport, journal)
        with pytest.raises(AmbiguousOrderError):
            instance.submit(execution_request())

        if reread_exists:
            assert instance.submit(execution_request()) == CLIENT_ORDER_ID
            states = instance.local_states()
            assert len(states) == 1
            assert states[0].order_reference == CLIENT_ORDER_ID
            assert states[0].state.value == "accepted"
            accepted = [
                event
                for event in journal.read_range()
                if event.event_type == ORDER_STATE_EVENT_TYPE
                and event.payload.state == "accepted"
            ]
            assert len(accepted) == 1
        else:
            with pytest.raises(AmbiguousOrderError):
                instance.submit(execution_request())
            assert instance.local_states()[0].state.value == "submitted"

        assert len(posts(transport)) == 1
        assert len(attempt_events(journal)) == 1


@pytest.mark.parametrize("reread_exists", [True, False], ids=["adopt", "not_found"])
def test_restart_durable_attempt_never_posts_same_identity_again(tmp_path, reread_exists):
    journal_path = tmp_path / "journal.db"
    first_transport = ScriptedTransport(
        responses=[*fx.environment_responses(), TransportTimeout("post-send ambiguity")]
    )
    with SQLiteEventJournal(journal_path) as journal:
        first = started(first_transport, journal)
        with pytest.raises(AmbiguousOrderError):
            first.submit(execution_request())
        assert len(posts(first_transport)) == 1
        assert len(attempt_events(journal)) == 1

    reread = (
        fx.order_response(CLIENT_ORDER_ID, status="NEW", order_id=778)
        if reread_exists
        else fx.rejected(code=-2013, msg="Order does not exist.")
    )
    second_transport = ScriptedTransport(responses=[*fx.environment_responses(), reread])
    with SQLiteEventJournal(journal_path) as journal:
        restarted = started(second_transport, journal)

        if reread_exists:
            assert restarted.submit(execution_request()) == CLIENT_ORDER_ID
            states = restarted.local_states()
            assert len(states) == 1
            assert states[0].order_reference == CLIENT_ORDER_ID
            assert states[0].state.value == "accepted"
        else:
            with pytest.raises(AmbiguousOrderError):
                restarted.submit(execution_request())

        assert posts(second_transport) == []
        assert len(attempt_events(journal)) == 1


def test_recovered_pending_identity_without_marker_is_never_posted(tmp_path):
    """recover_order() leaves the identity pending: a later submit must fail closed, zero POSTs."""
    transport = ScriptedTransport(
        responses=[
            *fx.environment_responses(),
            fx.rejected(code=-2013, msg="Order does not exist."),  # the recovery lookup
            fx.rejected(code=-2013, msg="Order does not exist."),  # the identity re-read
        ]
    )
    with SQLiteEventJournal(tmp_path / "journal.db") as journal:
        instance = started(transport, journal)
        assert instance.recover_order(INTENT_ID, correlation_id=CORRELATION_ID) is None
        assert attempt_events(journal) == []

        with pytest.raises(AmbiguousOrderError, match="permanently ineligible"):
            instance.submit(execution_request())

        assert posts(transport) == []
        assert attempt_events(journal) == []
        states = instance.local_states()
        assert len(states) == 1
        assert states[0].order_reference == CLIENT_ORDER_ID
        assert states[0].state.value == "submitted"


def test_duplicate_failed_reread_then_resubmit_still_never_posts_again(tmp_path):
    transport = ScriptedTransport(
        responses=[
            *fx.environment_responses(),
            fx.rejected(code=-2010, msg="Duplicate order sent."),
            fx.rejected(code=-2013, msg="Order does not exist."),
            fx.rejected(code=-2013, msg="Order does not exist."),
        ]
    )
    with SQLiteEventJournal(tmp_path / "journal.db") as journal:
        instance = started(transport, journal)

        with pytest.raises(AmbiguousOrderError):
            instance.submit(execution_request())
        with pytest.raises(AmbiguousOrderError):
            instance.submit(execution_request())

        assert len(posts(transport)) == 1
        assert len(attempt_events(journal)) == 1
        assert [
            event
            for event in journal.read_range()
            if event.event_type == ORDER_STATE_EVENT_TYPE
        ] == []



@pytest.mark.parametrize(
    "guard_read_failures",
    [
        pytest.param(
            [PreSendTransportError("guard read never connected")] * 3,
            id="pre_send_read_failure",
        ),
        pytest.param(
            [TransportTimeout("guard read timed out")] * 3,
            id="read_timeout",
        ),
    ],
)
def test_existing_durable_attempt_guard_read_failure_never_reposts(tmp_path, guard_read_failures):
    transport = ScriptedTransport(
        responses=[
            *fx.environment_responses(),
            TransportTimeout("post-send ambiguity"),
            *guard_read_failures,
        ]
    )
    with SQLiteEventJournal(tmp_path / "journal.db") as journal:
        instance = started(transport, journal)
        with pytest.raises(AmbiguousOrderError):
            instance.submit(execution_request())

        with pytest.raises(AmbiguousOrderError):
            instance.submit(execution_request())

        assert len(posts(transport)) == 1
        assert len(attempt_events(journal)) == 1
        assert [
            event
            for event in journal.read_range()
            if event.event_type == ORDER_STATE_EVENT_TYPE
        ] == []


def test_existing_durable_attempt_guard_rejects_mismatched_identity_without_caching(tmp_path):
    reread = fx.order_response(CLIENT_ORDER_ID, status="NEW", order_id=779)
    bad_body = {**dict(reread.body), "clientOrderId": "ct-foreign-identity"}
    transport = ScriptedTransport(
        responses=[
            *fx.environment_responses(),
            TransportTimeout("post-send ambiguity"),
            fx.response(200, bad_body),
        ]
    )
    with SQLiteEventJournal(tmp_path / "journal.db") as journal:
        instance = started(transport, journal)
        with pytest.raises(AmbiguousOrderError):
            instance.submit(execution_request())

        with pytest.raises(AmbiguousOrderError):
            instance.submit(execution_request())

        assert len(posts(transport)) == 1
        assert len(attempt_events(journal)) == 1
        assert instance.client.known_orders() == ()
        assert "ct-foreign-identity" not in instance.client._orders


def test_durable_attempt_for_one_identity_does_not_block_a_different_identity(tmp_path):
    second_intent = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
    second_correlation = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
    second_reference = f"ct-{second_intent.hex[:20]}"
    second_request = ExecutionRequest.create(
        correlation_id=second_correlation,
        symbol="BTCUSDT",
        side=ExecutionSide.BUY,
        order_type=ExecutionOrderType.LIMIT,
        quantity=Decimal("0.00100000"),
        limit_price=Decimal("27000.00"),
        intent_id=second_intent,
    )
    transport = ScriptedTransport(
        responses=[
            *fx.environment_responses(),
            TransportTimeout("first identity is ambiguous"),
            fx.order_response(second_reference, status="NEW", order_id=780),
        ]
    )
    with SQLiteEventJournal(tmp_path / "journal.db") as journal:
        instance = started(transport, journal)
        with pytest.raises(AmbiguousOrderError):
            instance.submit(execution_request())

        assert instance.submit(second_request) == second_reference

        assert len(posts(transport)) == 2
        attempts = attempt_events(journal)
        assert {event.payload.venue_reference for event in attempts} == {
            CLIENT_ORDER_ID,
            second_reference,
        }


@pytest.mark.parametrize(
    "first_outcome,first_error",
    [
        pytest.param(
            PreSendTransportError("write never connected"),
            PreSendTransportError,
            id="pre_send_canceled",
        ),
        pytest.param(
            fx.rejected(
                code=-1021,
                msg="Timestamp for this request is outside of the recvWindow",
            ),
            VenueProtocolRejectionError,
            id="timestamp_rejected",
        ),
        pytest.param(
            fx.rejected(code=-2019, msg="Margin is insufficient"),
            RejectedError,
            id="ordinary_rejected",
        ),
    ],
)
def test_terminal_durable_attempt_identity_is_never_reposted_in_process(
    tmp_path, first_outcome, first_error
):
    transport = ScriptedTransport(
        responses=[
            *fx.environment_responses(),
            first_outcome,
            fx.rejected(code=-2013, msg="Order does not exist."),
        ]
    )
    with SQLiteEventJournal(tmp_path / "journal.db") as journal:
        instance = started(transport, journal)
        with pytest.raises(first_error):
            instance.submit(execution_request())

        with pytest.raises(AmbiguousOrderError, match="permanently ineligible"):
            instance.submit(execution_request())

        assert len(posts(transport)) == 1
        assert len(attempt_events(journal)) == 1


@pytest.mark.parametrize(
    "first_outcome,first_error",
    [
        pytest.param(
            PreSendTransportError("write never connected"),
            PreSendTransportError,
            id="pre_send_canceled",
        ),
        pytest.param(
            fx.rejected(
                code=-1021,
                msg="Timestamp for this request is outside of the recvWindow",
            ),
            VenueProtocolRejectionError,
            id="timestamp_rejected",
        ),
        pytest.param(
            fx.rejected(code=-2019, msg="Margin is insufficient"),
            RejectedError,
            id="ordinary_rejected",
        ),
    ],
)
def test_terminal_durable_attempt_identity_is_never_reposted_after_restart(
    tmp_path, first_outcome, first_error
):
    journal_path = tmp_path / "terminal-identity.db"
    first_transport = ScriptedTransport(
        responses=[*fx.environment_responses(), first_outcome]
    )
    with SQLiteEventJournal(journal_path) as journal:
        first = started(first_transport, journal)
        with pytest.raises(first_error):
            first.submit(execution_request())
        assert len(posts(first_transport)) == 1
        assert len(attempt_events(journal)) == 1

    second_transport = ScriptedTransport(
        responses=[
            *fx.environment_responses(),
            fx.rejected(code=-2013, msg="Order does not exist."),
        ]
    )
    with SQLiteEventJournal(journal_path) as journal:
        restarted = started(second_transport, journal)
        with pytest.raises(AmbiguousOrderError, match="permanently ineligible"):
            restarted.submit(execution_request())

        assert posts(second_transport) == []
        assert len(attempt_events(journal)) == 1


def test_ordinary_deterministic_rejection_still_terminalises(tmp_path):
    """Control: a real venue rejection (not a duplicate identity) keeps terminal semantics."""
    transport = ScriptedTransport(
        responses=[*fx.environment_responses(), fx.rejected(code=-2019, msg="Margin is insufficient")]
    )
    with SQLiteEventJournal(tmp_path / "journal.db") as journal:
        instance = started(transport, journal)

        with pytest.raises(RejectedError):
            instance.submit(execution_request())

        assert len(posts(transport)) == 1
        rejected = [
            event
            for event in journal.read_range()
            if event.event_type == ORDER_STATE_EVENT_TYPE and event.payload.state == "rejected"
        ]
        assert len(rejected) == 1


# --- 4: 429 / 418 keep the attempt fenced, never terminal -------------------------

@pytest.mark.parametrize(
    "response,expected",
    [
        (fx.rate_limited("1"), RateLimited),
        (fx.banned("120"), RateLimitBanError),
    ],
    ids=["429", "418"],
)
def test_rate_limit_responses_keep_the_marker_and_never_terminalise(tmp_path, response, expected):
    transport = ScriptedTransport(responses=[*fx.environment_responses(), response])
    with SQLiteEventJournal(tmp_path / "journal.db") as journal:
        instance = started(transport, journal)

        with pytest.raises(expected):
            instance.submit(execution_request())

        assert len(posts(transport)) == 1, "no automatic POST retry"
        assert len(attempt_events(journal)) == 1, "the durable attempt stays for reconciliation"
        assert [
            event
            for event in journal.read_range()
            if event.event_type == ORDER_STATE_EVENT_TYPE
        ] == [], "429/418 prove no final order outcome, so no terminal lifecycle event"


# --- 12: durable restart / recovery proof -----------------------------------------

def test_restart_recovers_the_durable_attempt_from_the_journal_only(tmp_path):
    journal_path = tmp_path / "journal.db"
    transport = ScriptedTransport(
        responses=[*fx.environment_responses(), TransportTimeout("no response")]
    )
    with SQLiteEventJournal(journal_path) as journal:
        instance = started(transport, journal)
        with pytest.raises(AmbiguousOrderError):
            instance.submit(execution_request())
        assert instance.local_states()[0].submission_attempted is True

    # The runtime instance is gone; the journal is the sole authority.
    with SQLiteEventJournal(journal_path) as fresh:
        reconstructor, _summary = reconstruct_order_states(fresh)
        states = reconstructor.states()
        anchors = reconstructor.attempt_anchors(CLIENT_ORDER_ID)
        run = RestartRecovery(
            engine=ReconciliationEngine(load_policy({"policy_id": "recovery-v1"})),
            journal=fresh,
            venue_sources=[StaticVenueStateSource(entries=())],
            producer="application.recovery",
        ).run(now=datetime.fromtimestamp(fx.SERVER_TIME_MS / 1000, tz=timezone.utc))

    assert len(states) == 1
    state = states[0]
    assert state.order_reference == CLIENT_ORDER_ID
    assert state.submission_attempted is True
    assert anchors is not None
    assert anchors.intent_id == INTENT_ID
    assert anchors.quantity == Decimal("0.00100000")
    assert anchors.limit_price == Decimal("27000.00")
    assert anchors.venue_producer == "infrastructure.binance_testnet"
    # Safely unresolved and fenced until authoritative venue evidence exists.
    assert run.report.outcome_for(CLIENT_ORDER_ID) is (
        ReconciliationOutcome.AMBIGUOUS_PENDING_RECONCILIATION
    )
    assert run.report.requires_halt_new_risk is True
    assert run.recovery_state == "failed"


# --- transport classification (real RequestsTransport, no network) ---------------

def _stub_requests(monkeypatch, exc: Exception) -> list:
    import requests

    calls: list = []

    def fake_request(method, url, headers=None, timeout=None, allow_redirects=None):
        calls.append({"method": method, "allow_redirects": allow_redirects})
        raise exc

    monkeypatch.setattr(requests, "request", fake_request)
    return calls


def _new_connection_error() -> BaseException:
    from requests.packages.urllib3.exceptions import NewConnectionError

    return NewConnectionError(None, "connection refused")


def _connection_error_with_cause(requests, cause: BaseException):
    """Build the exception chain requests/urllib3 actually raise (``raise ... from err``)."""
    error = requests.exceptions.ConnectionError("connection refused")
    error.__cause__ = cause
    return error


def _request() -> HttpRequest:
    return HttpRequest(method="POST", url="https://testnet.binance.vision/api/v3/order", query="a=1", is_write=True)


@pytest.mark.parametrize(
    "exc_factory,expected,pre_send",
    [
        (lambda r: r.exceptions.ConnectTimeout("connect timed out"), PreSendTransportError, True),
        (lambda r: r.exceptions.SSLError("tls handshake failed"), TransportError, False),
        (lambda r: r.exceptions.ConnectionError("connection aborted"), TransportError, False),
        (lambda r: r.exceptions.ReadTimeout("read timed out"), TransportTimeout, False),
        (lambda r: r.exceptions.RequestException("unknown socket failure"), TransportError, False),
    ],
    ids=["connect_timeout", "tls", "aborted", "read_timeout", "unknown"],
)
def test_transport_classifies_only_proven_pre_send_failures(monkeypatch, exc_factory, expected, pre_send):
    import requests

    calls = _stub_requests(monkeypatch, exc_factory(requests))
    transport = RequestsTransport(timeout_seconds=1.0)

    with pytest.raises(expected) as error:
        transport.send(_request())

    assert isinstance(error.value, expected)
    assert isinstance(error.value, PreSendTransportError) is pre_send
    assert calls and calls[0]["allow_redirects"] is False


def _urllib3_cause(kind: str) -> BaseException:
    """Build the real urllib3 exception an installed requests/urllib3 chain carries."""
    from urllib3.exceptions import NameResolutionError, NewConnectionError

    if kind == "dns":
        return NameResolutionError("testnet.binance.vision", None, OSError("name resolution failed"))
    return NewConnectionError(None, "connection refused")


@pytest.mark.parametrize("kind", ["dns", "refused"])
def test_transport_classifies_dns_and_refused_independently(monkeypatch, kind):
    """Each proven pre-send class is entered through its own real exception chain."""
    import requests

    cause = _urllib3_cause(kind)
    error_raised = _connection_error_with_cause(requests, cause)
    calls = _stub_requests(monkeypatch, error_raised)
    transport = RequestsTransport(timeout_seconds=1.0)

    with pytest.raises(PreSendTransportError) as error:
        transport.send(_request())

    # The faithful requests -> urllib3 chain is preserved for evidence.
    assert isinstance(error.value.__cause__, requests.exceptions.ConnectionError)
    assert error.value.__cause__.__cause__ is cause
    assert type(cause).__name__ in {"NameResolutionError", "NewConnectionError"}
    assert calls and calls[0]["allow_redirects"] is False


def test_dns_and_refused_are_distinguishable_in_urllib3():
    """Evidence for the classifier's cause-chain names (urllib3 2.x semantics)."""
    import urllib3
    from urllib3.exceptions import NameResolutionError, NewConnectionError

    assert urllib3.__version__.startswith("2.")
    assert issubclass(NameResolutionError, NewConnectionError)
    assert type(_urllib3_cause("dns")).__name__ == "NameResolutionError"
    assert type(_urllib3_cause("refused")).__name__ == "NewConnectionError"


def test_transport_never_follows_redirects(monkeypatch):
    import requests

    captured: dict = {}

    class _Response:
        status_code = 307
        content = b""
        headers: dict = {}

        def json(self):
            raise ValueError("no json")

    def fake_request(method, url, headers=None, timeout=None, allow_redirects=None):
        captured["allow_redirects"] = allow_redirects
        return _Response()

    monkeypatch.setattr(requests, "request", fake_request)

    response = RequestsTransport(timeout_seconds=1.0).send(_request())

    assert response.status_code == 307
    assert captured["allow_redirects"] is False


# --- 5: the venue-write boundary requires durable authorization -------------------

def _fresh_prepared(client):  # noqa: ANN001 - test helper
    prepared = client.prepare_order(fx.limit_order_request(CLIENT_ORDER_ID))
    assert prepared.request is not None, "this test must exercise the non-dedup path"
    return prepared


def test_place_order_refuses_a_fresh_write_without_a_durable_attempt():
    transport = ScriptedTransport(
        responses=[fx.exchange_info_response(), fx.order_response(CLIENT_ORDER_ID)]
    )
    client = client_for(transport)

    with pytest.raises(AttemptJournalRequiredError):
        client.place_order(fx.limit_order_request(CLIENT_ORDER_ID))

    assert posts(transport) == []


def test_submit_prepared_refuses_a_fresh_write_without_authorization():
    transport = ScriptedTransport(
        responses=[fx.exchange_info_response(), fx.order_response(CLIENT_ORDER_ID)]
    )
    client = client_for(transport)
    prepared = _fresh_prepared(client)

    with pytest.raises(AttemptJournalRequiredError):
        client.submit_prepared(prepared)

    assert posts(transport) == []


def test_place_order_still_adopts_an_existing_order_without_a_marker(tmp_path):
    """The dedup path needs no marker and performs no POST."""
    transport = ScriptedTransport(
        responses=[
            *fx.environment_responses(),
            fx.order_response(CLIENT_ORDER_ID, status="NEW"),
            fx.order_response(CLIENT_ORDER_ID, status="NEW"),  # the dedup status lookup
        ]
    )
    with SQLiteEventJournal(tmp_path / "journal.db") as journal:
        instance = started(transport, journal)
        instance.submit(execution_request())
        markers_before = len(attempt_events(journal))
        posts_before = len(posts(transport))

        adopted = instance.client.place_order(fx.limit_order_request(CLIENT_ORDER_ID))

        assert adopted.deduplicated is True
        assert adopted.client_order_id == CLIENT_ORDER_ID
        assert len(posts(transport)) == posts_before, "no POST for an already-known order"
        assert len(attempt_events(journal)) == markers_before, "no marker for a dedup adoption"


def _marker_fields(prepared, **overrides):  # noqa: ANN001 - test helper
    fields = {
        "marker_event_id": uuid4(),
        "venue_reference": prepared.client_order_id,
        "order_id": deterministic_order_uuid(prepared.client_order_id),
        "intent_id": uuid4(),
        "correlation_id": uuid4(),
        "symbol": prepared.symbol,
        "side": prepared.side,
        "order_type": prepared.order_type,
        "quantity": prepared.quantity,
        "limit_price": prepared.limit_price,
        "time_in_force": prepared.time_in_force,
    }
    fields.update(overrides)
    return fields


@pytest.mark.parametrize(
    "field,tamper",
    [
        ("venue_reference", lambda prepared: "ct-someone-else"),
        ("order_id", lambda prepared: uuid4()),
        ("symbol", lambda prepared: "ETHUSDT"),
        ("side", lambda prepared: "sell"),
        ("quantity", lambda prepared: prepared.quantity + 1),
        ("limit_price", lambda prepared: prepared.limit_price + 1),
    ],
)
def test_authorization_refuses_any_tampered_marker_field(field, tamper):
    transport = ScriptedTransport(
        responses=[fx.exchange_info_response(), fx.order_response(CLIENT_ORDER_ID)]
    )
    client = client_for(transport)
    prepared = _fresh_prepared(client)
    fields = _marker_fields(prepared)
    fields[field] = tamper(prepared)

    with pytest.raises(AttemptJournalRequiredError):
        client._authorize_attempt(prepared, **fields)

    assert posts(transport) == []


def test_authorization_is_accepted_for_the_exact_prepared_order():
    transport = ScriptedTransport(
        responses=[fx.exchange_info_response(), fx.order_response(CLIENT_ORDER_ID, status="NEW")]
    )
    client = client_for(transport)
    prepared = _fresh_prepared(client)

    authorized = client._authorize_attempt(prepared, **_marker_fields(prepared))
    order = client.submit_prepared(authorized)

    assert order.client_order_id == CLIENT_ORDER_ID
    assert len(posts(transport)) == 1


# --- 7 / 13: the authorization binds to the committed marker ---------------------

def test_marker_visibility_binds_the_authorization_marker_id(tmp_path):
    journal_path = tmp_path / "journal.db"
    fault = TransportTimeout("post-send ambiguity")
    inner = ScriptedTransport(responses=list(fx.environment_responses()))
    transport = _MarkerVisibilityTransport(journal_path, fault, inner)
    with SQLiteEventJournal(journal_path) as journal:
        client = client_for(transport)
        captured: dict = {}
        original_authorize = client._authorize_attempt

        def spy_authorize(prepared, **fields):  # noqa: ANN001 - test spy
            authorized = original_authorize(prepared, **fields)
            captured["authorization"] = authorized.attempt_authorization
            return authorized

        client._authorize_attempt = spy_authorize  # type: ignore[method-assign]
        instance = OrderRuntime(client=client, instrument=INSTRUMENT, journal=journal)
        instance.start()
        with pytest.raises(AmbiguousOrderError):
            instance.submit(execution_request())
        markers = attempt_events(journal)

    authorization = captured["authorization"]
    assert authorization is not None
    assert len(markers) == 1
    assert authorization.marker_event_id == markers[0].event_id
    assert authorization.client_order_id == markers[0].payload.venue_reference == CLIENT_ORDER_ID
    assert authorization.order_id == markers[0].order_id
    assert transport.requests[-1] is not None and len(posts(transport)) == 1


# --- 4 / 1F: production call-site restriction (AST) ------------------------------

RUNTIME_ONLY_ATTRIBUTES = ("_authorize_attempt", "submit_prepared")
RUNTIME_FILE = Path("execution/binance_testnet/runtime.py")


def _production_source_files() -> list[Path]:
    package = Path(__file__).resolve().parents[1]
    return [
        path
        for path in package.rglob("*.py")
        if "__pycache__" not in path.parts and "tests" not in path.parts
    ]


#: The single proven pre-existing Phase-14 name collision: ``ProductionRuntime`` defines its OWN
#: ``submit_prepared`` (definition at ``production/runtime.py:596``) and calls it on ``self``
#: (``production/runtime.py:669``). The exemption below matches exactly that coordinate set.
PHASE14_RUNTIME_FILE = Path("production/runtime.py")
PHASE14_CLASS_NAME = "ProductionRuntime"


def _is_exact_phase14_collision(
    node: ast.Call, classes: tuple[ast.ClassDef, ...], relative_path: Path, attribute: str
) -> bool:
    """The one exact exemption: Phase-14 ``ProductionRuntime`` calling its own method on ``self``.

    Every coordinate must match exactly: path, attribute, the simple ``self`` receiver, and the
    INNERMOST enclosing class name. No ancestor-class match, no other receiver, no other file, no
    other attribute (``_authorize_attempt`` never qualifies), and it can never apply inside the
    adapter package.
    """
    return (
        relative_path == PHASE14_RUNTIME_FILE
        and attribute == "submit_prepared"
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "self"
        and bool(classes)
        and classes[-1].name == PHASE14_CLASS_NAME
    )


def _classify_restricted_production_calls(tree: ast.AST, relative_path: Path) -> list[str]:
    """Classify production-source calls that violate the Stage-2 write-boundary rules.

    Rules (identifier-based, direct attribute calls only):

    1. a direct ``.place_order(...)`` invocation is banned in production source (the method may
       still be *defined*: definitions are FunctionDef nodes, not Call nodes);
    2. a direct ``._authorize_attempt(...)`` / ``.submit_prepared(...)`` invocation is allowed only
       in ``execution/binance_testnet/runtime.py``, regardless of the receiver expression (``self``,
       ``client``, ``self._client``, ``runtime.client``, ...);
    3. exactly one narrow exemption, for the verified pre-existing Phase-14 call site:
       ``production/runtime.py`` + innermost enclosing class ``ProductionRuntime`` + attribute
       ``submit_prepared`` + receiver the simple name ``self``. That method is an unrelated
       same-name method, not the Binance adapter write boundary. Anything else - another file,
       another class, a nested or ancestor class, another receiver such as ``client``, or the
       attribute ``_authorize_attempt`` - is a violation, and the exemption can never apply inside
       ``execution/binance_testnet/``.

    Method-name *definitions* never matter: the guard inspects ``ast.Call`` nodes, not
    ``FunctionDef`` declarations, so a class that merely defines ``submit_prepared``/``place_order``
    is not flagged for the definition (only for a violating call).

    Limitation (documented threat model): the classifier detects **direct** attribute calls such as
    ``client._authorize_attempt(...)``, ``self._authorize_attempt(...)``,
    ``client.submit_prepared(...)`` or ``self.submit_prepared(...)``. It exists to prevent
    accidental/direct architectural bypass. It does NOT claim to defend against deliberately
    obfuscated Python access such as ``authorize = client._authorize_attempt; authorize(...)`` or
    ``getattr(client, "_authorize_attempt")(...)`` - that aliasing/reflection is outside the
    Stage-2 static guard's threat model. The direct-call rules above are never weakened for it.
    """
    violations: list[str] = []
    label = relative_path.as_posix()

    def _visit(node: ast.AST, classes: tuple[ast.ClassDef, ...]) -> None:
        if isinstance(node, ast.ClassDef):
            classes = (*classes, node)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            attribute = node.func.attr
            if attribute == "place_order":
                violations.append(f"{label}:{node.lineno} invokes place_order")
            elif attribute in RUNTIME_ONLY_ATTRIBUTES and relative_path != RUNTIME_FILE:
                if not _is_exact_phase14_collision(node, classes, relative_path, attribute):
                    violations.append(f"{label}:{node.lineno} invokes {attribute}")
        for child in ast.iter_child_nodes(node):
            _visit(child, classes)

    _visit(tree, ())
    return violations


def test_production_call_sites_are_restricted_to_the_runtime():
    """AST proof: only the runtime may authorize or send a prepared write; nothing calls place_order."""
    package = Path(__file__).resolve().parents[1]
    violations: list[str] = []
    for path in _production_source_files():
        relative = path.relative_to(package)
        tree = ast.parse(path.read_text(encoding="utf-8"))
        violations.extend(_classify_restricted_production_calls(tree, relative))

    assert violations == [], violations


@pytest.mark.parametrize(
    "attribute",
    ["_authorize_attempt", "submit_prepared"],
)
def test_negative_control_self_calls_outside_the_runtime_are_rejected(attribute):
    """A NON-runtime production file must not call these on any receiver, including ``self``."""
    source = (
        "class Helper:\n"
        "    def run(self):\n"
        f"        return self.{attribute}(1)\n"
    )
    violations = _classify_restricted_production_calls(
        ast.parse(source), Path("audit/synthetic_bypass.py")
    )

    assert violations == [f"audit/synthetic_bypass.py:3 invokes {attribute}"]


@pytest.mark.parametrize(
    "attribute",
    ["_authorize_attempt", "submit_prepared"],
)
def test_positive_control_runtime_calls_are_accepted(attribute):
    """The same direct calls are accepted when the synthetic path is exactly the runtime file."""
    source = (
        "class Runtime:\n"
        "    def run(self):\n"
        f"        return self.client.{attribute}(1)\n"
    )
    violations = _classify_restricted_production_calls(
        ast.parse(source), RUNTIME_FILE
    )

    assert violations == []


def test_negative_control_client_receiver_outside_the_runtime_is_rejected():
    source = "def bypass(client):\n    return client.submit_prepared(prepared)\n"
    violations = _classify_restricted_production_calls(
        ast.parse(source), Path("audit/synthetic_bypass.py")
    )

    assert violations == [
        "audit/synthetic_bypass.py:2 invokes submit_prepared"
    ]


@pytest.mark.parametrize("attribute", ["_authorize_attempt", "submit_prepared"])
def test_negative_control_adapter_self_calls_are_always_rejected(attribute):
    """Inside the adapter directory the Phase-14 exemption can never apply."""
    source = (
        "class Adapter:\n"
        f"    def {attribute}(self, value):\n"
        "        return value\n"
        "\n"
        "    def run(self):\n"
        f"        return self.{attribute}(1)\n"
    )
    violations = _classify_restricted_production_calls(
        ast.parse(source), Path(f"execution/binance_testnet/{attribute}_bypass.py")
    )

    assert violations == [
        f"execution/binance_testnet/{attribute}_bypass.py:6 invokes {attribute}"
    ]


def test_negative_control_phase14_receiver_mismatch_is_rejected():
    """A method named submit_prepared in the class must NOT authorize a call on ``client``."""
    source = (
        "class ProductionRuntime:\n"
        "    def submit_prepared(self, value):\n"
        "        return value\n"
        "\n"
        "    def other(self, client):\n"
        "        return client.submit_prepared(1)\n"
    )
    violations = _classify_restricted_production_calls(
        ast.parse(source), PHASE14_RUNTIME_FILE
    )

    assert violations == ["production/runtime.py:6 invokes submit_prepared"]


def test_negative_control_phase14_self_authorize_attempt_is_rejected():
    """No exemption exists for ``_authorize_attempt`` anywhere, including production/runtime.py."""
    source = (
        "class ProductionRuntime:\n"
        "    def other(self):\n"
        "        return self._authorize_attempt(1)\n"
    )
    violations = _classify_restricted_production_calls(
        ast.parse(source), PHASE14_RUNTIME_FILE
    )

    assert violations == ["production/runtime.py:3 invokes _authorize_attempt"]


def test_negative_control_unrelated_class_self_submit_prepared_is_rejected():
    """The mandatory control: an unrelated production class may not self-call submit_prepared."""
    source = (
        "class SomeOtherProductionClass:\n"
        "    def submit_prepared(self, value):\n"
        "        return value\n"
        "\n"
        "    def run(self):\n"
        "        return self.submit_prepared(1)\n"
    )
    violations = _classify_restricted_production_calls(
        ast.parse(source), Path("production/other_runtime.py")
    )

    assert violations == ["production/other_runtime.py:6 invokes submit_prepared"]


def test_negative_control_innermost_class_only_authorizes_phase14():
    """An ancestor/outer ProductionRuntime must not authorize a nested class's self-call."""
    source = (
        "class ProductionRuntime:\n"
        "    class Nested:\n"
        "        def submit_prepared(self, value):\n"
        "            return value\n"
        "\n"
        "        def run(self):\n"
        "            return self.submit_prepared(1)\n"
    )
    violations = _classify_restricted_production_calls(
        ast.parse(source), PHASE14_RUNTIME_FILE
    )

    assert violations == ["production/runtime.py:7 invokes submit_prepared"]


def test_positive_control_real_phase14_call_shape_is_accepted():
    """The exact verified coordinate set (path/class/self/submit_prepared) is accepted."""
    source = (
        "class ProductionRuntime:\n"
        "    def submit_prepared(self, preparation):\n"
        "        return preparation\n"
        "\n"
        "    def execute_intent(self, preparation):\n"
        "        return self.submit_prepared(preparation)\n"
    )
    violations = _classify_restricted_production_calls(
        ast.parse(source), PHASE14_RUNTIME_FILE
    )

    assert violations == []


def test_client_place_order_never_calls_the_write_boundary():
    """The compatibility wrapper must not be an unguarded fresh-write shortcut."""
    client_path = Path(__file__).resolve().parents[1] / "execution" / "binance_testnet" / "client.py"
    tree = ast.parse(client_path.read_text(encoding="utf-8"))
    offenders: list[int] = [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in {"place_order", "submit_prepared", "_authorize_attempt"}
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "self"
    ]
    assert offenders == [], f"client.py self-invokes a write-boundary call at lines {offenders}"


# --- migrated audit scenarios (runtime path) --------------------------------------

def test_migrated_audit_scenarios_use_the_runtime_path():
    from crypto_trader.audit.scenarios import (
        scenario_cancel_failure,
        scenario_network_interruption,
    )

    with tempfile.TemporaryDirectory() as workspace:
        cancel = scenario_cancel_failure(Path(workspace))
        network = scenario_network_interruption(Path(workspace))

    assert cancel.status == "pass", cancel.detail
    assert cancel.evidence["posts"] == 1
    assert cancel.evidence["attempt_marker"] is True
    assert network.status == "pass", network.detail
    assert network.evidence["write_posts"] == 1
    assert network.evidence["attempt_marker"] is True
