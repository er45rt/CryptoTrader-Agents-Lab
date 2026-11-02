"""P0-2 final-send regressions. All HTTP is replaced by scripted responses."""
import time
from dataclasses import replace
from datetime import datetime, timezone
from multiprocessing import get_context
from pathlib import Path
from threading import Event, Thread
from uuid import UUID

import pytest

from crypto_trader.breakglass.ownership import OwnerKind, OwnershipStore, WriterGuard
from crypto_trader.execution import ports
from crypto_trader.execution import NonDurableExecutionError
from crypto_trader.execution.binance_testnet import AttemptJournalRequiredError
from crypto_trader.execution.binance_testnet.transport import RequestsTransport, ScriptedTransport
from crypto_trader.persistence import SQLiteEventJournal
from crypto_trader.tests import test_testnet_durable_submission as durable
from crypto_trader.tests import testnet_fixtures as fx
from crypto_trader.tests import test_production_testnet_integration as production
from crypto_trader.tests.production_fixtures import enable_https_behind_checkpoint_gate

VENUE = "binance-spot-testnet"
ACCOUNT = "123456789"


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refused(*args, **kwargs):
        pytest.fail("an unmocked HTTPS sender was reached")
    monkeypatch.setattr(RequestsTransport, "send", refused)


@pytest.fixture
def boundary(tmp_path, monkeypatch):
    monkeypatch.setattr(ports, "_host_authority_root", lambda: tmp_path / "host")
    scope = ports.CanonicalWriterScope(VENUE, VENUE, ACCOUNT)
    scope.state_dir().mkdir(parents=True)
    with SQLiteEventJournal(scope.journal_path(), venue_writes=True) as journal:
        fake = ScriptedTransport([
            *fx.environment_responses(), fx.account_response(),
            fx.order_response(durable.CLIENT_ORDER_ID),
        ])
        https = RequestsTransport()
        https.send = fake.send  # The external operation alone is replaced.
        runtime = durable.started(https, journal)
        assert runtime.client.startup_account_snapshot("BTC", "USDT").venue_account_id == ACCOUNT
        runtime.client.canonical_scope = scope
        store = OwnershipStore(scope.ownership_path())
        yield runtime, fake, scope, journal, store


def bind_held_lease(boundary):
    """Boundary-level fixture; the separate production test verifies actual issuance."""
    runtime, _, scope, journal, store = boundary
    record = store.takeover(
        owner_id="offline-normal-writer", owner_kind=OwnerKind.NORMAL,
        acquired_at=datetime.now(timezone.utc), reason_code="offline-test",
        journal_path=str(journal.path), journal_id=journal.journal_id(),
    )
    lease = ports.CanonicalWriterLease(
        scope=scope, journal=journal, store=store, guard=WriterGuard(store, record.fence),
        request=durable.execution_request(),
    )
    runtime.client._canonical_writer_lease = lease
    return lease


@pytest.mark.parametrize("kind", ["custom", "scripted_subclass", "https_subclass"])
def test_unapproved_write_sender_is_refused_before_post(tmp_path, kind):
    fake = ScriptedTransport([
        *fx.environment_responses(), fx.order_response(durable.CLIENT_ORDER_ID),
    ])
    class CustomTransport:
        def send(self, request):
            return fake.send(request)
    class ScriptedSubclass(ScriptedTransport):
        def send(self, request):
            return fake.send(request)
    class HttpsSubclass(RequestsTransport):
        def send(self, request):
            return fake.send(request)
    with SQLiteEventJournal(tmp_path / "journal.db", venue_writes=True) as journal:
        runtime = durable.started(fake, journal)
        runtime.client.transport = {
            "custom": CustomTransport, "scripted_subclass": ScriptedSubclass,
            "https_subclass": HttpsSubclass,
        }[kind]()
        with pytest.raises(AttemptJournalRequiredError):
            runtime.submit(durable.execution_request())
        assert durable.posts(fake) == []


@pytest.mark.parametrize("owner", ["absent", "emergency_owner"])
def test_https_refuses_without_a_held_normal_lease(boundary, owner):
    runtime, fake, _, journal, store = boundary
    if owner == "emergency_owner":
        store.takeover(
            owner_id="offline-emergency", owner_kind=OwnerKind.EMERGENCY,
            acquired_at=datetime.now(timezone.utc), reason_code="offline-test",
            journal_path=str(journal.path), journal_id=journal.journal_id(),
        )
    with pytest.raises(AttemptJournalRequiredError):
        runtime.submit(durable.execution_request())
    assert durable.posts(fake) == []


