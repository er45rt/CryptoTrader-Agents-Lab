"""Offline authenticated Crew restart audit; no external transport or trading."""
from types import SimpleNamespace
from contextlib import contextmanager
from dataclasses import replace
import hashlib
import json
import socket
from uuid import uuid4

import pytest

from crypto_trader.crew_decision_gateway import CrewDecisionGateway
from crypto_trader.persistence import SQLiteEventJournal
from crypto_trader.production import ProductionStateError
from crypto_trader.production.runtime import CrewOriginAuthority
from crypto_trader.production.origin_audit import CrewOriginAudit
from crypto_trader.events import OrderSubmissionAttempted, OrderVenueIdentityRecorded
from crypto_trader.execution import ExecutionRequest
from crypto_trader.tests import testnet_fixtures as fx
from crypto_trader.tests.production_fixtures import (
    bind_test_admission, seed_canonical_equity_root, startup_responses,
)
from crypto_trader.tests.test_production_startup_evidence import wired
from crypto_trader.tests.test_r03a1b_crew_gateway import plan, trusted_issue
from crypto_trader.tests.test_r03a1b_origin_binding import NOW, fresh

KEY = b"offline-test-host-secret-never-live" * 2


@pytest.fixture(autouse=True)
def deny_network(monkeypatch):
    def deny(*args, **kwargs):
        pytest.fail("authenticated recovery test attempted real network access")
    monkeypatch.setattr(socket, "create_connection", deny)
    monkeypatch.setattr(socket.socket, "connect", deny)
    monkeypatch.setattr(socket.socket, "connect_ex", deny)


@contextmanager
def _issued(root):
    proofs = root / "proofs"
    proofs.mkdir()
    with SQLiteEventJournal(root / "audit.db", venue_writes=True) as journal:
        seed_canonical_equity_root(journal)
        audit = CrewOriginAudit(proofs, key=KEY, journal_id=journal.journal_id())
        authority = CrewOriginAuthority(audit=audit)
        runtime, admission, port, client, transport = _runtime(journal, root, authority)
        runtime.start()
        gateway = CrewDecisionGateway(
            journal=journal, runtime=runtime, authority=authority,
            producer="gateway.crew_strategist", policy=lambda parsed: fresh("sealed-origin"),
        )
        candidate, source = trusted_issue(gateway, plan(), root)
        try:
            yield SimpleNamespace(journal=journal, audit=audit, authority=authority,
                runtime=runtime, admission=admission, port=port, client=client,
                transport=transport, candidate=candidate, source=source, proofs=proofs)
        finally:
            runtime.stop()


def _request(candidate):
    return ExecutionRequest(
        intent_id=candidate.intent_id, correlation_id=candidate.correlation_id,
        symbol=candidate.symbol, side=candidate.side, order_type=candidate.order_type,
        quantity=candidate.quantity, limit_price=candidate.limit_price,
        time_in_force=candidate.time_in_force, strategy_id=candidate.strategy_id,
    )


def _refuse(*args, **kwargs):
    raise ProductionStateError("authenticated origin evidence refused")


def _runtime(journal, root, authority):
    runtime, port, client, transport = wired(
        journal, root, startup_responses(), when=NOW,
        bind_admission=False, require_origin_binding=True,
        trusted_origin_producer="gateway.crew_strategist", origin_authority=authority,
    )
    admission = bind_test_admission(runtime, when=NOW)
    return runtime, admission, port, client, transport


@pytest.mark.parametrize("case", ["no_authority", "unkeyed", "other_journal", "non_bool"])
def test_explicit_keyed_writer_rejects_untrusted_configuration(tmp_path, case):
    """Explicit keyed writer mode must refuse unsafe wiring before startup."""
    with SQLiteEventJournal(tmp_path / "keyed.db", venue_writes=True) as journal:
        seed_canonical_equity_root(journal)
        proofs = tmp_path / "proofs"
        proofs.mkdir()
        authority = None
        if case == "unkeyed":
            authority = CrewOriginAuthority()
        elif case == "other_journal":
            authority = CrewOriginAuthority(audit=CrewOriginAudit(
                proofs, key=KEY, journal_id=uuid4(),
            ))
        elif case == "non_bool":
            authority = CrewOriginAuthority(audit=CrewOriginAudit(
                proofs, key=KEY, journal_id=journal.journal_id(),
            ))
        with pytest.raises(ProductionStateError, match="authenticated origin audit|explicit boolean"):
            wired(
                journal, tmp_path, startup_responses(), when=NOW,
                bind_admission=False, require_origin_binding=True,
                trusted_origin_producer="gateway.crew_strategist",
                origin_authority=authority,
                require_authenticated_origin_audit=(1 if case == "non_bool" else True),
            )


