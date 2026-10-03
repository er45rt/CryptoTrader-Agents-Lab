"""Offline regression tests for durable production decision identity."""

from __future__ import annotations

from decimal import Decimal

import pytest

from crypto_trader.execution.binance_testnet import ScriptedTransport
from crypto_trader.execution.binance_testnet import TestnetOrderRuntime as OrderRuntime
from crypto_trader.persistence import EventIdConflictError, SQLiteEventJournal
from crypto_trader.production.runtime import (
    _durable_decision_identity,
    _durable_intent_anchor,
)
from crypto_trader.tests import testnet_fixtures as fx
from crypto_trader.tests.production_fixtures import (
    INSTRUMENT,
    admitted_prepare,
    build_runtime,
    canonical_risk_refresh_responses,
    canonical_test_risk_source,
    control_for,
    intent,
    seed_canonical_equity_root,
    startup_responses,
)


def test_same_decision_different_caller_ids_share_one_durable_identity():
    first = intent("durable-identity-first")
    replay = intent("durable-identity-replay")

    assert first.intent_id != replay.intent_id
    assert first.correlation_id != replay.correlation_id
    assert _durable_decision_identity(first) == _durable_decision_identity(replay)


def test_exact_replay_anchor_is_byte_idempotent_across_new_caller_ids(tmp_path):
    first = intent("durable-anchor-first")
    replay = intent("durable-anchor-replay")
    identity = _durable_decision_identity(first)

    first_anchor = _durable_intent_anchor(first, identity)
    replay_anchor = _durable_intent_anchor(replay, identity)

    assert first_anchor == replay_anchor
    assert first_anchor.event_id == identity
    assert first_anchor.correlation_id == identity

    with SQLiteEventJournal(tmp_path / "journal.db") as journal:
        assert journal.append(first_anchor) is True
        assert journal.append(replay_anchor) is False
        assert journal.count() == 1


def test_same_decision_changed_economics_conflicts_fail_closed(tmp_path):
    first = intent("durable-conflict-first")
    changed = intent(
        "durable-conflict-replay",
        quantity=Decimal("0.02000"),
    )

    first_identity = _durable_decision_identity(first)
    changed_identity = _durable_decision_identity(changed)
    assert first_identity == changed_identity

    with SQLiteEventJournal(tmp_path / "journal.db") as journal:
        assert journal.append(_durable_intent_anchor(first, first_identity)) is True
        with pytest.raises(EventIdConflictError):
            journal.append(_durable_intent_anchor(changed, changed_identity))
        assert journal.count() == 1


def test_new_decision_evidence_gets_a_new_durable_identity():
    first = intent("durable-new-decision", decision_scope="entry")
    other_scope = intent("durable-new-scope", decision_scope="exit")

    assert _durable_decision_identity(first) != _durable_decision_identity(other_scope)


def test_durable_runtime_prepares_with_canonical_identity_before_hard_risk(tmp_path):
    target = intent("durable-runtime-canonical")
    canonical_id = _durable_decision_identity(target)
    transport = ScriptedTransport(
        responses=[*startup_responses(), *canonical_risk_refresh_responses()]
    )
    client = fx.client(transport)

    with SQLiteEventJournal(tmp_path / "journal.db") as journal:
        state_dir = tmp_path / "state"
        seed_canonical_equity_root(journal)
        control = control_for(journal, state_dir)
        port = OrderRuntime(client=client, instrument=INSTRUMENT, journal=journal)
        risk_source = canonical_test_risk_source(
            journal=journal, control=control, client=client
        )
        runtime = build_runtime(
            journal=journal,
            state_dir=state_dir,
            execution=port,
            control=control,
            risk_state_source=risk_source,
            bind_admission=True,
        )
        runtime.start()

        preparation = admitted_prepare(runtime, target)

        assert preparation.approved is True
        assert preparation.intent_id == canonical_id
        assert preparation.intent_id != target.intent_id
        assert preparation.stages[0] == "identity:durable"
        anchor = journal.read_by_id(canonical_id)
        assert anchor is not None
        assert anchor.event_type == "trade.intent.created"
        assert not [request for request in transport.requests if request.method == "POST"]
