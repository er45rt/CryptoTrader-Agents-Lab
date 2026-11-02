"""Stage-A R-9 diagnostic packet: offline, immutable and never a clearance API."""

from __future__ import annotations

import builtins
import hashlib
import json
import os
import socket
import sqlite3
import subprocess
from pathlib import Path
from uuid import UUID, uuid5

import pytest

from crypto_trader.recovery.manual_hold_evidence import (
    ManualHoldEvidenceError,
    build_manual_hold_evidence,
)
from crypto_trader.tests import test_recovery_submission_attempt as f


SOURCE = "a" * 64  # Caller-provided claim, deliberately not trusted or authenticated.


def _canonical(event):
    return json.dumps(event.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))


def _snapshot(*events):
    ordered = sorted(events, key=lambda e: (e.occurred_at, e.sequence_number or 0, str(e.event_id)))
    return tuple(_canonical(event) for event in ordered)


def _state(name, **kwargs):
    if name in {"canceled", "expired", "rejected"}:
        kwargs.setdefault("reason", f"venue_status_{name}")
    return f.state_event(name, **kwargs)


def _packet(snapshot, *, symbol=f.SYMBOL, source=SOURCE):
    return json.loads(build_manual_hold_evidence(
        snapshot, symbol=symbol, source_fingerprint_sha256=source,
    ))


def _hold(state="canceled"):
    return _snapshot(f.attempt_event(index=0), _state(state, index=1))


def _reject(snapshot, reason):
    with pytest.raises(ManualHoldEvidenceError) as raised:
        _packet(snapshot)
    assert raised.value.reason == reason
    assert f.CLIENT_ORDER_ID not in str(raised.value)


@pytest.mark.parametrize("state", ["canceled", "expired", "rejected"])
def test_attempted_unknown_identity_is_a_diagnostic_hold(state):
    packet = _packet(_hold(state))
    assert packet["case_count"] == 1
    assert packet["cases"][0]["local_terminal_lifecycle"] == state
    assert packet["cases"][0]["journaled_venue_order_id"] is None
    assert packet["cases"][0]["disposition"].startswith("UNRESOLVED")
    assert packet["may_resume_trading"] is False
    assert packet["source_fingerprint_verified"] is False
    assert packet["snapshot_content_authenticity_verified"] is False


def test_known_venue_identity_excluded_without_clearance():
    packet = _packet(_snapshot(
        f.attempt_event(index=0), f.identity_event(index=1),
        _state("canceled", index=2),
    ))
    assert packet["cases"] == [] and packet["case_count"] == 0
    assert packet["may_resume_trading"] is False


@pytest.mark.parametrize("records", [(), ("never_attempted",), ("filled",)])
def test_empty_never_attempted_and_filled_do_not_authorize(records):
    if records == ("never_attempted",):
        events = _snapshot(_state("created", index=0))
    elif records == ("filled",):
        events = _snapshot(
            f.attempt_event(index=0),
            f.fill_event("1.00000000", index=1, remaining="0"),
        )
    else:
        events = ()
    packet = _packet(events)
    assert packet["cases"] == []
    assert packet["may_resume_trading"] is False


def test_legacy_terminal_without_durable_attempt_is_rejected_not_silently_excluded():
    # Recovery may infer submission_attempted for legacy terminal lifecycle records.
    # That is NOT independent evidence of an original durable attempt anchor.
    _reject(_snapshot(_state("canceled", index=0)), "missing_or_mismatched_attempt_anchor")


def test_complete_named_provenance_preserves_null_and_supporting_event_id():
    attempt = f.attempt_event(index=0)
    state = _state("canceled", index=1)
    packet = _packet(_snapshot(attempt, state))
    case = packet["cases"][0]
    assert case["client_order_reference"] == f.CLIENT_ORDER_ID
    assert case["local_order_uuid"] == str(attempt.order_id)
    assert case["intent_uuid"] == str(attempt.payload.intent_id)
    assert case["attempt_anchor"] == {
        "order_reference": f.CLIENT_ORDER_ID,
        "order_uuid": str(attempt.order_id),
        "intent_uuid": str(attempt.payload.intent_id),
        "correlation_uuid": str(attempt.correlation_id),
        "symbol": f.SYMBOL,
        "strategy_id": attempt.strategy_id,
        "side": "buy",
        "order_type": "limit",
        "original_quantity": "1.00000000",
        "limit_price": "100.00000000",
        "time_in_force": "gtc",
        "venue_producer": f.VENUE_PRODUCER,
        "occurred_at_ns": int(attempt.occurred_at.timestamp() * 1_000_000_000),
    }
    assert case["retained_attempt_event"]["event_uuid"] == str(attempt.event_id)
    assert case["retained_attempt_event"]["producer"] == attempt.producer
    assert case["retained_attempt_event"]["causation_uuid"] is None
    assert case["retained_attempt_event"]["sequence_number"] is None
    assert case["all_attempt_event_uuids"] == [str(attempt.event_id)]
    assert case["local_fill_records_not_venue_proof"] == []


