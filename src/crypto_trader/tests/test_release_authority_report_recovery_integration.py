"""I01-I08: writer authority, strict reports and recovery on one shared canonical journal.

Every HTTP answer is scripted; `RequestsTransport.send` is replaced before any use and sockets are
refused. P-06 strict reports consume native-paper provenance only: venue events carry no causation
link, so I01/I06/I07 pin that boundary rather than assume a bridge between the two event families.
"""
import os
import shutil
import socket
import time
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from multiprocessing import get_context
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest

from crypto_trader.breakglass.ownership import OwnerKind, OwnershipStore, WriterGuard
from crypto_trader.breakglass.errors import StaleWriterFenceError
from crypto_trader.crew_decision_gateway import CrewDecisionGateway
from crypto_trader.events import (
    ORDER_STATE_EVENT_TYPE, TRADE_INTENT_EVENT_TYPE,
    OrderSubmissionAttempted, OrderVenueIdentityRecorded,
)
from crypto_trader.execution import ExecutionRequest, ports
from crypto_trader.execution.binance_testnet import (
    AmbiguousOrderError, AttemptJournalRequiredError, TestnetOrderRuntime, TransportTimeout,
)
from crypto_trader.execution.binance_testnet.transport import RequestsTransport, ScriptedTransport
from crypto_trader.execution.nautilus import NautilusSimulationRunner
from crypto_trader.paper import PaperStateError, report_from_journal
from crypto_trader.persistence import SQLiteEventJournal
from crypto_trader.production import ProductionRecoveryRequiredError, ProductionStateError
from crypto_trader.production.origin_audit import CrewOriginAudit
from crypto_trader.production.runtime import CrewOriginAuthority
from crypto_trader.tests import paper_fixtures as paper_fx
from crypto_trader.tests import test_production_testnet_integration as production
from crypto_trader.tests import test_testnet_durable_submission as durable
from crypto_trader.tests import testnet_fixtures as fx
from crypto_trader.tests.production_fixtures import (
    INSTRUMENT, admitted_execute, build_runtime, canonical_test_risk_source, control_for,
    enable_https_behind_checkpoint_gate, intent, seed_canonical_equity_root, startup_responses,
)
from crypto_trader.tests.test_paper_nautilus import aligned_runner
from crypto_trader.tests.test_r03a1b_crew_gateway import plan, trusted_issue
from crypto_trader.tests.test_r03a1b_origin_binding import NOW, fresh

VENUE = "binance-spot-testnet"
ACCOUNT = "123456789"
KEY = b"offline-test-host-secret-never-live" * 2
STRATEGY = "strategy-paper-1"


@pytest.fixture(autouse=True)
def refuse_real_network(monkeypatch):
    def refused(*args, **kwargs):
        pytest.fail("an unscripted network path was reached")
    monkeypatch.setattr(RequestsTransport, "send", refused)
    monkeypatch.setattr(socket, "create_connection", refused)
    monkeypatch.setattr(socket.socket, "connect", refused)
    monkeypatch.setattr(socket.socket, "connect_ex", refused)


@pytest.fixture
def scope(tmp_path, monkeypatch):
    monkeypatch.setattr(ports, "_host_authority_root", lambda: tmp_path / "host")
    canonical = ports.CanonicalWriterScope(VENUE, VENUE, ACCOUNT)
    canonical.state_dir().mkdir(parents=True)
    return canonical


def _https(fake):
    https = RequestsTransport()
    https.send = fake.send  # The external operation alone is replaced.
    return https


def _direct(scope, journal, responses):
    """A started adapter whose only sender is the scripted replacement of the real HTTPS send."""
    fake = ScriptedTransport([*fx.environment_responses(), fx.account_response(), *responses])
    runtime = durable.started(_https(fake), journal)
    assert runtime.client.startup_account_snapshot("BTC", "USDT").venue_account_id == ACCOUNT
    runtime.client.canonical_scope = scope
    return runtime, fake


def _hold_lease(runtime, scope, journal, request=None, *, owner="offline-normal"):
    store = OwnershipStore(scope.ownership_path())
    record = store.takeover(
        owner_id=owner, owner_kind=OwnerKind.NORMAL, acquired_at=datetime.now(timezone.utc),
        reason_code="offline-test", journal_path=str(journal.path), journal_id=journal.journal_id(),
    )
    lease = ports.CanonicalWriterLease(
        scope=scope, journal=journal, store=store, guard=WriterGuard(store, record.fence),
        request=request or durable.execution_request(),
    )
    runtime.client._canonical_writer_lease = lease
    return lease