def test_explicit_keyed_writer_accepts_matching_host_proof_root(tmp_path):
    """An explicitly pinned host key and journal identity pass construction."""
    with SQLiteEventJournal(tmp_path / "keyed.db", venue_writes=True) as journal:
        seed_canonical_equity_root(journal)
        proofs = tmp_path / "proofs"
        proofs.mkdir()
        authority = CrewOriginAuthority(audit=CrewOriginAudit(
            proofs, key=KEY, journal_id=journal.journal_id(),
        ))
        runtime, _, _, transport = wired(
            journal, tmp_path, startup_responses(), when=NOW,
            bind_admission=False, require_origin_binding=True,
            trusted_origin_producer="gateway.crew_strategist",
            origin_authority=authority, require_authenticated_origin_audit=True,
        )
        bind_test_admission(runtime, when=NOW)
        runtime.start()
        assert not [req for req in transport.requests if req.method == "POST"]
        runtime.stop()


@pytest.mark.parametrize("case", ["no_reader", "wrong_digest", "missing_anchor", "non_bool"])
def test_external_checkpoint_mode_fails_closed_before_startup(tmp_path, case):
    """A caller opt-in is not a credential or a shortcut around a real host witness."""
    with SQLiteEventJournal(tmp_path / "checkpoint.db", venue_writes=True) as journal:
        seed_canonical_equity_root(journal)
        proofs = tmp_path / "proofs"
        proofs.mkdir()
        seed_audit = CrewOriginAudit(proofs, key=KEY, journal_id=journal.journal_id())
        baseline = seed_audit.export_checkpoint(journal)
        bad = dict(baseline)
        bad["digest"] = "0" * 64
        reader = (None if case == "no_reader" else
                  (lambda: None) if case == "missing_anchor" else
                  (lambda: bad) if case == "wrong_digest" else
                  (lambda: baseline))
        audit = CrewOriginAudit(
            proofs, key=KEY, journal_id=journal.journal_id(),
            checkpoint_reader=reader,
        )
        authority = CrewOriginAuthority(audit=audit)
        if case in ("no_reader", "non_bool"):
            with pytest.raises(ProductionStateError, match="checkpoint|boolean"):
                wired(
                    journal, tmp_path, startup_responses(), when=NOW,
                    bind_admission=False, require_origin_binding=True,
                    trusted_origin_producer="gateway.crew_strategist",
                    origin_authority=authority,
                    require_authenticated_origin_audit=True,
                    require_rollback_checkpoint=(1 if case == "non_bool" else True),
                )
        else:
            runtime, _, _, transport = wired(
                journal, tmp_path, startup_responses(), when=NOW,
                bind_admission=False, require_origin_binding=True,
                trusted_origin_producer="gateway.crew_strategist",
                origin_authority=authority,
                require_authenticated_origin_audit=True,
                require_rollback_checkpoint=True,
            )
            bind_test_admission(runtime, when=NOW)
            with pytest.raises(ProductionStateError, match="checkpoint"):
                runtime.start()
            assert not [r for r in transport.requests if r.method == "POST"]


def test_external_checkpoint_accepts_exact_host_witness_without_post(tmp_path):
    with SQLiteEventJournal(tmp_path / "checkpoint.db", venue_writes=True) as journal:
        seed_canonical_equity_root(journal)
        proofs = tmp_path / "proofs"
        proofs.mkdir()
        audit0 = CrewOriginAudit(proofs, key=KEY, journal_id=journal.journal_id())
        witness = audit0.export_checkpoint(journal)
        audit = CrewOriginAudit(
            proofs, key=KEY, journal_id=journal.journal_id(),
            checkpoint_reader=lambda: dict(witness),
        )
        authority = CrewOriginAuthority(audit=audit)
        runtime, _, _, transport = wired(
            journal, tmp_path, startup_responses(), when=NOW,
            bind_admission=False, require_origin_binding=True,
            trusted_origin_producer="gateway.crew_strategist",
            origin_authority=authority, require_authenticated_origin_audit=True,
            require_rollback_checkpoint=True,
        )
        bind_test_admission(runtime, when=NOW)
        runtime.start()
        assert not [r for r in transport.requests if r.method == "POST"]
        runtime.stop()


