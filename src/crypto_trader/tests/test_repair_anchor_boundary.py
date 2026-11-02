"""A current local lease is not independent monotonic authority."""
import pytest
from crypto_trader.persistence import SQLiteEventJournal
from crypto_trader.execution import ports
from crypto_trader.breakglass.ownership import OwnershipStore, WriterGuard
from crypto_trader.execution.binance_testnet import AttemptJournalRequiredError
from crypto_trader.tests.test_release_authority_report_recovery_integration import (
    scope, _direct, _hold_lease, _emergency, _high_water, durable, fx,
)


def test_aba_same_term_different_session_never_revives_old_guard(scope):
    with SQLiteEventJournal(scope.journal_path(), venue_writes=True) as journal:
        runtime, _ = _direct(scope, journal, [])
        _hold_lease(runtime, scope, journal)
        old = scope.ownership_path().read_bytes()
        high = _high_water(scope).read_bytes()
        stale = _hold_lease(runtime, scope, journal)
        scope.ownership_path().write_bytes(old)
        _high_water(scope).write_bytes(high)
        current = _hold_lease(runtime, scope, journal)
        assert stale.guard.fence.term == current.guard.fence.term
        assert stale.guard.fence.session_id != current.guard.fence.session_id
        from crypto_trader.breakglass.errors import StaleWriterFenceError
        with pytest.raises(StaleWriterFenceError):
            stale.guard.require_current()
        current.guard.require_current()


def test_second_intent_after_offline_closed_order_cannot_use_real_sender(scope):
    from crypto_trader.tests.test_release_authority_report_recovery_integration import _second_request
    with SQLiteEventJournal(scope.journal_path(), venue_writes=True) as journal:
        runtime, fake = _direct(scope, journal, [fx.order_response(durable.CLIENT_ORDER_ID, status='CANCELED')])
        real = runtime.client.transport
        runtime.client.transport = fake
        # Seed a completed, zero-fill historical order using the offline transport.
        runtime.submit(durable.execution_request())
        assert len(durable.posts(fake)) == 1
        runtime.client.transport = real
        request = _second_request()
        _hold_lease(runtime, scope, journal, request)
        with pytest.raises(AttemptJournalRequiredError, match='independent monotonic'):
            runtime.submit(request)
        assert len(durable.posts(fake)) == 1


@pytest.mark.parametrize('reader', ['static', 'self_derived'])
def test_checkpoint_reader_cannot_enable_real_writer(scope, reader):
    from crypto_trader.tests import test_release_authority_report_recovery_integration as it
    from crypto_trader.production.origin_audit import CrewOriginAudit
    from crypto_trader.production.runtime import CrewOriginAuthority
    from crypto_trader.crew_decision_gateway import CrewDecisionGateway
    from crypto_trader.tests.production_fixtures import seed_canonical_equity_root, admitted_execute
    with SQLiteEventJournal(scope.journal_path(), venue_writes=True) as journal:
        seed_canonical_equity_root(journal)
        proofs = scope.state_dir() / 'proofs'
        proofs.mkdir()
        witness = {}
        audit = CrewOriginAudit(proofs, key=it.KEY, journal_id=journal.journal_id(),
                                checkpoint_reader=lambda: dict(witness) if reader == 'static' else audit.export_checkpoint(journal))
        witness.update(audit.export_checkpoint(journal))
        authority = CrewOriginAuthority(audit=audit)
        runtime, port, fake = it._keyed(journal, scope.state_dir(), authority,
            https='at_construction', require_authenticated_origin_audit=True, require_rollback_checkpoint=True)
        fake.responses[5:5] = it.production._risk_refresh_responses()
        runtime.start()
        gateway = CrewDecisionGateway(journal=journal, runtime=runtime, authority=authority,
            producer='gateway.crew_strategist', policy=lambda parsed: it.fresh('anchor-regression'))
        candidate, _ = it.trusted_issue(gateway, it.plan(), scope.state_dir())
        with pytest.raises(AttemptJournalRequiredError, match='independent monotonic'):
            admitted_execute(runtime, candidate)
        assert durable.posts(fake) == []
        assert len(durable.attempt_events(journal)) == 1
        runtime.stop()


@pytest.mark.parametrize('rollback', [False, True])
def test_real_sender_refuses_without_independent_anchor_even_after_rollback(scope, rollback):
    with SQLiteEventJournal(scope.journal_path(), venue_writes=True) as journal:
        runtime, fake = _direct(scope, journal, [fx.order_response(durable.CLIENT_ORDER_ID)])
        _hold_lease(runtime, scope, journal)
        if rollback:
            old = scope.ownership_path().read_bytes()
            high = _high_water(scope).read_bytes()
            _emergency(scope, journal)
            scope.ownership_path().write_bytes(old)
            _high_water(scope).write_bytes(high)
        with pytest.raises(AttemptJournalRequiredError, match='independent monotonic'):
            runtime.submit(durable.execution_request())
        assert durable.posts(fake) == []


def test_restored_journal_ownership_and_high_water_never_authorize_https(scope):
    """E1/S3: total local rollback cannot restore permission for a real sender."""
    from crypto_trader.tests.test_release_authority_report_recovery_integration import _second_request
    from crypto_trader.events import OrderSubmissionAttempted

    path = scope.journal_path()
    with SQLiteEventJournal(path, venue_writes=True) as journal:
        runtime, fake = _direct(scope, journal, [])
        lease = _hold_lease(runtime, scope, journal)
        lease.require_current(journal)
        assert durable.posts(fake) == []

    # Snapshot an apparently valid NORMAL owner, the full SQLite journal, and
    # the anti-ABA high-water file; all are inside the rollback domain.
    original_journal = path.read_bytes()
    original_owner = scope.ownership_path().read_bytes()
    original_water = _high_water(scope).read_bytes()

    with SQLiteEventJournal(path, venue_writes=True) as journal:
        runtime, fake = _direct(scope, journal, [fx.order_response(durable.CLIENT_ORDER_ID)])
        # Reconstruct the originally held lease rather than mint a new takeover.
        store = OwnershipStore(scope.ownership_path())
        request = durable.execution_request()
        runtime.client._canonical_writer_lease = ports.CanonicalWriterLease(
            scope=scope, journal=journal, store=store,
            guard=WriterGuard(store, store.require_fence()), request=request,
        )
        with pytest.raises(AttemptJournalRequiredError, match="independent monotonic"):
            runtime.submit(request)
        assert durable.posts(fake) == []
        assert [e for e in journal.read_range() if isinstance(e, OrderSubmissionAttempted)]
        _emergency(scope, journal)

    # Complete local rollback: journal plus both owner and high-water evidence.
    path.write_bytes(original_journal)
    scope.ownership_path().write_bytes(original_owner)
    _high_water(scope).write_bytes(original_water)

    with SQLiteEventJournal(path, venue_writes=True) as journal:
        runtime, fake = _direct(scope, journal, [fx.order_response(durable.CLIENT_ORDER_ID)])
        store = OwnershipStore(scope.ownership_path())
        order = _second_request()
        lease = ports.CanonicalWriterLease(
            scope=scope, journal=journal, store=store,
            guard=WriterGuard(store, store.require_fence()), request=order,
        )
        lease.require_current(journal)  # All restored *local* checks would pass.
        runtime.client._canonical_writer_lease = lease
        with pytest.raises(AttemptJournalRequiredError, match="independent monotonic"):
            runtime.submit(order)
        assert durable.posts(fake) == []
        assert len(durable.attempt_events(journal)) == 1
