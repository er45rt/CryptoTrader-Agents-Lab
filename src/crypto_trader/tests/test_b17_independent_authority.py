"""Offline separately retained authority. No claim of deployed storage independence."""
import hashlib
from dataclasses import replace
from datetime import datetime, timezone
from uuid import uuid4
import pytest
from crypto_trader.persistence import SQLiteEventJournal
from crypto_trader.breakglass import BreakGlassControl, OwnerKind
from crypto_trader.breakglass.ownership import OwnershipStore, WriterGuard
from crypto_trader.breakglass.errors import OwnershipStateError
from crypto_trader.tests.test_breakglass_control import command, BASE
from crypto_trader.recovery import load_policy
from crypto_trader.tests.test_release_authority_report_recovery_integration import scope
from crypto_trader.tests import test_release_authority_report_recovery_integration as it

def authority_at(path,journal,scope):
    from crypto_trader.breakglass.independent_authority import SQLiteIndependentAuthority
    identity=uuid4()
    SQLiteIndependentAuthority.provision_offline(path,authority_id=identity,scope=scope,journal=journal)
    return SQLiteIndependentAuthority(path,authority_id=identity,scope=scope,journal_id=journal.journal_id())

def take(store,journal,owner='normal',kind=OwnerKind.NORMAL):
    return store.takeover(owner_id=owner,owner_kind=kind,acquired_at=datetime.now(timezone.utc),reason_code='offline-test',journal_path=journal.path,journal_id=journal.journal_id())

def test_separate_authority_rejects_restored_owner_pair(scope,tmp_path):
    with SQLiteEventJournal(scope.journal_path(),venue_writes=True) as journal:
        authority=authority_at(tmp_path/'anchor.db',journal,scope)
        store=OwnershipStore(scope.ownership_path(),independent_authority=authority)
        a=take(store,journal); old=store.path.read_bytes(); high=store._high_water_path.read_bytes()
        b=take(store,journal,'emergency',OwnerKind.EMERGENCY)
        store.path.write_bytes(old); store._high_water_path.write_bytes(high)
        with pytest.raises(OwnershipStateError): store.require_current(a.fence)
        with pytest.raises(OwnershipStateError): take(store,journal,'stale-reclaim')
        assert authority.current_writer()==b

def test_attempt_reservation_survives_restart_and_local_rollback(scope,tmp_path):
    with SQLiteEventJournal(scope.journal_path(),venue_writes=True) as journal:
        pass
    snapshot=scope.journal_path().read_bytes()
    with SQLiteEventJournal(scope.journal_path(),venue_writes=True) as journal:
        authority=authority_at(tmp_path/'anchor.db',journal,scope)
        store=OwnershipStore(scope.ownership_path(),independent_authority=authority)
        record=take(store,journal)
        runtime,fake=it._direct(scope,journal,[it.fx.order_response(it.durable.CLIENT_ORDER_ID)])
        runtime.client.transport=fake
        runtime.submit(it.durable.execution_request())
        marker=it.durable.attempt_events(journal)[0]
        authority.reserve_attempt(record,journal,marker)
        with pytest.raises(OwnershipStateError): authority.reserve_attempt(record,journal,marker)
        cls=type(authority)
        reopened=cls(authority.path,authority_id=authority.authority_id,scope=scope,journal_id=journal.journal_id())
        with pytest.raises(OwnershipStateError): reopened.reserve_attempt(record,journal,marker)
    scope.journal_path().write_bytes(snapshot)
    with SQLiteEventJournal(scope.journal_path(),venue_writes=True) as journal:
        with pytest.raises(OwnershipStateError): reopened.verify_journal(journal)
        from crypto_trader.execution.ports import CanonicalWriterLease
        with pytest.raises(OwnershipStateError):
            CanonicalWriterLease(scope=scope,journal=journal,store=store,
                guard=WriterGuard(store,record.fence),request=it.durable.execution_request())

@pytest.mark.parametrize('failure',['missing','identity','scope','journal'])
def test_authority_unavailable_or_wrong_binding_fails_closed(scope,tmp_path,failure):
    with SQLiteEventJournal(scope.journal_path(),venue_writes=True) as journal:
        authority=authority_at(tmp_path/'anchor.db',journal,scope); cls=type(authority)
        if failure=='missing': authority.path.unlink()
        with pytest.raises(OwnershipStateError):
            cls(authority.path,authority_id=uuid4() if failure=='identity' else authority.authority_id,
                scope=replace(scope,account_id='12345') if failure=='scope' else scope,
                journal_id=uuid4() if failure=='journal' else journal.journal_id()).current_writer()