def _emergency(scope, journal):
    return OwnershipStore(scope.ownership_path()).takeover(
        owner_id="offline-emergency", owner_kind=OwnerKind.EMERGENCY,
        acquired_at=datetime.now(timezone.utc), reason_code="offline-test",
        journal_path=str(journal.path), journal_id=journal.journal_id(),
    )


def _capture_handle(runtime, before_send):
    """Run `before_send` after the marker and authorization exist, keeping the one-shot handle."""
    original = runtime._send_authorized_write
    captured = []

    def hook(prepared, request, **kwargs):
        captured.append(prepared.handle)
        before_send()
        return original(prepared, request, **kwargs)

    runtime._send_authorized_write = hook
    return captured


def _refused_replay(runtime, handle, journal, fake):
    with pytest.raises(AttemptJournalRequiredError):
        runtime.client.submit_prepared(handle, journal=journal)
    assert durable.posts(fake) == []


def _restart_refuses_blind_resubmit(scope, journal, request=None):
    """A fresh adapter on the reopened journal: reservation read only, never a POST."""
    runtime, fake = _direct(scope, journal, [fx.rejected(-2013, "Order does not exist.")])
    with pytest.raises(AmbiguousOrderError):
        runtime.submit(request or durable.execution_request())
    assert durable.posts(fake) == []
    assert len(durable.attempt_events(journal)) == 1


def _rewire(journal, state_dir, responses, *, https=False, **kwargs):
    """Offline recovery uses ScriptedTransport; HTTPS construction tests opt in explicitly.

    B2 now rejects the old post-construction checkpoint bypass at start(). Successful
    recovery tests cannot use that invalid fixture to stand in for an enabled writer.
    """
    fake = ScriptedTransport(list(responses))
    client = fx.client(fake, retries=1)
    if https == "at_construction":
        client.transport = _https(fake)
    port = TestnetOrderRuntime(client=client, instrument=INSTRUMENT, journal=journal)
    control = control_for(journal, state_dir)
    risk_source = canonical_test_risk_source(journal=journal, control=control, client=client)
    runtime = build_runtime(
        journal=journal, state_dir=state_dir, execution=port, control=control,
        risk_state_source=risk_source, bind_admission=True, **kwargs,
    )
    if https == "behind_gate":
        enable_https_behind_checkpoint_gate(runtime, _https(fake))
    return runtime, port, fake


def _admit(scope, journal, order, write_answer, *, simulated=False):
    """Use exact ScriptedTransport for offline send; HTTPS mode must fail closed."""
    https_fake = ScriptedTransport([*startup_responses(), write_answer])
    client = fx.client(https_fake)
    runtime, port, _ = production._wired(journal, scope.state_dir(), https_fake, client=client)
    if not simulated:
        enable_https_behind_checkpoint_gate(runtime, _https(https_fake))
    runtime.start()
    return runtime, port, https_fake


def _native_session(journal):
    """A real native-paper run, on the same journal as the venue events."""
    original = aligned_runner()
    simulator = NautilusSimulationRunner(
        instrument=original.instrument, book_updates=original.book_updates,
        trades=(*original.trades[:-1], replace(
            original.trades[-1], size=Decimal("0.60000000"), trade_id="I-STRICT-PARTIAL")),
    )
    paper_fx.session(
        journal, decisions=paper_fx.scripted(paper_fx.buy_decision(quantity="1.5", limit_price="2700.00")),
        data_feed=paper_fx.feed("2700.00"), simulator=simulator,
    ).run()


def _strict(events, *, strict=True):
    return report_from_journal(
        events, session_id=paper_fx.SESSION_ID, mode="paper", symbol="ETHUSDT",
        strategy_id=STRATEGY, strict_native_provenance=strict,
    )


def _durable_intent(events):
    found = {e.event_id for e in events
             if e.event_type == TRADE_INTENT_EVENT_TYPE and e.correlation_id == paper_fx.SESSION_ID}
    assert len(found) == 1
    return next(iter(found))


