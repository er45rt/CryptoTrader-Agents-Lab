"""Regression tests for atomic ownership and final submission relative to takeover."""
from concurrent.futures import ThreadPoolExecutor
from multiprocessing import get_context
from threading import Barrier, Event
from time import sleep

import pytest

from crypto_trader.breakglass import OwnershipStore, OwnerKind, OwnershipStateError
from crypto_trader.persistence import SQLiteEventJournal
from crypto_trader.tests.production_fixtures import INSTRUMENT, RecordingExecutionPort, build_runtime, control_for, intent, BASE


def _takeover(path, name):
    return OwnershipStore(path).takeover(owner_id=name,owner_kind=OwnerKind.NORMAL,acquired_at=BASE,reason_code='offline-regression').term


def _process_takeover(path, name, start, result):
    start.wait(10)
    try:result.put(('ok',_takeover(path,name)))
    except Exception as exc:result.put(('error',type(exc).__name__))


def test_concurrent_threads_mint_unique_monotonic_terms(tmp_path):
    barrier=Barrier(8)
    def run(index):
        barrier.wait(5)
        return _takeover(tmp_path/'ownership.json',str(index))
    with ThreadPoolExecutor(max_workers=8) as pool:
        terms=list(pool.map(run,range(8)))
    assert sorted(terms)==list(range(1,9))


def test_independent_processes_mint_unique_monotonic_terms(tmp_path):
    context=get_context('spawn')
    start=context.Event()
    result=context.Queue()
    processes=[context.Process(target=_process_takeover,args=(str(tmp_path/'ownership.json'),str(i),start,result)) for i in range(3)]
    try:
        for process in processes:process.start()
        start.set()
        responses=[result.get(timeout=20) for _ in processes]
        for process in processes:
            process.join(20)
            assert process.exitcode==0
        assert sorted(responses)==[('ok',1),('ok',2),('ok',3)]
    finally:
        for process in processes:
            if process.is_alive():process.terminate();process.join(5)
        result.close()


def test_takeover_waits_until_admitted_send_completes(tmp_path):
    sending=Event();release=Event();takeover_started=Event();takeover_done=Event()
    class BlockingPort(RecordingExecutionPort):
        def submit(self,request):
            sending.set()
            assert release.wait(10)
            return super().submit(request)
    def submit():
        with SQLiteEventJournal(tmp_path/'journal.db') as journal:
            control=control_for(journal,tmp_path/'state')
            port=BlockingPort(INSTRUMENT)
            runtime=build_runtime(journal=journal,state_dir=tmp_path/'state',execution=port,control=control)
            runtime.start()
            result=runtime.execute_intent(intent('thread-send'))
            return result.submitted,port.submit_count
    def takeover():
        takeover_started.set()
        record=OwnershipStore(tmp_path/'state'/'ownership.json').takeover(owner_id='emergency:test',owner_kind=OwnerKind.EMERGENCY,acquired_at=BASE,reason_code='offline-regression')
        takeover_done.set()
        return record.term
    with ThreadPoolExecutor(max_workers=2) as pool:
        submitted=pool.submit(submit)
        try:
            assert sending.wait(10)
            taken=pool.submit(takeover)
            assert takeover_started.wait(5)
            assert not takeover_done.wait(0.1)
        finally:release.set()
        assert submitted.result(10)==(True,1)
        assert taken.result(10)==2


def test_reentrant_takeover_at_final_check_fails_closed(tmp_path,monkeypatch):
    with SQLiteEventJournal(tmp_path/'journal.db') as journal:
        control=control_for(journal,tmp_path/'state')
        port=RecordingExecutionPort(INSTRUMENT)
        runtime=build_runtime(journal=journal,state_dir=tmp_path/'state',execution=port,control=control)
        runtime.start()
        prepared=runtime.prepare_intent(intent('reentrant-takeover'))
        authorize=runtime._fence.authorize_new_risk
        def interleave(**kwargs):
            check=authorize(**kwargs)
            control.ownership_store.takeover(owner_id='emergency:test',owner_kind=OwnerKind.EMERGENCY,acquired_at=BASE,reason_code='offline-regression')
            return check
        monkeypatch.setattr(runtime._fence,'authorize_new_risk',interleave)
        with pytest.raises(OwnershipStateError,match='re-enter'):
            runtime.submit_prepared(prepared)
        assert port.submit_count==0


def test_lock_error_refuses_ownership_mutation(tmp_path,monkeypatch):
    from crypto_trader.breakglass import ownership
    def fail(*args,**kwargs):raise ownership.sqlite3.OperationalError('offline injected failure')
    monkeypatch.setattr(ownership.sqlite3,'connect',fail)
    with pytest.raises(OwnershipStateError,match='lock unavailable'):_takeover(tmp_path/'ownership.json','one')
    assert not (tmp_path/'ownership.json').exists()


def test_r03_competing_journal_writers_commit_one_attempt_and_post_once(tmp_path, record_property):
    from crypto_trader.tests.test_testnet_durable_submission import (
        _r03_journal, started, execution_request, CLIENT_ORDER_ID, attempt_events, posts,
    )
    from crypto_trader.tests import testnet_fixtures as fx
    path = tmp_path / "competing.db"
    with _r03_journal(path):
        pass
    barrier = Barrier(2)
    def writer(_index):
        transport = fx.ScriptedTransport([*fx.environment_responses(), fx.order_response(CLIENT_ORDER_ID)])
        with _r03_journal(path) as journal:
            port = started(transport, journal)
            prepare = port._prepare_durable_write
            def rendezvous(request):
                prepared = prepare(request)
                barrier.wait(10)
                return prepared
            port._prepare_durable_write = rendezvous
            try:
                port.submit(execution_request())
                result = "sent"
            except Exception as exc:
                result = type(exc).__name__
            return len(posts(transport)), result
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(writer, range(2)))
    with _r03_journal(path) as journal:
        attempts = attempt_events(journal)
    total = sum(count for count, _result in results)
    record_property("post_count", total)
    record_property("attempt_count", len(attempts))
    assert total == 1, results
    assert len(attempts) == 1