def test_current_normal_record_without_held_proof_is_insufficient(boundary):
    bind_held_lease(boundary)
    runtime, fake, _, _, _ = boundary
    runtime.client._canonical_writer_lease = None
    with pytest.raises(AttemptJournalRequiredError):
        runtime.submit(durable.execution_request())
    assert durable.posts(fake) == []


def test_held_proof_for_one_admitted_intent_cannot_authorize_another(boundary):
    bind_held_lease(boundary)
    runtime, fake, _, _, _ = boundary
    foreign = replace(durable.execution_request(), intent_id=UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"))
    with pytest.raises(AttemptJournalRequiredError):
        runtime.submit(foreign)
    assert durable.posts(fake) == []


def test_closed_authorized_journal_prevents_last_send(boundary):
    bind_held_lease(boundary)
    runtime, fake, _, journal, _ = boundary
    original = runtime._send_authorized_write
    def close_before_send(prepared, request, **kwargs):
        journal.close()
        return original(prepared, request, **kwargs)
    runtime._send_authorized_write = close_before_send
    with pytest.raises(AttemptJournalRequiredError):
        runtime.submit(durable.execution_request())
    assert durable.posts(fake) == []


@pytest.mark.parametrize("changed", ["runtime_journal", "client_journal", "transport", "credentials"])
def test_post_marker_binding_changes_refuse_and_consume_authorization(boundary, tmp_path, changed):
    bind_held_lease(boundary)
    runtime, fake, _, journal, _ = boundary
    original = runtime._send_authorized_write
    captured = []
    with SQLiteEventJournal(tmp_path / "foreign.db", venue_writes=True) as foreign:
        original_transport = runtime.client.transport
        original_credentials = runtime.client.credentials
        def change_before_send(prepared, request, **kwargs):
            captured.append(prepared.handle)
            if changed == "runtime_journal":
                runtime.journal = foreign
            elif changed == "client_journal":
                runtime.client._canonical_attempt_journal = foreign
            elif changed == "transport":
                replacement = RequestsTransport()
                replacement.send = fake.send
                runtime.client.transport = replacement
            else:
                runtime.client.credentials = replace(original_credentials)
            return original(prepared, request, **kwargs)
        runtime._send_authorized_write = change_before_send
        with pytest.raises(AttemptJournalRequiredError):
            runtime.submit(durable.execution_request())
        assert len(captured) == 1
        assert len(durable.attempt_events(journal)) == 1
        runtime.journal = journal
        runtime.client._canonical_attempt_journal = journal
        runtime.client.transport = original_transport
        runtime.client.credentials = original_credentials
        with pytest.raises(AttemptJournalRequiredError):
            runtime.client.submit_prepared(captured[0], journal=journal)
        assert durable.posts(fake) == []


def test_writer_takeover_after_marker_prevents_final_send(boundary):
    lease = bind_held_lease(boundary)
    runtime, fake, _, journal, store = boundary
    original = runtime._send_authorized_write
    def takeover_before_send(prepared, request, **kwargs):
        emergency = store.takeover(
            owner_id="offline-emergency", owner_kind=OwnerKind.EMERGENCY,
            acquired_at=datetime.now(timezone.utc), reason_code="offline-test",
            journal_path=str(journal.path), journal_id=journal.journal_id(),
        )
        assert emergency.term > lease.guard.fence.term
        return original(prepared, request, **kwargs)
    runtime._send_authorized_write = takeover_before_send
    with pytest.raises(AttemptJournalRequiredError):
        runtime.submit(durable.execution_request())
    assert len(durable.attempt_events(journal)) == 1
    assert durable.posts(fake) == []


def test_held_current_lease_allows_one_send_and_no_capability_replay(boundary):
    lease = bind_held_lease(boundary)
    runtime, fake, _, journal, _ = boundary
    # Test local durable-capability semantics with the exact offline transport.
    runtime.client.transport = fake
    original = runtime._send_authorized_write
    captured = []
    def capture(prepared, request, **kwargs):
        captured.append(prepared.handle)
        return original(prepared, request, **kwargs)
    runtime._send_authorized_write = capture
    with lease.send_guard(journal):
        assert runtime.submit(durable.execution_request()) == durable.CLIENT_ORDER_ID
    with pytest.raises(AttemptJournalRequiredError):
        runtime.client.submit_prepared(captured[0], journal=journal)
    assert len(durable.posts(fake)) == 1
    assert len(durable.attempt_events(journal)) == 1


def test_takeover_waits_for_an_already_admitted_send(boundary):
    lease = bind_held_lease(boundary)
    runtime, fake, _, journal, store = boundary
    # Offline sender plus explicit production lock: no simulated HTTPS exemption.
    runtime.client.transport = fake
    scripted_send = fake.send
    entered, trying, finished = Event(), Event(), Event()
    errors = []
    path, identity = str(journal.path), journal.journal_id()
    def takeover():
        try:
            assert entered.wait(5)
            trying.set()
            OwnershipStore(store.path).takeover(
                owner_id="offline-emergency", owner_kind=OwnerKind.EMERGENCY,
                acquired_at=datetime.now(timezone.utc), reason_code="offline-test",
                journal_path=path, journal_id=identity,
            )
        except BaseException as exc:
            errors.append(exc)
        finally:
            finished.set()
    def send(request):
        entered.set()
        assert trying.wait(5)
        assert not finished.wait(0.1), "takeover completed inside an admitted send"
        return scripted_send(request)
    fake.send = send
    worker = Thread(target=takeover)
    worker.start()
    try:
        with lease.send_guard(journal):
            assert runtime.submit(durable.execution_request()) == durable.CLIENT_ORDER_ID
    finally:
        entered.set()
        worker.join(15)
    assert not worker.is_alive()
    assert errors == []
    assert finished.is_set()
    assert store.read().owner_kind is OwnerKind.EMERGENCY
    assert len(durable.posts(fake)) == 1


def test_actual_production_issuance_survives_existing_submission_lock(tmp_path, monkeypatch):
    monkeypatch.setattr(ports, "_host_authority_root", lambda: tmp_path / "host")
    scope = ports.CanonicalWriterScope(VENUE, VENUE, ACCOUNT)
    scope.state_dir().mkdir(parents=True)
    intent = production.intent("p0-2-production-positive")
    fake = ScriptedTransport([
        *production.startup_responses(),
        fx.order_response(production._reference_for(intent)),
    ])
    client = fx.client(fake)
    with SQLiteEventJournal(scope.journal_path(), venue_writes=True) as journal:
        runtime, port, _ = production._wired(journal, scope.state_dir(), fake, client=client)
        # Exact offline ScriptedTransport retains admission + canonical send-lock tests.
        runtime.start()
        assert client._canonical_writer_lease is None
        outcome = production.admitted_execute(runtime, intent)
        assert outcome.submitted is True
        assert len(durable.posts(fake)) == 1
        assert client._canonical_writer_lease is None
        # The offline adapter has no implicit lease. The actual HTTPS boundary
        # must still refuse any direct post-admission submit without held proof.
        https = RequestsTransport()
        https.send = fake.send
        client.transport = https
        with pytest.raises(AttemptJournalRequiredError):
            port.submit(durable.execution_request(quantity="0.001"))
        assert len(durable.posts(fake)) == 1


def _unowned_worker(root, journal_path, intent, ready, submit_lock, queue):
    ports._host_authority_root = lambda: Path(root)
    fake = ScriptedTransport([
        *fx.environment_responses(), fx.account_response(),
        fx.order_response(f"ct2-{intent.hex}"),
    ])
    https = RequestsTransport()
    https.send = fake.send  # Spawned Windows workers also replace all HTTP.
    status = "setup-not-completed"
    try:
        with SQLiteEventJournal(Path(journal_path), venue_writes=True) as journal:
            runtime = durable.started(https, journal)
            assert runtime.client.startup_account_snapshot("BTC", "USDT").venue_account_id == ACCOUNT
            runtime.client.canonical_scope = ports.CanonicalWriterScope(VENUE, VENUE, ACCOUNT)
            assert ready.wait(15)
            try:
                # Serialise only this offline harness's journal-append turns so the
                # conditional attempt-marker commit cannot raise a revision race inside
                # the test. Each independent process must still be refused by the
                # production writer guard; the security property is zero POSTs.
                with submit_lock:
                    runtime.submit(replace(durable.execution_request(), intent_id=intent))
            except (AttemptJournalRequiredError, NonDurableExecutionError):
                status = "refused"
            else:
                status = "unexpected-success"
    except BaseException as exc:
        status = "setup:" + type(exc).__name__
    queue.put((len(durable.posts(fake)), status))


def test_two_unowned_processes_with_distinct_intents_cannot_send(tmp_path):
    root = tmp_path / "host"
    path = root / VENUE / VENUE / ACCOUNT / "journal.db"
    path.parent.mkdir(parents=True)
    with SQLiteEventJournal(path, venue_writes=True):
        pass
    ctx = get_context("spawn")
    ready, submit_lock, queue = ctx.Event(), ctx.Lock(), ctx.Queue()
    processes = [ctx.Process(target=_unowned_worker, args=(str(root), str(path), UUID(value), ready, submit_lock, queue))
                 for value in ("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")]
    try:
        for process in processes:
            process.start()
        ready.set()
        results = [queue.get(timeout=50) for _ in processes]
        for process in processes:
            process.join(30)
            assert process.exitcode == 0
        assert results == [(0, "refused"), (0, "refused")]
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(5)
        queue.close()


def test_invalid_runtime_journal_refusal_retires_authorization(boundary):
    """A pre-send refusal must retire the one-shot capability, not park it for replay."""
    bind_held_lease(boundary)
    runtime, fake, _, journal, _ = boundary
    original = runtime._send_authorized_write
    captured = []
    def invalidate(prepared, request, **kwargs):
        captured.append(prepared.handle)
        runtime.journal = None
        return original(prepared, request, **kwargs)
    runtime._send_authorized_write = invalidate
    with pytest.raises(AttemptJournalRequiredError):
        runtime.submit(durable.execution_request())
    assert len(captured) == 1
    assert len(durable.attempt_events(journal)) == 1
    assert durable.posts(fake) == []
    runtime.journal = journal
    # Restoring the journal must not resurrect the capability that the refusal retired.
    with pytest.raises(AttemptJournalRequiredError):
        runtime.client.submit_prepared(captured[0], journal=journal)
    assert durable.posts(fake) == []


def _cross_process_sender(root, journal_id, in_send, allow_finish, sender_done, queue):
    """Independent Windows process: hold the NORMAL lease and perform one admitted send."""
    ports._host_authority_root = lambda: Path(root)
    scope = ports.CanonicalWriterScope(VENUE, VENUE, ACCOUNT)
    fake = ScriptedTransport([
        *fx.environment_responses(), fx.account_response(),
        fx.order_response(durable.CLIENT_ORDER_ID),
    ])
    result = {"role": "sender", "status": "not-run"}
    try:
        with SQLiteEventJournal(scope.journal_path(), venue_writes=True) as journal:
            runtime = durable.started(fake, journal)
            assert runtime.client.startup_account_snapshot("BTC", "USDT").venue_account_id == ACCOUNT
            runtime.client.canonical_scope = scope
            store = OwnershipStore(scope.ownership_path())
            record = store.takeover(
                owner_id="cross-process-normal", owner_kind=OwnerKind.NORMAL,
                acquired_at=datetime.now(timezone.utc), reason_code="cross-process-normal",
                journal_path=str(journal.path), journal_id=journal.journal_id(),
            )
            lease = ports.CanonicalWriterLease(
                scope=scope, journal=journal, store=store,
                guard=WriterGuard(store, record.fence),
                request=durable.execution_request(),
            )
            runtime.client._canonical_writer_lease = lease

            scripted_send = fake.send

            def gated_send(request):
                if request.method != "POST":
                    return scripted_send(request)
                in_send.set()
                assert allow_finish.wait(30), "test never released the admitted send"
                response = scripted_send(request)  # synthetic response; never the network
                return response

            fake.send = gated_send
            try:
                with lease.send_guard(journal):
                    reference = runtime.submit(durable.execution_request())
            except BaseException as exc:  # pragma: no cover - reported to the parent
                result = {"role": "sender", "status": "error", "detail": type(exc).__name__}
            else:
                result = {
                    "role": "sender",
                    "status": "sent",
                    "reference": reference,
                    "posts": len(durable.posts(fake)),
                    "attempts": len(durable.attempt_events(journal)),
                }
    except BaseException as exc:  # pragma: no cover - reported to the parent
        result = {"role": "sender", "status": "setup-error", "detail": type(exc).__name__}
    queue.put(result)
    sender_done.set()


def _cross_process_emergency_takeover(root, journal_id, in_send, takeover_attempted, takeover_done, queue):
    """Independent Windows process: attempt an emergency takeover during the admitted send."""
    ports._host_authority_root = lambda: Path(root)
    scope = ports.CanonicalWriterScope(VENUE, VENUE, ACCOUNT)
    store = OwnershipStore(scope.ownership_path())
    assert in_send.wait(30), "the sender never reached its admitted send"
    takeover_attempted.set()
    started = time.monotonic()
    try:
        record = store.takeover(
            owner_id="cross-process-emergency", owner_kind=OwnerKind.EMERGENCY,
            acquired_at=datetime.now(timezone.utc), reason_code="cross-process-takeover",
            journal_path=str(scope.journal_path()), journal_id=UUID(journal_id),
        )
        result = {
            "role": "takeover", "status": "takeover", "term": record.term,
            "elapsed_seconds": time.monotonic() - started,
        }
    except BaseException as exc:  # pragma: no cover - reported to the parent
        result = {
            "role": "takeover", "status": "error", "detail": type(exc).__name__,
            "elapsed_seconds": time.monotonic() - started,
        }
    queue.put(result)
    takeover_done.set()


def test_independent_windows_process_takeover_waits_for_an_admitted_send(tmp_path):
    """Two independent Windows processes on one journal: takeover cannot cut an admitted send.

    Process A holds the canonical NORMAL lease and is inside an admitted POST. Independent
    process B attempts an EMERGENCY takeover. The takeover must block on the same canonical
    ownership lock until the send finishes, then succeed with a newer fence term. Exactly one
    synthetic POST is allowed and B never sends anything.
    """
    root = tmp_path / "cross_process_host"
    ports._host_authority_root = lambda: root
    scope = ports.CanonicalWriterScope(VENUE, VENUE, ACCOUNT)
    scope.state_dir().mkdir(parents=True)
    with SQLiteEventJournal(scope.journal_path(), venue_writes=True) as journal:
        journal_id = journal.journal_id().hex
    ctx = get_context("spawn")
    in_send, allow_finish, sender_done = ctx.Event(), ctx.Event(), ctx.Event()
    takeover_attempted, takeover_done = ctx.Event(), ctx.Event()
    queue = ctx.Queue()
    sender = ctx.Process(
        target=_cross_process_sender,
        args=(str(root), journal_id, in_send, allow_finish, sender_done, queue),
    )
    owner = ctx.Process(
        target=_cross_process_emergency_takeover,
        args=(str(root), journal_id, in_send, takeover_attempted, takeover_done, queue),
    )
    try:
        sender.start()
        owner.start()
        assert in_send.wait(60), "sender process never reached its admitted POST"
        assert takeover_attempted.wait(15), "takeover process never attempted its takeover"
        assert not takeover_done.wait(0.5), (
            "emergency takeover completed while an admitted send was still in progress"
        )
        allow_finish.set()
        assert sender_done.wait(30), "sender process did not finish"
        assert takeover_done.wait(30), "takeover process did not finish"
        results = [queue.get(timeout=30), queue.get(timeout=30)]
        for process in (sender, owner):
            process.join(30)
            assert process.exitcode == 0
        by_role = {item["role"]: item for item in results}
        assert by_role["sender"]["status"] == "sent", by_role
        assert by_role["sender"]["reference"] == durable.CLIENT_ORDER_ID
        assert by_role["sender"]["posts"] == 1
        assert by_role["sender"]["attempts"] == 1
        assert by_role["takeover"]["status"] == "takeover", by_role
        assert by_role["takeover"]["term"] == 2
        assert by_role["takeover"]["elapsed_seconds"] >= 0.4, (
            "the independent takeover did not wait for the in-progress admitted send"
        )
        record = OwnershipStore(scope.ownership_path()).read()
        assert record is not None and record.owner_kind is OwnerKind.EMERGENCY
        assert record.term == 2
        with SQLiteEventJournal(scope.journal_path(), venue_writes=True) as journal:
            assert len(durable.attempt_events(journal)) == 1
    finally:
        for process in (sender, owner):
            if process.is_alive():
                process.terminate()
                process.join(5)
        queue.close()