def test_i01_admission_restart_and_strict_replay_never_add_a_post(scope):
    order = intent("i01", quantity=Decimal("0.00100"))
    reference = production._reference_for(order)
    path = scope.journal_path()
    with SQLiteEventJournal(path, venue_writes=True) as journal:
        runtime, _, fake = _admit(scope, journal, order, fx.order_response(reference, order_id=100), simulated=True)
        assert admitted_execute(runtime, order).submitted is True
        assert len(durable.posts(fake)) == 1
        runtime.stop()
    replies = [fx.time_response(), fx.open_orders_response(reference), *fx.environment_responses()]
    with SQLiteEventJournal(path, venue_writes=True) as journal:
        restarted, _, fake = _rewire(journal, scope.state_dir(), replies)
        restarted.start()
        assert durable.posts(fake) == []
        restarted.stop()
        attempts = [e for e in journal.read_range() if isinstance(e, OrderSubmissionAttempted)]
        identities = [e for e in journal.read_range() if isinstance(e, OrderVenueIdentityRecorded)]
        assert [e.payload.intent_id for e in attempts] == [order.intent_id]
        assert [e.payload.intent_id for e in identities] == [order.intent_id]
        # Venue order events alone are not a native report: strict replay refuses them.
        with pytest.raises(PaperStateError):
            report_from_journal(
                journal.read_range(), session_id=order.correlation_id, mode="paper",
                symbol=order.symbol, strategy_id=order.strategy_id, strict_native_provenance=True,
            )
        _native_session(journal)
    with SQLiteEventJournal(path, venue_writes=True) as journal:
        events = tuple(journal.read_range())
        expected = _durable_intent(events)
        report = _strict(events)
        assert report.orders and report.fills
        assert all(item.intent_id == expected for item in (*report.orders, *report.fills))
        legacy = _strict(events, strict=False)
        assert all(item.intent_id is None for item in (*legacy.orders, *legacy.fills))
        assert len([e for e in events if isinstance(e, OrderSubmissionAttempted)]) == 1


def _reclaim_worker(root, queue):
    """A separate Windows process restarts on the journal and reclaims the writer."""
    ports._host_authority_root = lambda: Path(root)
    canonical = ports.CanonicalWriterScope(VENUE, VENUE, ACCOUNT)
    with SQLiteEventJournal(canonical.journal_path(), venue_writes=True) as journal:
        runtime, fake = _direct(canonical, journal, [fx.rejected(-2013, "Order does not exist.")])
        lease = _hold_lease(runtime, canonical, journal, owner="restarted-normal")
        try:
            runtime.submit(durable.execution_request())
        except AmbiguousOrderError:
            status = "reservation-held"
        else:
            status = "unexpected-success"
        queue.put({"pid": os.getpid(), "status": status, "posts": len(durable.posts(fake)),
                   "term": lease.guard.fence.term})


def test_i02_refusal_retirement_survives_a_restart_and_reclaimed_writer(scope):
    ctx = get_context("spawn")
    with SQLiteEventJournal(scope.journal_path(), venue_writes=True) as journal:
        runtime, fake = _direct(scope, journal, [fx.order_response(durable.CLIENT_ORDER_ID)])
        first_lease = _hold_lease(runtime, scope, journal)
        def invalidate():
            runtime.journal = None
        captured = _capture_handle(runtime, invalidate)
        with pytest.raises(AttemptJournalRequiredError):
            runtime.submit(durable.execution_request())
        assert len(captured) == 1 and durable.posts(fake) == []

        queue = ctx.Queue()
        worker = ctx.Process(target=_reclaim_worker, args=(str(scope.state_dir().parents[2]), queue))
        worker.start()
        result = queue.get(timeout=60)
        worker.join(30)
        assert worker.exitcode == 0
        assert result["pid"] != os.getpid()
        assert result["status"] == "reservation-held" and result["posts"] == 0
        assert result["term"] > first_lease.guard.fence.term

        # The surviving process keeps the old handle; neither its stale lease nor a freshly
        # reclaimed one can revive what the refusal retired.
        runtime.journal = journal
        _refused_replay(runtime, captured[0], journal, fake)
        _hold_lease(runtime, scope, journal, owner="surviving-normal")
        _refused_replay(runtime, captured[0], journal, fake)
        assert len(durable.attempt_events(journal)) == 1


