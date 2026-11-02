"""Exercise durable authorization attacks against real client/runtime/journal paths."""
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from crypto_trader.execution.binance_testnet import (
    AmbiguousOrderError, AttemptJournalRequiredError, PreSendTransportError,
    ScriptedTransport, TransportTimeout,
)
from crypto_trader.persistence import SQLiteEventJournal
from crypto_trader.events import OrderSubmissionAttempted, OrderSubmissionAttemptPayload
from crypto_trader.execution.binance_testnet.translate import PRODUCER
from crypto_trader.tests import testnet_fixtures as fx
from crypto_trader.tests import test_testnet_durable_submission as durable


def test_synthetic_marker_fields_do_not_authorize_without_a_committed_marker():
    transport = ScriptedTransport([fx.exchange_info_response()])
    client = durable.client_for(transport)
    prepared = durable._fresh_prepared(client)
    with pytest.raises(AttemptJournalRequiredError):
        client._authorize_attempt(prepared, **durable._marker_fields(prepared))
    assert durable.posts(transport) == []


def test_authorization_cannot_be_reused_after_an_ambiguous_send(tmp_path):
    transport = ScriptedTransport([
        *fx.environment_responses(), TransportTimeout('unknown result'),
        fx.order_response(durable.CLIENT_ORDER_ID),
    ])
    with SQLiteEventJournal(tmp_path / 'journal.db', venue_writes=True) as journal:
        runtime = durable.started(transport, journal)
        original = runtime.client._authorize_attempt
        captured = []

        def capture(prepared, **fields):
            authorized = original(prepared, **fields)
            captured.append(authorized)
            return authorized

        runtime.client._authorize_attempt = capture
        with pytest.raises(AmbiguousOrderError):
            runtime.submit(durable.execution_request())
        with pytest.raises(AttemptJournalRequiredError):
            runtime.client.submit_prepared(captured[0])
        assert len(durable.posts(transport)) == 1
        assert len(durable.attempt_events(journal)) == 1


def test_replacing_the_authorized_http_request_is_refused_before_transport(tmp_path):
    transport = ScriptedTransport([
        *fx.environment_responses(), fx.order_response(durable.CLIENT_ORDER_ID),
    ])
    with SQLiteEventJournal(tmp_path / 'journal.db', venue_writes=True) as journal:
        runtime = durable.started(transport, journal)
        original = runtime.client._authorize_attempt

        def substitute(prepared, **fields):
            authorized = original(prepared, **fields)
            return replace(authorized, request=replace(
                authorized.request, url='https://outside.invalid/api/v3/order',
                query='symbol=ETHUSDT&signature=SYNTHETIC',
            ))

        runtime.client._authorize_attempt = substitute
        with pytest.raises(AttemptJournalRequiredError):
            runtime.submit(durable.execution_request())
        assert durable.posts(transport) == []
        assert len(durable.attempt_events(journal)) == 1


def test_prepared_http_headers_cannot_be_mutated_after_authorization():
    transport = ScriptedTransport([fx.exchange_info_response()])
    client = durable.client_for(transport)
    prepared = durable._fresh_prepared(client)
    authorized = fx.authorize_prepared(client, prepared)
    with pytest.raises(TypeError):
        authorized.request.headers['X-MBX-APIKEY'] = 'SYNTHETIC_REPLACED_KEY'
    assert durable.posts(transport) == []


@pytest.mark.parametrize('outcome, expected', [
    (fx.order_response(durable.CLIENT_ORDER_ID), None),
    (PreSendTransportError('never connected'), PreSendTransportError),
    (fx.response(503, {}), AmbiguousOrderError),
])
def test_authorization_is_single_use_for_every_transport_outcome(outcome, expected):
    transport = ScriptedTransport([
        fx.exchange_info_response(), outcome, fx.order_response(durable.CLIENT_ORDER_ID),
    ])
    client = durable.client_for(transport)
    authorized = fx.authorize_prepared(client, durable._fresh_prepared(client))
    if expected is None:
        client.submit_prepared(authorized)
    else:
        with pytest.raises(expected):
            client.submit_prepared(authorized)
    with pytest.raises(AttemptJournalRequiredError):
        client.submit_prepared(authorized)
    assert len(durable.posts(transport)) == 1


def test_empty_real_durable_journal_cannot_authorize(tmp_path):
    transport = ScriptedTransport([fx.exchange_info_response()])
    client = durable.client_for(transport)
    prepared = durable._fresh_prepared(client)
    with SQLiteEventJournal(tmp_path / 'empty.db', venue_writes=True) as journal:
        with pytest.raises(AttemptJournalRequiredError):
            client._authorize_attempt(prepared, journal=journal, **durable._marker_fields(prepared))
        assert durable.attempt_events(journal) == []
    assert durable.posts(transport) == []