def test_corrupt_water_recovery_requires_reconcile_ack_and_independent_authority(scope,tmp_path):
    with SQLiteEventJournal(scope.journal_path(),venue_writes=True) as journal:
        authority=authority_at(tmp_path/'anchor.db',journal,scope)
        control=BreakGlassControl(journal=journal,state_dir=scope.state_dir(),clock=lambda:BASE,
            reconciliation_policy=load_policy({'policy_id':'offline'}),independent_authority=authority)
        guard=control.claim_normal_writer(owner_id='old')
        water=control.ownership_store._high_water_path; water.write_bytes(b'corrupt')
        digest=hashlib.sha256(b'corrupt').hexdigest(); rid=uuid4()
        assert control.handle(command('halt_new_risk')).accepted
        with pytest.raises(OwnershipStateError): control.recover_writer_high_water(recovery_id=rid,expected_sha256=digest,reason='offline verified recovery')
        assert control.handle(command('reconcile')).reconciliation_status=='resolved'
        assert control.handle(command('acknowledge')).accepted
        with pytest.raises(OwnershipStateError): control.recover_writer_high_water(recovery_id=rid,expected_sha256='0'*64,reason='wrong evidence')
        before=control.ownership_store.read()
        repaired=control.recover_writer_high_water(recovery_id=rid,expected_sha256=digest,reason='offline verified recovery')
        assert repaired.owner_kind is OwnerKind.EMERGENCY and repaired.term>before.term
        assert not control.status().new_risk_permitted
        assert control.handle(command('restore_normal')).accepted
        control.claim_normal_writer(owner_id='new').require_current()
        with pytest.raises(OwnershipStateError): guard.require_current()

def test_recovery_crash_after_independent_commit_can_resume_same_receipt(scope,tmp_path,monkeypatch):
    with SQLiteEventJournal(scope.journal_path(),venue_writes=True) as journal:
        authority=authority_at(tmp_path/'anchor.db',journal,scope)
        control=BreakGlassControl(journal=journal,state_dir=scope.state_dir(),clock=lambda:BASE,
            reconciliation_policy=load_policy({'policy_id':'offline'}),independent_authority=authority)
        control.claim_normal_writer(owner_id='old'); store=control.ownership_store
        store._high_water_path.write_bytes(b'broken'); digest=hashlib.sha256(b'broken').hexdigest(); rid=uuid4()
        control.handle(command('halt_new_risk')); control.handle(command('reconcile')); control.handle(command('acknowledge'))
        def crash(record): raise OSError('offline crash after water, before record')
        with monkeypatch.context() as m:
            m.setattr(store,'_write',crash)
            with pytest.raises(OSError): control.recover_writer_high_water(recovery_id=rid,expected_sha256=digest,reason='recovery')
        with pytest.raises(OwnershipStateError): store.require_current(store.read().fence)
        result=control.recover_writer_high_water(recovery_id=rid,expected_sha256=digest,reason='recovery')
        assert result==authority.current_writer()
        assert control.handle(command('restore_normal')).accepted

def test_no_independent_authority_never_repairs_water(scope):
    with SQLiteEventJournal(scope.journal_path(),venue_writes=True) as journal:
        control=BreakGlassControl(journal=journal,state_dir=scope.state_dir(),clock=lambda:BASE,reconciliation_policy=load_policy({'policy_id':'offline'}))
        control.claim_normal_writer(owner_id='old'); water=control.ownership_store._high_water_path; water.write_bytes(b'broken')
        control.handle(command('halt_new_risk')); control.handle(command('reconcile')); control.handle(command('acknowledge'))
        with pytest.raises(OwnershipStateError): control.recover_writer_high_water(recovery_id=uuid4(),expected_sha256=hashlib.sha256(b'broken').hexdigest(),reason='recovery')
        assert water.read_bytes()==b'broken'

def test_normal_emergency_restore_keeps_existing_epoch_contract(scope,tmp_path):
    with SQLiteEventJournal(scope.journal_path(),venue_writes=True) as journal:
        authority=authority_at(tmp_path/'anchor.db',journal,scope)
        control=BreakGlassControl(journal=journal,state_dir=scope.state_dir(),clock=lambda:BASE,
            reconciliation_policy=load_policy({'policy_id':'offline'}),independent_authority=authority)
        old=control.claim_normal_writer(owner_id='old')
        control.handle(command('halt_new_risk')); control.handle(command('reconcile')); control.handle(command('acknowledge'))
        assert control.handle(command('restore_normal')).accepted
        control.claim_normal_writer(owner_id='new').require_current()
        with pytest.raises(OwnershipStateError): old.require_current()