def test_i03_stale_owner_cannot_send_or_regain_authority_after_restart(scope):
    path = scope.journal_path()
    with SQLiteEventJournal(path, venue_writes=True) as journal:
        runtime, fake = _direct(scope, journal, [fx.order_response(durable.CLIENT_ORDER_ID)])
        lease = _hold_lease(runtime, scope, journal)
        emergency = []
        captured = _capture_handle(runtime, lambda: emergency.append(_emergency(scope, journal)))
        with pytest.raises(AttemptJournalRequiredError):
            runtime.submit(durable.execution_request())
        assert emergency[0].term > lease.guard.fence.term
        assert durable.posts(fake) == []
        with pytest.raises(AttemptJournalRequiredError):
            runtime.submit(replace(durable.execution_request(), intent_id=UUID(int=3)))
        assert durable.posts(fake) == []
        _refused_replay(runtime, captured[0], journal, fake)
        stale_client = runtime.client
    with SQLiteEventJournal(path, venue_writes=True) as journal:
        with pytest.raises(PaperStateError):  # A report is a read; it can issue no authority.
            _strict(journal.read_range())
        with pytest.raises(AttemptJournalRequiredError):
            stale_client.submit_prepared(captured[0], journal=journal)
        _restart_refuses_blind_resubmit(scope, journal)
    assert durable.posts(fake) == []


@pytest.mark.parametrize("writer", ["no_lease", "stale_lease"])
def test_t3_1_writer_without_current_lease_reserves_no_identity(scope, writer):
    """A refused writer reserves nothing; EMERGENCY still requires operator recovery."""
    with SQLiteEventJournal(scope.journal_path(), venue_writes=True) as journal:
        seed_canonical_equity_root(journal)
        runtime, fake = _direct(scope, journal, [fx.order_response(durable.CLIENT_ORDER_ID)])
        if writer == "stale_lease":
            _hold_lease(runtime, scope, journal)
            _emergency(scope, journal)
        with pytest.raises(AttemptJournalRequiredError):
            runtime.submit(durable.execution_request())
        assert durable.posts(fake) == []
        assert durable.attempt_events(journal) == []
        replies = [fx.time_response(), fx.open_orders_response(), *fx.environment_responses()]
        next_owner, _, next_fake = _rewire(journal, scope.state_dir(), replies)
        if writer == "stale_lease":
            with pytest.raises(ProductionStateError, match="normal writer lease unavailable"):
                next_owner.start()
            assert durable.posts(next_fake) == []
            return
        next_owner.start()
        assert durable.posts(next_fake) == []
        next_owner.stop()


def test_t3_3_real_https_writer_without_rollback_checkpoint_is_refused(scope):
    with SQLiteEventJournal(scope.journal_path(), venue_writes=True) as journal:
        seed_canonical_equity_root(journal)
        with pytest.raises(ProductionStateError, match="rollback checkpoint"):
            _rewire(journal, scope.state_dir(), [], https="at_construction")


@pytest.mark.parametrize("pin", ["current", "stale"])
def test_t3_3_real_https_writer_starts_only_on_the_current_rollback_pin(scope, pin):
    proofs = scope.state_dir() / "proofs"
    proofs.mkdir()
    with SQLiteEventJournal(scope.journal_path(), venue_writes=True) as journal:
        seed_canonical_equity_root(journal)
        witness = {}
        audit = CrewOriginAudit(proofs, key=KEY, journal_id=journal.journal_id(),
                                checkpoint_reader=lambda: dict(witness))
        witness.update(audit.export_checkpoint(journal))
        if pin == "stale":
            seed_canonical_equity_root(journal, occurred_at=NOW)
        runtime, _, fake = _keyed(
            journal, scope.state_dir(), CrewOriginAuthority(audit=audit), https="at_construction",
            require_authenticated_origin_audit=True, require_rollback_checkpoint=True,
        )
        if pin == "stale":
            with pytest.raises(ProductionStateError, match="origin checkpoint"):
                runtime.start()
            assert fake.requests == []
        else:
            runtime.start()
            assert durable.posts(fake) == []
            runtime.stop()


def _high_water(scope):
    return scope.ownership_path().with_name("ownership.json.high_water")


@pytest.mark.parametrize("tamper", ["restored_old_record", "high_water_removed", "record_removed"])
def test_t3_2_old_ownership_evidence_cannot_revive_a_fenced_writer(scope, tamper):
    """An older ownership file must not undo a takeover; emergency control stays available."""
    with SQLiteEventJournal(scope.journal_path(), venue_writes=True) as journal:
        runtime, fake = _direct(scope, journal, [fx.order_response(durable.CLIENT_ORDER_ID)])
        _hold_lease(runtime, scope, journal)
        old = scope.ownership_path().read_bytes()
        minted = _emergency(scope, journal)
        scope.ownership_path().write_bytes(old)
        if tamper == "high_water_removed":
            _high_water(scope).unlink()
        elif tamper == "record_removed":
            scope.ownership_path().unlink()
        with pytest.raises(AttemptJournalRequiredError):
            runtime.submit(durable.execution_request())
        assert durable.posts(fake) == []
        assert durable.attempt_events(journal) == []
        store = OwnershipStore(scope.ownership_path())
        if tamper != "high_water_removed":
            assert store.next_term() == minted.term + 1
        recovered = _emergency(scope, journal)
        assert recovered.term == store.next_term() - 1
        if tamper != "high_water_removed":
            assert recovered.term == minted.term + 1


