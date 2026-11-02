"""Offline R-9 manual-hold inventory from a single immutable event snapshot.

This module has no journal, filesystem, transport or operator-disposition access.
Its output is diagnostic evidence, never a venue identity or resume authorization.
The caller, not this helper, must acquire and authenticate a complete snapshot.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict

from crypto_trader.events import (
    EXECUTION_FILL_EVENT_TYPE,
    ORDER_SUBMISSION_ATTEMPTED_EVENT_TYPE,
    ORDER_VENUE_IDENTITY_EVENT_TYPE,
)
from crypto_trader.events.registry import build_default_registry
from crypto_trader.recovery.reconstruction import OrderStateReconstructor


_SCHEMA = "r9-manual-hold-diagnostic"
_SCHEMA_VERSION = 1
_CANONICALIZATION = "event-model-json-sorted-keys-v1"
_TERMINAL_HOLDS = frozenset({"canceled", "expired", "rejected"})
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_SYMBOL = re.compile(r"[A-Z0-9]{2,30}\Z")
_MAX_EVENTS = 100_000
_MAX_EVENT_BYTES = 1_048_576
_MAX_SNAPSHOT_BYTES = 67_108_864


class ManualHoldEvidenceError(ValueError):
    """Stable, non-sensitive rejection code for an invalid diagnostic inventory."""

    def __init__(self, reason: str, position: int | None = None) -> None:
        self.reason = reason
        self.position = position
        label = reason if position is None else f"{reason} at event index {position}"
        super().__init__(label)


def _reject_duplicate_json_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_nonfinite_json(_value: str) -> None:
    raise ValueError("nonfinite JSON constant")


def _canonical_json(value: object) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
        allow_nan=False,
    )


def _uuid_or_none(value: object) -> str | None:
    return None if value is None else str(value)


def build_manual_hold_evidence(
    events: tuple[str, ...], *, symbol: str, source_fingerprint_sha256: str,
) -> bytes:
    """Build canonical diagnostic JSON from *one* caller-supplied snapshot.

    Each entry is a complete, canonical serialized registered event envelope,
    ordered as in SQLiteEventJournal.read_range(). There is no second read.
    The caller-supplied source hash and the packet's content hash are not
    authentication or authorization. No packet, even an empty one, releases R-9.
    """
    if type(events) is not tuple:
        raise ManualHoldEvidenceError("snapshot_must_be_immutable_tuple")
    if type(symbol) is not str or not _SYMBOL.fullmatch(symbol):
        raise ManualHoldEvidenceError("invalid_symbol")
    if (
        type(source_fingerprint_sha256) is not str
        or not _SHA256.fullmatch(source_fingerprint_sha256)
    ):
        raise ManualHoldEvidenceError("invalid_source_fingerprint_claim")
    if len(events) > _MAX_EVENTS:
        raise ManualHoldEvidenceError("snapshot_event_limit_exceeded")

    registry = build_default_registry()
    decoded = []
    canonical = []
    observed_ids: set[str] = set()
    last_position: tuple[object, int, str] | None = None
    snapshot_bytes = 0
    attempt_events: dict[str, list] = defaultdict(list)
    venue_identity_references: list[str] = []
    fills_by_order: dict[str, list] = defaultdict(list)

    for index, raw in enumerate(events):
        if type(raw) is not str:
            raise ManualHoldEvidenceError("snapshot_entry_must_be_canonical_text", index)
        if len(raw) > _MAX_EVENT_BYTES:
            raise ManualHoldEvidenceError("snapshot_size_limit_exceeded", index)
        try:
            size = len(raw.encode("utf-8"))
        except UnicodeError:
            raise ManualHoldEvidenceError("invalid_utf8_snapshot_event", index) from None
        snapshot_bytes += size
        if size > _MAX_EVENT_BYTES or snapshot_bytes > _MAX_SNAPSHOT_BYTES:
            raise ManualHoldEvidenceError("snapshot_size_limit_exceeded", index)
        try:
            object_value = json.loads(
                raw, object_pairs_hook=_reject_duplicate_json_keys,
                parse_constant=_reject_nonfinite_json,
            )
            if not isinstance(object_value, dict) or "event_id" not in object_value:
                raise ValueError("missing event identity")
            event = registry.decode(object_value)
            canonical_event = _canonical_json(event.model_dump(mode="json"))
        except (ValueError, TypeError, OverflowError):
            raise ManualHoldEvidenceError("invalid_event_contract_or_json", index) from None
        except Exception:
            # Registry errors may contain source text; never disclose that text.
            raise ManualHoldEvidenceError("invalid_event_contract_or_json", index) from None
        if raw != canonical_event:
            raise ManualHoldEvidenceError("noncanonical_snapshot_event", index)
        identity = str(event.event_id)
        if identity in observed_ids:
            raise ManualHoldEvidenceError("duplicate_event_identity", index)
        observed_ids.add(identity)
        position = (event.occurred_at, event.sequence_number or 0, identity)
        if last_position is not None and position < last_position:
            raise ManualHoldEvidenceError("snapshot_out_of_order", index)
        last_position = position
        if event.symbol is not None and event.symbol != symbol:
            raise ManualHoldEvidenceError("mixed_symbol_snapshot", index)
        decoded.append(event)
        canonical.append(canonical_event)
        if event.event_type == ORDER_SUBMISSION_ATTEMPTED_EVENT_TYPE:
            attempt_events[str(event.payload.venue_reference)].append(event)
        elif event.event_type == ORDER_VENUE_IDENTITY_EVENT_TYPE:
            venue_identity_references.append(str(event.payload.venue_reference))
        elif event.event_type == EXECUTION_FILL_EVENT_TYPE and event.order_id is not None:
            fills_by_order[str(event.order_id)].append(event)

    reconstructed = OrderStateReconstructor()
    try:
        for event in decoded:
            reconstructed.handle(event)
        if reconstructed.conflicts():
            raise ManualHoldEvidenceError("conflicted_reconstruction")
    except ManualHoldEvidenceError:
        raise
    except Exception:
        raise ManualHoldEvidenceError("reconstruction_failed") from None

    anchors_by_reference = reconstructed.attempt_anchor_map()
    if any(ref not in anchors_by_reference for ref in venue_identity_references):
        raise ManualHoldEvidenceError("orphan_venue_identity")
    if any(anchor.symbol != symbol for anchor in anchors_by_reference.values()):
        raise ManualHoldEvidenceError("mixed_symbol_attempt_anchors")

    cases = []
    states = reconstructed.states()
    if any(state.symbol != symbol for state in states):
        raise ManualHoldEvidenceError("mixed_symbol_order_states")
    for state in sorted(states, key=lambda item: item.order_reference):
        if not (
            state.submission_attempted
            and state.state.value in _TERMINAL_HOLDS
            and state.venue_order_id is None
        ):
            continue
        anchor = anchors_by_reference.get(state.order_reference)
        contributors = attempt_events.get(state.order_reference, [])
        if anchor is None or not contributors or contributors[0].order_id != anchor.order_id:
            raise ManualHoldEvidenceError("missing_or_mismatched_attempt_anchor")
        original = contributors[0]
        cases.append({
            "client_order_reference": state.order_reference,
            "local_order_uuid": str(state.order_id),
            "intent_uuid": _uuid_or_none(state.intent_id),
            "symbol": state.symbol,
            "local_terminal_lifecycle": state.state.value,
            "journaled_venue_order_id": None,
            "local_filled_quantity_not_execution_proof": str(state.filled_quantity),
            "local_remaining_quantity_not_execution_proof": str(state.remaining_quantity),
            "attempt_anchor": {
                "order_reference": anchor.order_reference,
                "order_uuid": str(anchor.order_id),
                "intent_uuid": str(anchor.intent_id),
                "correlation_uuid": _uuid_or_none(anchor.correlation_id),
                "symbol": anchor.symbol,
                "strategy_id": anchor.strategy_id,
                "side": anchor.side,
                "order_type": anchor.order_type,
                "original_quantity": str(anchor.quantity),
                "limit_price": None if anchor.limit_price is None else str(anchor.limit_price),
                "time_in_force": anchor.time_in_force,
                "venue_producer": anchor.venue_producer,
                "occurred_at_ns": anchor.occurred_at_ns,
            },
            "retained_attempt_event": {
                "event_uuid": str(original.event_id),
                "producer": original.producer,
                "correlation_uuid": _uuid_or_none(original.correlation_id),
                "causation_uuid": _uuid_or_none(original.causation_id),
                "sequence_number": original.sequence_number,
                "occurred_at": original.occurred_at.isoformat(),
            },
            "all_attempt_event_uuids": [str(item.event_id) for item in contributors],
            "local_fill_records_not_venue_proof": [
                {
                    "event_uuid": str(fill.event_id),
                    "producer": fill.producer,
                    "occurred_at": fill.occurred_at.isoformat(),
                    "quantity": str(fill.payload.quantity),
                    "remaining_quantity": (
                        None if fill.payload.remaining_quantity is None
                        else str(fill.payload.remaining_quantity)
                    ),
                }
                for fill in fills_by_order.get(str(state.order_id), [])
            ],
            "disposition": "UNRESOLVED_REQUIRES_INDEPENDENT_EVIDENCE",
        })

    snapshot_identity = _canonical_json({
        "domain": "crypto-trader:r9-manual-hold:snapshot-content:v1",
        "symbol": symbol,
        "event_envelopes": canonical,
    }).encode("utf-8")
    packet = {
        "schema": _SCHEMA,
        "schema_version": _SCHEMA_VERSION,
        "canonicalization": _CANONICALIZATION,
        "purpose": "diagnostic_only",
        "may_resume_trading": False,
        "source_fingerprint_sha256": source_fingerprint_sha256,
        "source_fingerprint_verified": False,
        "snapshot_content_sha256": hashlib.sha256(snapshot_identity).hexdigest(),
        "snapshot_content_authenticity_verified": False,
        "symbol": symbol,
        "snapshot_event_count": len(decoded),
        "case_count": len(cases),
        "cases": cases,
    }
    return _canonical_json(packet).encode("utf-8")