def test_replacing_a_request_before_authorization_is_also_refused():
    transport = ScriptedTransport([fx.exchange_info_response()])
    client = durable.client_for(transport)
    prepared = durable._fresh_prepared(client)
    replaced = replace(prepared, request=replace(prepared.request, method='DELETE'))
    with pytest.raises(AttemptJournalRequiredError):
        fx.authorize_prepared(client, replaced)
    assert durable.posts(transport) == []


def test_a_new_marker_cannot_reauthorize_an_identity_after_ambiguity():
    transport = ScriptedTransport([fx.exchange_info_response(), TransportTimeout('unknown')])
    client = durable.client_for(transport)
    prepared = durable._fresh_prepared(client)
    with pytest.raises(AmbiguousOrderError):
        client.submit_prepared(fx.authorize_prepared(client, prepared))
    another_prepared = durable._fresh_prepared(client)
    with pytest.raises(AttemptJournalRequiredError):
        fx.authorize_prepared(client, another_prepared)
    assert len(durable.posts(transport)) == 1


def test_old_durable_marker_cannot_mint_authority_on_a_fresh_client(tmp_path):
    first_transport = ScriptedTransport([
        *fx.environment_responses(), TransportTimeout('unknown result'),
    ])
    journal_path = tmp_path / 'journal.db'
    with SQLiteEventJournal(journal_path, venue_writes=True) as journal:
        runtime = durable.started(first_transport, journal)
        captured = {}
        original = runtime.client._authorize_attempt

        def capture(prepared, **fields):
            captured.update(fields)
            return original(prepared, **fields)

        runtime.client._authorize_attempt = capture
        with pytest.raises(AmbiguousOrderError):
            runtime.submit(durable.execution_request())
    second_transport = ScriptedTransport([
        fx.exchange_info_response(), fx.order_response(durable.CLIENT_ORDER_ID),
    ])
    with SQLiteEventJournal(journal_path, venue_writes=True) as reopened:
        client = durable.client_for(second_transport)
        prepared = durable._fresh_prepared(client)
        captured['journal'] = reopened
        with pytest.raises(AttemptJournalRequiredError):
            authorized = client._authorize_attempt(prepared, **captured)
            client.submit_prepared(authorized)
        assert len(durable.attempt_events(reopened)) == 1
    assert len(durable.posts(first_transport)) == 1
    assert durable.posts(second_transport) == []


def test_subclass_equality_cannot_bridge_mismatched_marker_economics(tmp_path):
    class Spoof(str):
        def __eq__(self, other):
            return True

        def __ne__(self, other):
            return False

    transport = ScriptedTransport([fx.exchange_info_response(), fx.order_response(durable.CLIENT_ORDER_ID)])
    client = durable.client_for(transport)
    prepared = durable._fresh_prepared(client)
    fields = durable._marker_fields(prepared)
    marker = OrderSubmissionAttempted(
        event_id=fields['marker_event_id'],
        occurred_at=datetime.fromtimestamp(fx.SERVER_TIME_MS / 1000, timezone.utc),
        correlation_id=fields['correlation_id'], producer=PRODUCER,
        order_id=fields['order_id'], symbol='ETHUSDT',
        payload=OrderSubmissionAttemptPayload(**{
            key: fields[key] for key in (
                'venue_reference', 'intent_id', 'side', 'order_type',
                'quantity', 'limit_price', 'time_in_force',
            )
        }, venue_producer=PRODUCER),
    )
    fields['symbol'] = Spoof('ETHUSDT')
    with SQLiteEventJournal(tmp_path / 'journal.db', venue_writes=True) as journal:
        journal.append(marker)
        with pytest.raises(AttemptJournalRequiredError):
            client.submit_prepared(client._authorize_attempt(prepared, journal=journal, **fields))
    assert durable.posts(transport) == []


def test_preparation_refuses_decimal_subclass_economic_anchors():
    class DerivedDecimal(Decimal):
        pass

    transport = ScriptedTransport([fx.exchange_info_response()])
    client = durable.client_for(transport)
    request = replace(fx.limit_order_request(durable.CLIENT_ORDER_ID), quantity=DerivedDecimal('0.001'))
    with pytest.raises(AttemptJournalRequiredError):
        client.prepare_order(request)
    assert transport.requests == []