def test_t3_2_crash_between_high_water_and_record_fails_closed_until_takeover(scope):
    with SQLiteEventJournal(scope.journal_path(), venue_writes=True) as journal:
        runtime, fake = _direct(scope, journal, [fx.order_response(durable.CLIENT_ORDER_ID)])
        lease = _hold_lease(runtime, scope, journal)
        store = OwnershipStore(scope.ownership_path())
        original = store._write

        def crash(_record):
            raise OSError("offline crash before the record was replaced")

        store._write = crash
        with pytest.raises(OSError, match="offline crash"):
            store.takeover(
                owner_id="next", owner_kind=OwnerKind.EMERGENCY,
                acquired_at=datetime.now(timezone.utc), reason_code="offline-test",
                journal_path=str(journal.path), journal_id=journal.journal_id(),
            )
        store._write = original
        with pytest.raises(AttemptJournalRequiredError):
            runtime.submit(durable.execution_request())
        assert durable.posts(fake) == []
        assert _emergency(scope, journal).term == lease.guard.fence.term + 2


def test_t3_2_current_writer_is_not_disturbed(scope):
    """The missing external authority blocks HTTPS, but never revokes a valid lease."""
    with SQLiteEventJournal(scope.journal_path(), venue_writes=True) as journal:
        runtime, fake = _direct(scope, journal, [fx.order_response(durable.CLIENT_ORDER_ID)])
        lease = _hold_lease(runtime, scope, journal)
        lease.guard.require_current()
        with pytest.raises(AttemptJournalRequiredError, match="independent monotonic"):
            runtime.submit(durable.execution_request())
        assert durable.posts(fake) == []
        lease.guard.require_current()
        assert OwnershipStore(scope.ownership_path()).read().owner_kind is OwnerKind.NORMAL


@pytest.mark.parametrize("substitute", ["foreign_identity", "rollback_copy_same_identity"])
def test_i04_journal_substituted_after_the_marker_is_refused_and_not_revived(scope, substitute):
    path = scope.journal_path()
    with SQLiteEventJournal(path, venue_writes=True):
        pass
    rollback = path.with_name("journal.rollback.db")
    shutil.copy2(path, rollback)
    moved = path.with_name("journal.original.db")
    held = {}
    journal = SQLiteEventJournal(path, venue_writes=True)
    held["journal"] = journal
    try:
        runtime, fake = _direct(scope, journal, [fx.order_response(durable.CLIENT_ORDER_ID)])
        _hold_lease(runtime, scope, journal)

        def swap():
            journal.close()
            os.replace(path, moved)
            if substitute == "rollback_copy_same_identity":
                shutil.copy2(rollback, path)
            with SQLiteEventJournal(path, venue_writes=True):
                pass
            replacement = SQLiteEventJournal(path, venue_writes=True)
            held["journal"] = replacement
            runtime.journal = replacement
            runtime.client._canonical_attempt_journal = replacement

        captured = _capture_handle(runtime, swap)
        with pytest.raises(AttemptJournalRequiredError):
            runtime.submit(durable.execution_request())
        assert durable.posts(fake) == []

        held["journal"].close()
        os.replace(moved, path)
        restored = SQLiteEventJournal(path, venue_writes=True)
        held["journal"] = restored
        runtime.journal = restored
        runtime.client._canonical_attempt_journal = restored
        _refused_replay(runtime, captured[0], restored, fake)
        _restart_refuses_blind_resubmit(scope, restored)
    finally:
        held["journal"].close()


