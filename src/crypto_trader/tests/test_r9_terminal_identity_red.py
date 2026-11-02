"""R-9 historical terminal identity: falsifiable isolated safety probes. No POST."""
from decimal import Decimal
from types import SimpleNamespace

import pytest

from crypto_trader.execution import OrderLifecycleState
from crypto_trader.execution.binance_testnet.models import TestnetOrder
from crypto_trader.persistence import SQLiteEventJournal
from crypto_trader.production.equity import load_equity_chain
from crypto_trader.production.errors import ProductionStateError
from crypto_trader.production.risk_state import ProductionRiskStateSource
from crypto_trader.tests import test_recovery_submission_attempt as a
from crypto_trader.tests.production_fixtures import INSTRUMENT, seed_canonical_equity_root


def probe(tmp_path, *, local_identity=None, remote_identity="different-venue-order"):
    journal = SQLiteEventJournal(tmp_path / "identity.db", venue_writes=True)
    seed_canonical_equity_root(journal)
    marker = a.attempt_event(index=0, quantity="0.001", limit_price="27000").model_copy(
        update={"symbol": INSTRUMENT.symbol}
    )
    journal.append(marker)
    if local_identity is not None:
        journal.append(a.identity_event(
            local_identity, index=1, symbol=INSTRUMENT.symbol,
        ))
    journal.append(a.state_event("canceled", index=2, reason="venue_status_canceled").model_copy(
        update={"symbol": INSTRUMENT.symbol}
    ))
    chain = load_equity_chain(
        journal, symbol=INSTRUMENT.symbol, environment="binance-spot-testnet",
        venue_account_id="123456789",
        base_asset=INSTRUMENT.base_currency, quote_asset=INSTRUMENT.quote_currency,
    )
    remote = TestnetOrder(
        symbol=INSTRUMENT.symbol, client_order_id=a.CLIENT_ORDER_ID,
        venue_order_id=remote_identity,
        state=OrderLifecycleState.CANCELED, status="CANCELED", side="BUY",
        original_quantity=Decimal("0.001"), executed_quantity=Decimal("0"),
        price=Decimal("27000"),
    )
    reads = []
    def signed_order_status(symbol, client_id):
        reads.append((symbol, client_id))
        return remote
    source = object.__new__(ProductionRiskStateSource)
    source._instrument = INSTRUMENT
    source._client = SimpleNamespace(startup_order_status=signed_order_status)
    source._verified_terminal_orders = set()
    return journal, chain, source, reads


def test_unknown_local_venue_identity_cannot_vouch_for_other_terminal_order(tmp_path):
    journal, chain, source, reads = probe(tmp_path)
    try:
        with pytest.raises(ProductionStateError, match="identity|venue order"):
            source._check_terminal_execution_evidence(journal.read_range(), chain)
        assert reads == []  # Unknown local identity is rejected before any network query.
        assert not source._verified_terminal_orders
    finally:
        journal.close()


def test_known_terminal_venue_identity_matches_strict_signed_report(tmp_path):
    journal, chain, source, reads = probe(
        tmp_path, local_identity="4242", remote_identity="4242",
    )
    try:
        source._check_terminal_execution_evidence(journal.read_range(), chain)
        assert reads == [(INSTRUMENT.symbol, a.CLIENT_ORDER_ID)]
        assert len(source._verified_terminal_orders) == 1
    finally:
        journal.close()


def test_known_terminal_venue_identity_rejects_different_order(tmp_path):
    journal, chain, source, reads = probe(
        tmp_path, local_identity="4242", remote_identity="9999",
    )
    try:
        with pytest.raises(ProductionStateError, match="not proved"):
            source._check_terminal_execution_evidence(journal.read_range(), chain)
        assert len(reads) == 1
    finally:
        journal.close()


def test_startup_must_not_omit_terminal_attempt_without_verified_venue_identity(tmp_path):
    from crypto_trader.production.runtime import JournalView

    journal, _chain, _source, _reads = probe(tmp_path)
    try:
        view = JournalView.capture(journal, INSTRUMENT.symbol)
        assert a.CLIENT_ORDER_ID in view.state_references
        assert a.CLIENT_ORDER_ID in view.required_references, (
            "R-9: previous startup selects only nonterminal attempted orders, "
            "so this terminal ambiguous order is not covered by signed /order reads"
        )
    finally:
        journal.close()


def test_startup_refuses_terminal_attempt_with_missing_venue_identity(tmp_path):
    """No automatic writer RUNNING if the terminal order's venue identity is unproven."""
    from crypto_trader.tests import testnet_fixtures as fx
    from crypto_trader.tests.production_fixtures import (
        seed_canonical_equity_root, testnet_runtime as production_on_testnet,
    )
    from crypto_trader.production.errors import ProductionRecoveryRequiredError
    with SQLiteEventJournal(tmp_path / "terminal_startup.db", venue_writes=True) as journal:
        seed_canonical_equity_root(journal)
        journal.append(a.attempt_event(
            index=0, quantity="0.001", limit_price="27000",
        ).model_copy(update={"symbol": INSTRUMENT.symbol}))
        journal.append(a.state_event(
            "canceled", index=2, reason="venue_status_canceled",
        ).model_copy(update={"symbol": INSTRUMENT.symbol}))
        # Signed-looking but different venue order ID. Scripted GET only.
        responses = [
            fx.time_response(), fx.open_orders_response(),
            fx.order_response(a.CLIENT_ORDER_ID, status="CANCELED", order_id=999),
            *fx.environment_responses(),
        ]
        transport = fx.ScriptedTransport(responses)
        runtime, _port = production_on_testnet(
            journal=journal, state_dir=tmp_path / "state",
            client=fx.client(transport, retries=1),
        )
        with pytest.raises(ProductionRecoveryRequiredError, match="identity|terminal"):
            runtime.start()
        assert all(request.method == "GET" for request in transport.requests)