def test_compatible_repeated_attempts_report_all_sources_without_claiming_acceptance():
    first = f.attempt_event(index=0)
    second = f.attempt_event(index=1)
    report = _packet(_snapshot(first, second, _state("canceled", index=2)))
    assert report["cases"][0]["all_attempt_event_uuids"] == [
        str(first.event_id), str(second.event_id),
    ]


def test_local_fill_is_observation_not_execution_proof_or_release():
    events = _snapshot(
        f.attempt_event(index=0),
        f.fill_event("0.25", index=1, remaining="0.75"),
        _state("canceled", index=2),
    )
    packet = _packet(events)
    assert packet["case_count"] == 1
    case = packet["cases"][0]
    assert case["local_filled_quantity_not_execution_proof"] == "0.25"
    assert len(case["local_fill_records_not_venue_proof"]) == 1
    assert packet["may_resume_trading"] is False


@pytest.mark.parametrize("bad", [[], {}, "event", None, iter(())])
def test_mutable_or_non_tuple_snapshot_is_rejected(bad):
    _reject(bad, "snapshot_must_be_immutable_tuple")


def test_object_is_rejected_even_when_it_is_a_valid_event():
    _reject((f.attempt_event(),), "snapshot_entry_must_be_canonical_text")


@pytest.mark.parametrize("raw", ["garbage", "[]", "{}", "null", "NaN"])
def test_invalid_serialized_entry_rejects_without_payload_leak(raw):
    _reject((raw,), "invalid_event_contract_or_json")


def test_invalid_unicode_and_oversized_snapshot_fail_with_fixed_errors():
    _reject((chr(0xD800),), "invalid_utf8_snapshot_event")
    _reject(("x" * 1_048_577,), "snapshot_size_limit_exceeded")


def test_missing_event_id_does_not_generate_a_new_uuid():
    data = json.loads(_canonical(f.attempt_event()))
    data.pop("event_id")
    _reject((json.dumps(data, sort_keys=True, separators=(",", ":")),),
            "invalid_event_contract_or_json")


@pytest.mark.parametrize("mutation", [
    lambda d: d.update(event_type="unknown.random.event"),
    lambda d: d.update(schema_version=900),
    lambda d: d.update(unexpected_data="this must be rejected"),
    lambda d: d["payload"].update(quantity="-5"),
])
def test_unknown_or_invalid_contract_rejected(mutation):
    data = json.loads(_canonical(f.attempt_event()))
    mutation(data)
    raw = json.dumps(data, sort_keys=True, separators=(",", ":"))
    _reject((raw,), "invalid_event_contract_or_json")


def test_duplicate_json_key_rejected_before_model_validation():
    raw = _canonical(f.attempt_event())
    assert raw.startswith('{"causation_id":')
    bad = raw.replace('{"causation_id":', '{"causation_id":null,"causation_id":', 1)
    _reject((bad,), "invalid_event_contract_or_json")


def test_noncanonical_spacing_or_omitted_default_rejected():
    canonical = _canonical(f.attempt_event())
    _reject((json.dumps(json.loads(canonical), indent=2),), "noncanonical_snapshot_event")
    data = json.loads(canonical)
    data.pop("causation_id")
    _reject((json.dumps(data, sort_keys=True, separators=(",", ":")),),
            "noncanonical_snapshot_event")


@pytest.mark.parametrize("changed", [False, True])
def test_duplicate_event_id_rejected_even_if_payloads_differ(changed):
    attempt = f.attempt_event(index=0)
    second = (_state("canceled", index=1).model_copy(update={"event_id": attempt.event_id})
              if changed else attempt)
    _reject(_snapshot(attempt, second), "duplicate_event_identity")