@contextmanager
def _sealed_attempt(scope):
    """A real authenticated crew origin whose attempt is sealed, then a crash before the POST."""
    root = scope.state_dir()
    proofs = root / "proofs"
    proofs.mkdir()
    with SQLiteEventJournal(scope.journal_path(), venue_writes=True) as journal:
        seed_canonical_equity_root(journal)
        audit = CrewOriginAudit(proofs, key=KEY, journal_id=journal.journal_id())
        authority = CrewOriginAuthority(audit=audit)
        runtime, port, fake = _keyed(journal, root, authority)
        runtime.start()
        gateway = CrewDecisionGateway(
            journal=journal, runtime=runtime, authority=authority,
            producer="gateway.crew_strategist", policy=lambda parsed: fresh("i05-origin"),
        )
        candidate, source = trusted_issue(gateway, plan(), root)
        request = ExecutionRequest(
            intent_id=candidate.intent_id, correlation_id=candidate.correlation_id,
            symbol=candidate.symbol, side=candidate.side, order_type=candidate.order_type,
            quantity=candidate.quantity, limit_price=candidate.limit_price,
            time_in_force=candidate.time_in_force, strategy_id=candidate.strategy_id,
        )
        def crash(*_args):
            raise RuntimeError("offline interruption after the sealed attempt")
        port._bind_durable_authorization = crash
        with pytest.raises(RuntimeError, match="sealed attempt"):
            port.submit_durably(request, origin_audit=audit)
        assert durable.posts(fake) == []
        runtime.stop()
        journal_id = journal.journal_id()
    yield SimpleNamespace(journal_id=journal_id, proofs=proofs, request=request, root=root)


def _keyed(journal, root, authority, *, https=False, **kwargs):
    # The crash here precedes any lease, so the sealed marker needs the scripted offline sender.
    runtime, port, fake = _rewire(
        journal, root, startup_responses(), https=https, when=NOW, require_origin_binding=True,
        trusted_origin_producer="gateway.crew_strategist", origin_authority=authority, **kwargs,
    )
    return runtime, port, fake


@pytest.mark.parametrize("missing", ["wrong_key", "no_key_on_restart", "proof_removed", "no_checkpoint"])
def test_i05_restart_without_authenticated_source_fails_closed(scope, missing):
    with _sealed_attempt(scope) as sealed:
        if missing == "proof_removed":
            for proof in sealed.proofs.iterdir():
                proof.unlink()
        with SQLiteEventJournal(scope.journal_path(), venue_writes=True) as journal:
            before = tuple(journal.read_range())
            key = b"another-host-secret-offline-only" * 2 if missing == "wrong_key" else KEY
            audit = CrewOriginAudit(
                sealed.proofs, key=key, journal_id=sealed.journal_id,
                **({"checkpoint_reader": lambda: None} if missing == "no_checkpoint" else {}),
            )
            kwargs = {"require_rollback_checkpoint": True} if missing == "no_checkpoint" else {}
            with pytest.raises((ProductionStateError, ProductionRecoveryRequiredError)):
                if missing == "no_key_on_restart":
                    _keyed(journal, sealed.root, CrewOriginAuthority(),
                           require_authenticated_origin_audit=True)
                else:
                    runtime, _, fake = _keyed(
                        journal, sealed.root, CrewOriginAuthority(audit=audit),
                        require_authenticated_origin_audit=True, **kwargs,
                    )
                    try:
                        runtime.start()
                    finally:
                        assert durable.posts(fake) == []
            assert tuple(journal.read_range()) == before
            assert not [e for e in before if isinstance(e, OrderVenueIdentityRecorded)]


def _lost_ack(scope):
    order = intent("i06", quantity=Decimal("0.00100"))
    with SQLiteEventJournal(scope.journal_path(), venue_writes=True) as journal:
        runtime, _, fake = _admit(scope, journal, order, TransportTimeout("response lost"), simulated=True)
        with pytest.raises(AmbiguousOrderError):
            admitted_execute(runtime, order)
        assert len(durable.posts(fake)) == 1
        assert runtime.status()["last_outcome"] == "unknown_pending_reconciliation"
        runtime.stop()
    return order, production._reference_for(order)


def _request_for(order):
    return ExecutionRequest(
        intent_id=order.intent_id, correlation_id=order.correlation_id, symbol=order.symbol,
        side=order.side, order_type=order.order_type, quantity=order.quantity,
        limit_price=order.limit_price, time_in_force=order.time_in_force,
        strategy_id=order.strategy_id,
    )