def test_external_anchor_detects_joint_journal_and_proof_rollback(tmp_path):
    """Valid old MACs and a self-consistent old journal must not pass a newer host pin."""
    with _issued(tmp_path) as issued:
        gateway = issued.authority._gateway
        def alternate_economics(_plan):
            candidate = fresh("second-economic-proposal")
            return replace(candidate, quantity=candidate.quantity * 2)
        gateway._policy = alternate_economics
        second, second_source = trusted_issue(gateway, plan(confidence="medium"), tmp_path)
        witness = issued.audit.export_checkpoint(issued.journal)
        assert len(issued.audit.audit(issued.journal)) == 2
        with issued.journal._conn:
            issued.journal._conn.execute(
                "DELETE FROM events WHERE event_id IN (?, ?)",
                (str(second.origin_decision_event_id), str(second.intent_id)),
            )
        (issued.proofs / (str(second.intent_id) + ".json")).unlink()
        assert issued.audit.audit(issued.journal) == (issued.source,)
        check = CrewOriginAudit(
            issued.proofs, key=KEY, journal_id=issued.journal.journal_id(),
            checkpoint_reader=lambda: witness,
        )
        with pytest.raises(ProductionStateError, match="checkpoint"):
            check.verify_external_checkpoint(issued.journal)


def test_startup_consults_origin_audit_before_any_venue_request(tmp_path):
    with SQLiteEventJournal(tmp_path / "audit.db", venue_writes=True) as journal:
        seed_canonical_equity_root(journal)
        authority = CrewOriginAuthority()
        authority._audit = SimpleNamespace(audit=_refuse)
        runtime, _, _, _, transport = _runtime(journal, tmp_path, authority)
        with pytest.raises(ProductionStateError, match="authenticated origin"):
            runtime.start()
        assert transport.requests == []


def test_root_sealing_failure_never_issues_session_authority(tmp_path):
    with SQLiteEventJournal(tmp_path / "audit.db", venue_writes=True) as journal:
        seed_canonical_equity_root(journal)
        authority = CrewOriginAuthority()
        runtime, _, _, _, transport = _runtime(journal, tmp_path, authority)
        runtime.start()
        authority._audit = SimpleNamespace(audit=lambda journal: (), seal_origin=_refuse)
        gateway = CrewDecisionGateway(
            journal=journal, runtime=runtime, authority=authority,
            producer="gateway.crew_strategist", policy=lambda parsed: fresh("seal-failure"),
        )
        with pytest.raises(ProductionStateError, match="authenticated origin"):
            trusted_issue(gateway, plan(), tmp_path)
        assert authority._tickets == {}
        assert not [request for request in transport.requests if request.method == "POST"]


def test_authenticated_source_survives_reopen_without_restoring_authority(tmp_path):
    with _issued(tmp_path) as issued:
        candidate, source = issued.candidate, issued.source
        assert issued.audit.audit(issued.journal) == (source,)
        journal_id = issued.journal.journal_id()
    with SQLiteEventJournal(tmp_path / "audit.db", venue_writes=True) as reopened:
        audit = CrewOriginAudit(tmp_path / "proofs", key=KEY, journal_id=journal_id)
        before = tuple(reopened.read_range())
        proof_bytes = {p.name: p.read_bytes() for p in (tmp_path / "proofs").iterdir()}
        assert audit.audit(reopened) == (source,)
        assert tuple(reopened.read_range()) == before
        assert proof_bytes == {p.name: p.read_bytes() for p in (tmp_path / "proofs").iterdir()}
        authority = CrewOriginAuthority(audit=audit)
        decision = reopened.read_by_id(candidate.origin_decision_event_id)
        created = reopened.read_by_id(candidate.intent_id)
        assert authority.verified(candidate, decision, created) is False
        runtime, admission, _, _, transport = _runtime(reopened, tmp_path, authority)
        runtime.start()
        assert admission.offer(candidate) == (False, "prior_session_intent")
        with pytest.raises(ProductionStateError, match="prior.session"):
            runtime._verify_durable_origin(candidate)
        assert not [r for r in transport.requests if r.method == "POST"]
        runtime.stop()