def test_durable_port_advances_authority_before_final_creation_denial(scope,tmp_path):
    from crypto_trader.execution import ports
    from crypto_trader.execution.binance_testnet import AttemptJournalRequiredError
    with SQLiteEventJournal(scope.journal_path(),venue_writes=True) as journal:
        authority=authority_at(tmp_path/'anchor.db',journal,scope)
        store=OwnershipStore(scope.ownership_path(),independent_authority=authority)
        record=take(store,journal)
        runtime,fake=it._direct(scope,journal,[])
        runtime.client._canonical_writer_lease=ports.CanonicalWriterLease(scope=scope,journal=journal,
            store=store,guard=WriterGuard(store,record.fence),request=it.durable.execution_request())
        with pytest.raises(AttemptJournalRequiredError,match='independent monotonic'):
            runtime.submit(it.durable.execution_request())
        marker=it.durable.attempt_events(journal)[0]
        with pytest.raises(OwnershipStateError,match='already reserved'): authority.reserve_attempt(record,journal,marker)
        assert it.durable.posts(fake)==[]

def test_authority_cannot_be_provisioned_over_existing_enrollment(scope,tmp_path):
    with SQLiteEventJournal(scope.journal_path(),venue_writes=True) as journal:
        authority=authority_at(tmp_path/'anchor.db',journal,scope)
        before=authority.path.read_bytes()
        with pytest.raises(FileExistsError):
            type(authority).provision_offline(authority.path,authority_id=uuid4(),scope=scope,journal=journal)
        assert authority.path.read_bytes()==before

def test_emergency_fences_independent_advance_when_local_write_was_lost(scope,tmp_path,monkeypatch):
    with SQLiteEventJournal(scope.journal_path(),venue_writes=True) as journal:
        authority=authority_at(tmp_path/'anchor.db',journal,scope)
        store=OwnershipStore(scope.ownership_path(),independent_authority=authority)
        old=take(store,journal)
        def crash(record): raise OSError('lost local owner write')
        with monkeypatch.context() as m:
            m.setattr(store,'_write',crash)
            with pytest.raises(OSError): take(store,journal,'interrupted')
        with pytest.raises(OwnershipStateError): store.require_current(old.fence)
        emergency=take(store,journal,'emergency',OwnerKind.EMERGENCY)
        assert emergency.term>old.term+1 and emergency==authority.current_writer()

def _reserve_worker(path,identity,scope,journal_path,record,marker_id,ready,queue):
    from crypto_trader.breakglass.independent_authority import SQLiteIndependentAuthority
    try:
        with SQLiteEventJournal(journal_path,venue_writes=True) as journal:
            authority=SQLiteIndependentAuthority(path,authority_id=identity,scope=scope,journal_id=journal.journal_id())
            assert ready.wait(30)
            try:
                authority.reserve_attempt(record,journal,journal.read_by_id(marker_id))
            except OwnershipStateError:
                queue.put('refused')
            else:
                queue.put('reserved')
    except BaseException as exc:
        queue.put('setup:'+type(exc).__name__)

def test_two_processes_reserve_same_intent_only_once(scope,tmp_path):
    from multiprocessing import get_context
    with SQLiteEventJournal(scope.journal_path(),venue_writes=True) as journal:
        authority=authority_at(tmp_path/'anchor.db',journal,scope)
        store=OwnershipStore(scope.ownership_path(),independent_authority=authority); record=take(store,journal)
        runtime,fake=it._direct(scope,journal,[it.fx.order_response(it.durable.CLIENT_ORDER_ID)])
        runtime.client.transport=fake; runtime.submit(it.durable.execution_request())
        marker=it.durable.attempt_events(journal)[0]
    ctx=get_context('spawn'); ready=ctx.Event(); queue=ctx.Queue()
    children=[ctx.Process(target=_reserve_worker,args=(authority.path,authority.authority_id,scope,scope.journal_path(),record,marker.event_id,ready,queue)) for _ in range(2)]
    try:
        for child in children: child.start()
        ready.set()
        assert sorted(queue.get(timeout=60) for _ in children)==['refused','reserved']
        for child in children:
            child.join(30); assert child.exitcode==0
    finally:
        for child in children:
            if child.is_alive(): child.terminate(); child.join(5)
        queue.close()

def test_lost_authority_commit_ack_never_allows_second_reservation(scope,tmp_path,monkeypatch):
    from contextlib import contextmanager
    with SQLiteEventJournal(scope.journal_path(),venue_writes=True) as journal:
        authority=authority_at(tmp_path/'anchor.db',journal,scope)
        store=OwnershipStore(scope.ownership_path(),independent_authority=authority); record=take(store,journal)
        runtime,fake=it._direct(scope,journal,[it.fx.order_response(it.durable.CLIENT_ORDER_ID)])
        runtime.client.transport=fake; runtime.submit(it.durable.execution_request())
        marker=it.durable.attempt_events(journal)[0]; transaction=authority._transaction
        @contextmanager
        def lose_ack():
            with transaction() as state: yield state
            raise OwnershipStateError('offline response lost after durable commit')
        with monkeypatch.context() as m:
            m.setattr(authority,'_transaction',lose_ack)
            with pytest.raises(OwnershipStateError,match='response lost'): authority.reserve_attempt(record,journal,marker)
        with pytest.raises(OwnershipStateError,match='already reserved'): authority.reserve_attempt(record,journal,marker)