def test_i06_lost_ack_reconciles_by_reads_without_a_second_post(scope):
    order, reference = _lost_ack(scope)
    answer = fx.order_response(reference, order_id=4242)
    with SQLiteEventJournal(scope.journal_path(), venue_writes=True) as journal:
        replies = [fx.time_response(), fx.open_orders_response(reference),
                   *fx.environment_responses(), answer]
        restarted, port, fake = _rewire(journal, scope.state_dir(), replies)
        restarted.start()
        assert port.submit(_request_for(order)) == reference
        assert durable.posts(fake) == []
        restarted.stop()
        events = tuple(journal.read_range())
        attempts = [e for e in events if isinstance(e, OrderSubmissionAttempted)]
        assert [e.payload.intent_id for e in attempts] == [order.intent_id]
        # A later read cannot name our venue order: only our own write answer does.
        assert not [e for e in events if isinstance(e, OrderVenueIdentityRecorded)]
        with pytest.raises(PaperStateError):
            report_from_journal(
                events, session_id=order.correlation_id, mode="paper", symbol=order.symbol,
                strategy_id=order.strategy_id, strict_native_provenance=True,
            )


def test_i06_lost_ack_with_a_different_venue_order_never_adopts_or_posts(scope):
    order, reference = _lost_ack(scope)
    answer = fx.order_response(reference, orig_qty="0.00200000", order_id=4242)
    with SQLiteEventJournal(scope.journal_path(), venue_writes=True) as journal:
        replies = [fx.time_response(), fx.open_orders_response(reference),
                   *fx.environment_responses(), answer]
        restarted, port, fake = _rewire(journal, scope.state_dir(), replies)
        restarted.start()
        with pytest.raises(AmbiguousOrderError):
            port.submit(_request_for(order))
        assert durable.posts(fake) == []
        restarted.stop()


@pytest.mark.parametrize("corruption", ["missing", "foreign", "conflicting"])
def test_i07_corrupt_strict_causation_is_refused_and_grants_no_admission(scope, corruption):
    with SQLiteEventJournal(scope.journal_path(), venue_writes=True) as journal:
        runtime, fake = _direct(scope, journal, [fx.order_response(durable.CLIENT_ORDER_ID)])
        _hold_lease(runtime, scope, journal)
        captured = _capture_handle(runtime, lambda: _emergency(scope, journal))
        with pytest.raises(AttemptJournalRequiredError):
            runtime.submit(durable.execution_request())
        _native_session(journal)
        events = tuple(journal.read_range())
        foreign = UUID("eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee")
        seen = []

        def corrupt(event):
            if event.event_type != ORDER_STATE_EVENT_TYPE or event.correlation_id != paper_fx.SESSION_ID:
                return event
            seen.append(event)
            if corruption == "missing":
                return event.model_copy(update={"causation_id": None})
            if corruption == "conflicting" and len(seen) > 1:
                return event
            return event.model_copy(update={"causation_id": foreign})

        corrupted = tuple(corrupt(event) for event in events)
        assert seen
        with pytest.raises(PaperStateError, match="provenance|unknown durable intent"):
            _strict(corrupted)
        if corruption == "conflicting":
            with pytest.raises(PaperStateError, match="conflicting"):  # Legacy refuses this too.
                _strict(corrupted, strict=False)
        else:
            legacy = _strict(corrupted, strict=False)
            assert all(item.intent_id is None for item in (*legacy.orders, *legacy.fills))
        _refused_replay(runtime, captured[0], journal, fake)
        _hold_lease(runtime, scope, journal, owner="after-report")
        _refused_replay(runtime, captured[0], journal, fake)
        assert len(durable.attempt_events(journal)) == 1