def test_runtime_rejects_marker_from_unexpected_producer(tmp_path):
    """P0-1: an economic match alone cannot authorize another producer."""
    transport = ScriptedTransport([
        *fx.environment_responses(), fx.order_response(durable.CLIENT_ORDER_ID),
    ])
    with SQLiteEventJournal(tmp_path / 'journal.db', venue_writes=True) as journal:
        runtime = durable.started(transport, journal)
        create = runtime._durable_attempt_event

        def foreign_producer(prepared, request):
            return create(prepared, request).model_copy(update={'producer': 'foreign.synthetic.producer'})

        runtime._durable_attempt_event = foreign_producer
        with pytest.raises(AttemptJournalRequiredError):
            runtime.submit(durable.execution_request())
        assert durable.posts(transport) == []


def test_runtime_refuses_if_writer_closed_after_authorization(tmp_path):
    """P0-1: closing the authoritative writer before the final send blocks POST."""
    transport = ScriptedTransport([
        *fx.environment_responses(), fx.order_response(durable.CLIENT_ORDER_ID),
    ])
    with SQLiteEventJournal(tmp_path / 'journal.db', venue_writes=True) as journal:
        runtime = durable.started(transport, journal)
        original = runtime._send_authorized_write

        def closed_writer(prepared, request, **kwargs):
            journal.close()
            return original(prepared, request, **kwargs)

        runtime._send_authorized_write = closed_writer
        with pytest.raises(Exception):
            runtime.submit(durable.execution_request())
        assert durable.posts(transport) == []


def test_real_https_transport_rejects_unbound_foreign_journal_before_post(tmp_path, monkeypatch):
    """P0-2 RED: real transport API must not POST from independently founded journal."""
    from crypto_trader.execution.binance_testnet.transport import RequestsTransport

    fake = ScriptedTransport([
        *fx.environment_responses(), fx.order_response(durable.CLIENT_ORDER_ID),
    ])
    with SQLiteEventJournal(tmp_path / "foreign.db", venue_writes=True) as journal:
        runtime = durable.started(fake, journal)
        live_transport = RequestsTransport()
        monkeypatch.setattr(live_transport, "send", fake.send)
        runtime.client.transport = live_transport
        with pytest.raises(AttemptJournalRequiredError, match="canonical|account"):
            runtime.submit(durable.execution_request())
        assert durable.posts(fake) == []


def test_real_https_transport_needs_authenticated_account_proof(tmp_path, monkeypatch):
    """A configured canonical path alone is not signed account authentication."""
    from crypto_trader.execution.binance_testnet.transport import RequestsTransport
    from crypto_trader.execution import ports as fence
    monkeypatch.setattr(fence, "_host_authority_root", lambda: tmp_path / "host")
    scoped = fence.CanonicalWriterScope("binance-spot-testnet", "binance-spot-testnet", "123")
    path = scoped.journal_path()
    path.parent.mkdir(parents=True)
    fake = ScriptedTransport([
        *fx.environment_responses(), fx.order_response(durable.CLIENT_ORDER_ID),
    ])
    with SQLiteEventJournal(path, venue_writes=True) as journal:
        runtime = durable.started(fake, journal)
        runtime.client.canonical_scope = scoped
        live_transport = RequestsTransport()
        monkeypatch.setattr(live_transport, "send", fake.send)
        runtime.client.transport = live_transport
        with pytest.raises(AttemptJournalRequiredError, match="account|canonical"):
            runtime.submit(durable.execution_request())
        assert durable.posts(fake) == []


def test_real_https_transport_refuses_signed_account_without_held_writer_proof(
    tmp_path, monkeypatch,
):
    """Synthetic responses model one signed startup account: no network performed."""
    from crypto_trader.execution.binance_testnet.transport import RequestsTransport
    from crypto_trader.execution import ports as fence

    monkeypatch.setattr(fence, "_host_authority_root", lambda: tmp_path / "host")
    scoped = fence.CanonicalWriterScope(
        "binance-spot-testnet", "binance-spot-testnet", "123456789",
    )
    path = scoped.journal_path()
    path.parent.mkdir(parents=True)
    fake = ScriptedTransport([
        *fx.environment_responses(),
        fx.account_response(),
        fx.order_response(durable.CLIENT_ORDER_ID),
    ])
    with SQLiteEventJournal(path, venue_writes=True) as journal:
        runtime = durable.started(fake, journal)
        snapshot = runtime.client.startup_account_snapshot("BTC", "USDT")
        assert snapshot.venue_account_id == scoped.account_id
        runtime.client.canonical_scope = scoped
        live_transport = RequestsTransport()
        monkeypatch.setattr(live_transport, "send", fake.send)
        runtime.client.transport = live_transport
        with pytest.raises(AttemptJournalRequiredError):
            runtime.submit(durable.execution_request())
        assert durable.posts(fake) == []
        assert durable.attempt_events(journal) == []