@pytest.mark.parametrize("events", [
    lambda: (f.attempt_event(index=0), f.attempt_event(index=1, quantity="2")),
    lambda: (f.attempt_event(index=0), f.attempt_event(
        index=1, client_order_id=f.OTHER_CLIENT_ORDER_ID, order_id=f.ORDER_ID)),
    lambda: (f.attempt_event(index=0, venue_producer="unknown.venue"),),
    lambda: (f.identity_event(index=0),),
    lambda: (f.attempt_event(index=0), f.identity_event("4242", index=1),
             f.identity_event("777", index=2)),
    lambda: (f.attempt_event(index=0), _state("canceled", index=1),
             _state("accepted", index=2)),
])
def test_inconsistent_reconstruction_never_produces_empty_success(events):
    with pytest.raises(ManualHoldEvidenceError) as raised:
        _packet(_snapshot(*events()))
    assert raised.value.reason in {"conflicted_reconstruction", "orphan_venue_identity"}


def test_scope_and_order_reject_instead_of_silent_reordering():
    attempt = f.attempt_event(index=0)
    state = _state("canceled", index=1)
    _reject((_canonical(state), _canonical(attempt)), "snapshot_out_of_order")
    other = attempt.model_copy(update={"symbol": "OTHERUSDT"})
    _reject((_canonical(other),), "mixed_symbol_snapshot")


def test_identical_timestamp_obeys_sequence_then_uuid_order():
    attempt = f.attempt_event(index=0, sequence_number=1)
    state = _state("canceled", index=0).model_copy(update={"sequence_number": 2})
    correct = (_canonical(attempt), _canonical(state))
    assert _packet(correct)["case_count"] == 1
    _reject(tuple(reversed(correct)), "snapshot_out_of_order")


def test_full_content_digest_detects_same_id_modified_payload_and_snapshot_size():
    attempt = f.attempt_event(index=0)
    state = _state("canceled", index=1, reason="original")
    before = _snapshot(attempt, state)
    modified = state.model_copy(update={"payload": state.payload.model_copy(
        update={"reason": "new reason"})})
    after = _snapshot(attempt, modified)
    assert [json.loads(x)["event_id"] for x in before] == [
        json.loads(x)["event_id"] for x in after]
    before_bytes = build_manual_hold_evidence(before, symbol=f.SYMBOL,
                                              source_fingerprint_sha256=SOURCE)
    assert before_bytes == build_manual_hold_evidence(before, symbol=f.SYMBOL,
                                                      source_fingerprint_sha256=SOURCE)
    after_bytes = build_manual_hold_evidence(after, symbol=f.SYMBOL,
                                             source_fingerprint_sha256=SOURCE)
    assert json.loads(before_bytes)["snapshot_content_sha256"] != json.loads(after_bytes)["snapshot_content_sha256"]
    assert json.loads(before_bytes)["snapshot_content_sha256"] != _packet((
        _canonical(attempt),))["snapshot_content_sha256"]
    assert b'"may_resume_trading":false' in before_bytes


def test_snapshot_is_independent_of_mutating_a_later_event_copy():
    attempt = f.attempt_event(index=0)
    state = _state("canceled", index=1)
    snap = _snapshot(attempt, state)
    replacement = state.model_copy(update={"event_id": UUID(int=99)})
    assert replacement.event_id != state.event_id
    assert snap == _snapshot(attempt, state)
    assert _packet(snap)["case_count"] == 1


def test_pure_builder_does_not_open_files_sqlite_or_network(monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("forbidden side effect")

    snapshot = _hold()
    monkeypatch.setattr(builtins, "open", forbidden)
    monkeypatch.setattr(Path, "open", forbidden)
    monkeypatch.setattr(sqlite3, "connect", forbidden)
    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(os, "getenv", forbidden)
    assert _packet(snapshot)["case_count"] == 1


@pytest.mark.parametrize("count", [3, 10, 1000])
def test_large_hold_inventory_stays_offline_without_status_query_budget(count):
    generated = []
    for index in range(count):
        identity = uuid5(UUID("00000000-0000-0000-0000-000000000012"), str(index))
        reference = f"ct-{index:020d}"
        generated.append(f.attempt_event(
            index=index * 2, client_order_id=reference, order_id=identity,
            intent_id=identity,
        ))
        generated.append(_state(
            "canceled", index=index * 2 + 1, order_id=identity,
        ))
    raw = _snapshot(*generated)
    packet = _packet(raw)
    assert packet["case_count"] == count
    assert packet["snapshot_event_count"] == 2 * count
    assert packet["may_resume_trading"] is False
    assert packet["snapshot_content_sha256"] == _packet(raw)["snapshot_content_sha256"]


def test_input_provenance_claim_requires_exact_lowercase_sha256():
    for bad in ("x", "A" * 64, "0" * 63, "0" * 65, None):
        with pytest.raises(ManualHoldEvidenceError, match="invalid_source_fingerprint_claim"):
            _packet((), source=bad)
    assert hashlib.sha256(b"").hexdigest() != SOURCE