def _second_request():
    return replace(durable.execution_request(), intent_id=UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"))


def _admitted_sender(root, in_send, release, takeover_done, queue):
    """Holds term 1, sends once, then tries again with a lease proved before the takeover."""
    ports._host_authority_root = lambda: Path(root)
    canonical = ports.CanonicalWriterScope(VENUE, VENUE, ACCOUNT)
    with SQLiteEventJournal(canonical.journal_path(), venue_writes=True) as journal:
        fake = ScriptedTransport([
            *fx.environment_responses(), fx.account_response(),
            fx.order_response(durable.CLIENT_ORDER_ID),
            fx.order_response(f"ct2-{_second_request().intent_id.hex}"),
        ])
        # Offline concurrency probe: exact scripted transport, never a real HTTPS writer.
        runtime = durable.started(fake, journal)
        assert runtime.client.startup_account_snapshot("BTC", "USDT").venue_account_id == ACCOUNT
        runtime.client.canonical_scope = canonical
        lease = _hold_lease(runtime, canonical, journal, owner="process-a")
        stale_ready = ports.CanonicalWriterLease(
            scope=canonical, journal=journal, store=lease.store, guard=lease.guard,
            request=_second_request(),
        )

        scripted_send = fake.send

        def gated(request):
            if request.method == "POST":
                in_send.set()
                assert release.wait(30)
            return scripted_send(request)

        fake.send = gated
        # ScriptedTransport intentionally has no automatic real-writer lease.
        # Explicitly exercise the SAME production send_guard against takeover.
        with lease.send_guard(journal):
            first = runtime.submit(durable.execution_request())
        assert takeover_done.wait(30)
        runtime.client._canonical_writer_lease = stale_ready
        try:
            # Exact scripted transports have no implicit lease binding; model
            # production's final guard explicitly without weakening HTTPS safety.
            with stale_ready.send_guard(journal):
                runtime.submit(_second_request())
        except (AttemptJournalRequiredError, ports.CanonicalWriterScopeError, StaleWriterFenceError):
            status = "stale-refused"
        else:
            status = "stale-sent"
        queue.put({"role": "sender", "pid": os.getpid(), "first": first, "status": status,
                   "posts": len(durable.posts(fake))})


def _emergency_then_restart(root, journal_id, in_send, trying, takeover_done, queue):
    ports._host_authority_root = lambda: Path(root)
    canonical = ports.CanonicalWriterScope(VENUE, VENUE, ACCOUNT)
    assert in_send.wait(60)
    trying.set()
    started = time.monotonic()
    record = OwnershipStore(canonical.ownership_path()).takeover(
        owner_id="process-b", owner_kind=OwnerKind.EMERGENCY,
        acquired_at=datetime.now(timezone.utc), reason_code="offline-test",
        journal_path=str(canonical.journal_path()), journal_id=UUID(journal_id),
    )
    waited = time.monotonic() - started
    takeover_done.set()
    with SQLiteEventJournal(canonical.journal_path(), venue_writes=True) as journal:
        runtime, fake = _direct(canonical, journal, [fx.order_response(durable.CLIENT_ORDER_ID)])
        third = replace(durable.execution_request(), intent_id=UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc"))
        try:
            runtime.submit(third)
        except AttemptJournalRequiredError:
            status = "no-normal-lease"
        else:
            status = "unexpected-success"
        queue.put({"role": "emergency", "pid": os.getpid(), "term": record.term, "waited": waited,
                   "status": status, "posts": len(durable.posts(fake))})


def test_i08_takeover_waits_for_the_admitted_send_then_fences_the_stale_process(tmp_path):
    root = tmp_path / "host"
    ports_root, ports._host_authority_root = ports._host_authority_root, lambda: root
    try:
        canonical = ports.CanonicalWriterScope(VENUE, VENUE, ACCOUNT)
        canonical.state_dir().mkdir(parents=True)
        with SQLiteEventJournal(canonical.journal_path(), venue_writes=True) as journal:
            journal_id = journal.journal_id().hex
    finally:
        ports._host_authority_root = ports_root
    ctx = get_context("spawn")
    in_send, release, trying, takeover_done = ctx.Event(), ctx.Event(), ctx.Event(), ctx.Event()
    queue = ctx.Queue()
    sender = ctx.Process(target=_admitted_sender, args=(str(root), in_send, release, takeover_done, queue))
    owner = ctx.Process(target=_emergency_then_restart,
                        args=(str(root), journal_id, in_send, trying, takeover_done, queue))
    try:
        sender.start()
        owner.start()
        assert in_send.wait(60) and trying.wait(30)
        assert not takeover_done.wait(0.5), "takeover finished inside an admitted send"
        release.set()
        results = {item["role"]: item for item in (queue.get(timeout=60), queue.get(timeout=60))}
        for process in (sender, owner):
            process.join(30)
            assert process.exitcode == 0
        assert results["sender"]["pid"] != results["emergency"]["pid"]
        assert results["sender"]["first"] == durable.CLIENT_ORDER_ID
        assert results["sender"]["status"] == "stale-refused"
        assert results["sender"]["posts"] == 1
        assert results["emergency"]["term"] == 2 and results["emergency"]["waited"] >= 0.4
        assert results["emergency"]["status"] == "no-normal-lease" and results["emergency"]["posts"] == 0
    finally:
        for process in (sender, owner):
            if process.is_alive():
                process.terminate()
                process.join(5)
        queue.close()
