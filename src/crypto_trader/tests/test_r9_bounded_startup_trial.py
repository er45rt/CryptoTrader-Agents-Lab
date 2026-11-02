"""Read-only R-9 signed startup lookup budget probe. S2 is NOT enabled."""
from types import SimpleNamespace
from uuid import uuid4

import pytest

from crypto_trader.execution.binance_testnet import TestnetOrderRuntime
from crypto_trader.execution.binance_testnet.startup import BinanceStartupCollector, StartupEvidenceError
from crypto_trader.persistence import SQLiteEventJournal
from crypto_trader.tests import testnet_fixtures as fx
from crypto_trader.tests.production_fixtures import INSTRUMENT, BASE


def probe(tmp_path, responses, *, budget):
    journal = SQLiteEventJournal(tmp_path / "r9-budget.db", venue_writes=True)
    transport = fx.ScriptedTransport(responses)
    client = fx.client(transport, retries=1)
    port = TestnetOrderRuntime(client=client, instrument=INSTRUMENT, journal=journal)
    collector = BinanceStartupCollector(
        port, journal=journal, instrument=INSTRUMENT, endpoint=client.base_url,
        max_status_queries=budget,
    )
    return journal, collector, transport


def view(refs):
    return SimpleNamespace(
        required_references=tuple(refs), fingerprint="synthetic-r9-budget-view",
    )


@pytest.mark.parametrize("n", [3, 10, 1000])
def test_over_budget_refuses_before_any_signed_order_status_query(tmp_path, n):
    journal, collector, transport = probe(
        tmp_path, [fx.time_response(), fx.open_orders_response()], budget=2,
    )
    try:
        with pytest.raises(StartupEvidenceError, match="status budget exceeded"):
            collector.collect(
                nonce=uuid4(), fence_term=1, view=view([f"ct-order-{i:05}" for i in range(n)]),
                clock=lambda: BASE,
            )
        assert len(transport.requests) == 2
        assert all(not r.url.endswith("/order") for r in transport.requests)
        assert all(r.method == "GET" for r in transport.requests)
        assert journal.count() == 0
    finally:
        journal.close()


def test_budget_zero_allows_zero_signed_status_reads(tmp_path):
    journal, collector, transport = probe(
        tmp_path, [fx.time_response(), fx.open_orders_response()], budget=0,
    )
    try:
        batch = collector.collect(
            nonce=uuid4(), fence_term=1, view=view(()), clock=lambda: BASE,
        )
        assert batch.status_references == ()
        assert len(transport.requests) == 2
        assert not any(r.method == "POST" for r in transport.requests)
    finally:
        journal.close()


def test_duplicate_outstanding_status_identity_refused_without_query(tmp_path):
    journal, collector, transport = probe(
        tmp_path, [fx.time_response(), fx.open_orders_response()], budget=4,
    )
    try:
        with pytest.raises(StartupEvidenceError, match="identities must be unique"):
            collector.collect(
                nonce=uuid4(), fence_term=1,
                view=view(("duplicate-id", "duplicate-id")), clock=lambda: BASE,
            )
        assert len(transport.requests) == 2 and journal.count() == 0
    finally:
        journal.close()


@pytest.mark.parametrize("budget", [-1, True, 10001, "10", 0.5])
def test_invalid_budget_fails_at_constructor_without_network(tmp_path, budget):
    journal = SQLiteEventJournal(tmp_path / "r9-budget.db", venue_writes=True)
    try:
        transport = fx.ScriptedTransport([])
        client = fx.client(transport, retries=1)
        port = TestnetOrderRuntime(client=client, instrument=INSTRUMENT, journal=journal)
        with pytest.raises(StartupEvidenceError, match="budget"):
            BinanceStartupCollector(
                port, journal=journal, instrument=INSTRUMENT,
                endpoint=client.base_url, max_status_queries=budget,
            )
        assert not transport.requests
    finally:
        journal.close()

def test_within_budget_fetches_every_required_status_once(tmp_path):
    reference = "ct-budget-1"
    journal, collector, transport = probe(
        tmp_path, [
            fx.time_response(), fx.open_orders_response(),
            fx.order_response(reference, status="CANCELED", order_id=42),
        ],
        budget=1,
    )
    try:
        batch = collector.collect(
            nonce=uuid4(), fence_term=1,
            view=view((reference,)), clock=lambda: BASE,
        )
        assert batch.status_references == (reference,)
        assert batch.required_references == (reference,)
        assert len(transport.requests) == 3
        assert len([r for r in transport.requests if r.url.endswith("/order")]) == 1
        assert all(r.method == "GET" for r in transport.requests)
        assert journal.count() == 0
    finally:
        journal.close()