@pytest.mark.parametrize("mutation", ["missing", "corrupt", "extra", "source", "plan_hash"])
def test_mutable_proof_artifacts_cannot_authorize_restart(tmp_path, mutation):
    with _issued(tmp_path) as issued:
        journal_id = issued.journal.journal_id()
    proof = next((tmp_path / "proofs").iterdir())
    if mutation == "missing":
        proof.unlink()
    elif mutation == "corrupt":
        proof.write_bytes(proof.read_bytes() + b"garbage")
    elif mutation == "extra":
        (proof.parent / (str(uuid4()) + ".json")).write_bytes(proof.read_bytes())
    else:
        data = json.loads(proof.read_bytes())
        data["body"]["source" if mutation == "source" else "plan_sha256"] = (
            str(uuid4()) if mutation == "source" else "0" * 64
        )
        proof.write_text(json.dumps(data, sort_keys=True, separators=(",", ":")) + "\n")
    with SQLiteEventJournal(tmp_path / "audit.db", venue_writes=True) as journal:
        audit = CrewOriginAudit(tmp_path / "proofs", key=KEY, journal_id=journal_id)
        runtime, _, _, _, transport = _runtime(journal, tmp_path, CrewOriginAuthority(audit=audit))
        before = tuple(journal.read_range())
        with pytest.raises(ProductionStateError, match="origin audit"):
            runtime.start()
        assert transport.requests == []
        assert tuple(journal.read_range()) == before


@pytest.mark.parametrize("wrong", ["key", "journal"])
def test_independent_host_root_is_required(tmp_path, wrong):
    with _issued(tmp_path) as issued:
        audit = CrewOriginAudit(issued.proofs,
            key=b"different-host-secret" * 3 if wrong == "key" else KEY,
            journal_id=uuid4() if wrong == "journal" else issued.journal.journal_id())
        with pytest.raises(ProductionStateError, match="origin audit"):
            audit.audit(issued.journal)


def test_journal_tampering_with_recomputed_plain_hash_is_detected(tmp_path):
    with _issued(tmp_path) as issued:
        decision = issued.journal.read_by_id(issued.candidate.origin_decision_event_id)
        altered = decision.model_copy(update={"payload": decision.payload.model_copy(
            update={"rationale": "attacker recomputed ordinary SHA256"})})
        raw = altered.to_json()
        issued.journal._conn.execute(
            "UPDATE events SET payload_json=?, payload_sha256=? WHERE event_id=?",
            (raw, hashlib.sha256(raw.encode()).hexdigest(), str(decision.event_id)),
        )
        with pytest.raises(ProductionStateError, match="origin audit"):
            issued.audit.audit(issued.journal)


def test_real_durable_adapter_seals_attempt_and_venue_identity(tmp_path):
    with _issued(tmp_path) as issued:
        reference = "ct2-" + issued.candidate.intent_id.hex
        issued.transport.responses.append(fx.order_response(reference, orig_qty="0.01000"))
        assert issued.port.submit_durably(_request(issued.candidate), origin_audit=issued.audit) == reference
        attempts = [e for e in issued.journal.read_range() if isinstance(e, OrderSubmissionAttempted)]
        identities = [e for e in issued.journal.read_range() if isinstance(e, OrderVenueIdentityRecorded)]
        assert len(attempts) == len(identities) == 1
        assert attempts[0].payload.intent_id == identities[0].payload.intent_id == issued.candidate.intent_id
        assert issued.audit.audit(issued.journal) == (issued.source,)
        assert len([r for r in issued.transport.requests if r.method == "POST"]) == 1
        journal_id, source = issued.journal.journal_id(), issued.source
    with SQLiteEventJournal(tmp_path / "audit.db", venue_writes=True) as journal:
        audit = CrewOriginAudit(tmp_path / "proofs", key=KEY, journal_id=journal_id)
        assert audit.audit(journal) == (source,)


def test_attempt_sealing_failure_blocks_post_and_requires_reconciliation(tmp_path, monkeypatch):
    with _issued(tmp_path) as issued:
        monkeypatch.setattr(CrewOriginAudit, "seal_attempt", _refuse)
        with pytest.raises(ProductionStateError, match="authenticated origin"):
            issued.port.submit_durably(_request(issued.candidate), origin_audit=issued.audit)
        assert not [r for r in issued.transport.requests if r.method == "POST"]
        assert len([e for e in issued.journal.read_range() if isinstance(e, OrderSubmissionAttempted)]) == 1
        with pytest.raises(ProductionStateError, match="origin audit"):
            issued.audit.audit(issued.journal)
