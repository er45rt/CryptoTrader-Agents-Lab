"""The break-glass epoch: the fail-closed posture an operator activation opens (Phase 13).

An epoch is opened by the first accepted break-glass action and stays open until an explicit
operator restore returns the system to ``normal``. While it is open:

* no new risk may be created (the kill switch is engaged and the emergency writer owns the lease);
* reconciliation is mandatory, and its result is recorded on the epoch;
* an explicit operator acknowledgement is mandatory;
* ``normal`` is never restored automatically - by a restart, a timeout, an AI decision or a
  command that merely "looks healthy" again.

The epoch is DERIVED from the authoritative journal (the additive
``safety.operator_command.recorded`` contract plus the Phase 9 ``safety.state.changed`` contract).
The JSON cache is an acceleration only: if it is missing or corrupt the epoch is rebuilt from the
journal, and if the safety state is post-break-glass but no provenance exists in the journal, an
epoch is synthesised as ``provenance_missing`` and the mandatory gates stay engaged - fail closed,
never "assume it was fine".
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any, Literal
from uuid import UUID, uuid4

from crypto_trader.events import OPERATOR_COMMAND_EVENT_TYPE
from crypto_trader.persistence import EventJournal
from crypto_trader.safety import SafetyMode

from .errors import BreakGlassStateError

__all__ = [
    "EPOCH_SCHEMA_VERSION",
    "BreakGlassEpoch",
    "BreakGlassLedger",
    "OperatorAcknowledgement",
    "RecordedOperatorCommand",
    "derive_ledger",
]

EPOCH_SCHEMA_VERSION = 1

#: Reconciliation states an epoch can be in.
ReconciliationStatus = Literal["not_required", "pending", "resolved", "failed"]


@dataclass(frozen=True)
class OperatorAcknowledgement:
    """One explicit operator acknowledgement of a completed reconciliation."""

    operator_label: str
    operator_identity: str | None
    acknowledged_at: datetime
    command_id: UUID
    reconciliation_reference: str | None = None

    def to_record(self) -> dict[str, Any]:
        return {
            "operator_label": self.operator_label,
            "operator_identity": self.operator_identity,
            "acknowledged_at": self.acknowledged_at.astimezone(timezone.utc).isoformat(),
            "command_id": str(self.command_id),
            "reconciliation_reference": self.reconciliation_reference,
        }

    @classmethod
    def from_record(cls, payload: Mapping[str, Any]) -> "OperatorAcknowledgement":
        try:
            return cls(
                operator_label=str(payload["operator_label"]),
                operator_identity=(
                    None
                    if payload.get("operator_identity") is None
                    else str(payload["operator_identity"])
                ),
                acknowledged_at=datetime.fromisoformat(str(payload["acknowledged_at"])),
                command_id=UUID(str(payload["command_id"])),
                reconciliation_reference=(
                    None
                    if payload.get("reconciliation_reference") is None
                    else str(payload["reconciliation_reference"])
                ),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise BreakGlassStateError(f"the acknowledgement record is not usable: {exc}") from exc


@dataclass(frozen=True)
class BreakGlassEpoch:
    """The durable, fail-closed break-glass posture."""

    session_id: UUID
    opened_at: datetime
    opened_by_command_id: UUID
    opened_by: str
    opened_by_reason_code: str
    emergency_owner: str
    fence_term: int
    actions: tuple[str, ...] = ()
    escalation_path: tuple[str, ...] = ()
    reconciliation_status: ReconciliationStatus = "pending"
    reconciliation_reason_code: str = "reconciliation_required"
    reconciliation_checked_at: datetime | None = None
    acknowledgement: OperatorAcknowledgement | None = None
    closed_at: datetime | None = None
    closed_by_command_id: UUID | None = None
    provenance: Literal["recorded", "journal_replay", "provenance_missing"] = "recorded"
    schema_version: int = EPOCH_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != EPOCH_SCHEMA_VERSION:
            raise BreakGlassStateError(
                f"unsupported break-glass epoch schema {self.schema_version}; refusing to guess."
            )
        if self.reconciliation_status not in ("not_required", "pending", "resolved", "failed"):
            raise BreakGlassStateError(
                f"unknown reconciliation status {self.reconciliation_status!r}."
            )
        if not self.escalation_path:
            raise BreakGlassStateError("a break-glass epoch must record its escalation path.")

    # --- reads ---

    @property
    def is_open(self) -> bool:
        return self.closed_at is None

    @property
    def reconciliation_satisfied(self) -> bool:
        return self.reconciliation_status in ("resolved", "not_required")

    @property
    def acknowledgement_satisfied(self) -> bool:
        return self.acknowledgement is not None

    @property
    def new_risk_permitted(self) -> bool:
        """A break-glass epoch never permits new risk while it is open."""
        return not self.is_open

    @property
    def restore_blockers(self) -> tuple[str, ...]:
        """Every gate that currently blocks a return to ``normal`` (empty when restorable)."""
        blockers: list[str] = []
        if not self.reconciliation_satisfied:
            blockers.append("reconciliation_pending")
        if not self.acknowledgement_satisfied:
            blockers.append("operator_acknowledgement_missing")
        return tuple(blockers)

    @property
    def current_mode(self) -> str:
        return self.escalation_path[-1]

    def with_updates(self, **changes: Any) -> "BreakGlassEpoch":
        return replace(self, **changes)

    # --- serialization (cache only; the journal stays authoritative) ---

    def to_record(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "session_id": str(self.session_id),
            "opened_at": self.opened_at.astimezone(timezone.utc).isoformat(),
            "opened_by_command_id": str(self.opened_by_command_id),
            "opened_by": self.opened_by,
            "opened_by_reason_code": self.opened_by_reason_code,
            "emergency_owner": self.emergency_owner,
            "fence_term": self.fence_term,
            "actions": list(self.actions),
            "escalation_path": list(self.escalation_path),
            "reconciliation_status": self.reconciliation_status,
            "reconciliation_reason_code": self.reconciliation_reason_code,
            "reconciliation_checked_at": (
                None
                if self.reconciliation_checked_at is None
                else self.reconciliation_checked_at.astimezone(timezone.utc).isoformat()
            ),
            "acknowledgement": (
                None if self.acknowledgement is None else self.acknowledgement.to_record()
            ),
            "closed_at": (
                None
                if self.closed_at is None
                else self.closed_at.astimezone(timezone.utc).isoformat()
            ),
            "closed_by_command_id": (
                None if self.closed_by_command_id is None else str(self.closed_by_command_id)
            ),
            "provenance": self.provenance,
        }

    @classmethod
    def from_record(cls, payload: Mapping[str, Any]) -> "BreakGlassEpoch":
        try:
            acknowledgement = payload.get("acknowledgement")
            return cls(
                schema_version=int(payload["schema_version"]),
                session_id=UUID(str(payload["session_id"])),
                opened_at=datetime.fromisoformat(str(payload["opened_at"])),
                opened_by_command_id=UUID(str(payload["opened_by_command_id"])),
                opened_by=str(payload["opened_by"]),
                opened_by_reason_code=str(payload["opened_by_reason_code"]),
                emergency_owner=str(payload["emergency_owner"]),
                fence_term=int(payload["fence_term"]),
                actions=tuple(str(item) for item in payload.get("actions", ())),
                escalation_path=tuple(str(item) for item in payload.get("escalation_path", ())),
                reconciliation_status=str(payload["reconciliation_status"]),  # type: ignore[arg-type]
                reconciliation_reason_code=str(payload["reconciliation_reason_code"]),
                reconciliation_checked_at=(
                    None
                    if payload.get("reconciliation_checked_at") is None
                    else datetime.fromisoformat(str(payload["reconciliation_checked_at"]))
                ),
                acknowledgement=(
                    None
                    if acknowledgement is None
                    else OperatorAcknowledgement.from_record(acknowledgement)
                ),
                closed_at=(
                    None
                    if payload.get("closed_at") is None
                    else datetime.fromisoformat(str(payload["closed_at"]))
                ),
                closed_by_command_id=(
                    None
                    if payload.get("closed_by_command_id") is None
                    else UUID(str(payload["closed_by_command_id"]))
                ),
                provenance=str(payload.get("provenance", "recorded")),  # type: ignore[arg-type]
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise BreakGlassStateError(f"the break-glass epoch record is not usable: {exc}") from exc


@dataclass(frozen=True)
class RecordedOperatorCommand:
    """The journal's view of one recorded operator command (the idempotency ledger entry)."""

    command_id: UUID
    command_type: str
    command_digest: str
    requested_at: datetime
    mode_path: tuple[str, ...]
    operator_label: str
    operator_identity: str | None
    reason: str
    source: str
    previous_safety_state: str
    resulting_safety_state: str
    disposition: str
    reason_code: str
    venue_control: str
    reconciliation_status: str
    epoch_opened: bool
    epoch_closed: bool
    fence_term: int
    event_id: UUID
    correlation_id: UUID
    causation_id: UUID | None

    @property
    def accepted(self) -> bool:
        return self.disposition == "accepted"

    def to_record(self) -> dict[str, Any]:
        """The audit evidence for this command (never contains credential material)."""
        return {
            "command_id": str(self.command_id),
            "command_type": self.command_type,
            "requested_at": self.requested_at.astimezone(timezone.utc).isoformat(),
            "operator_label": self.operator_label,
            "operator_identity": self.operator_identity,
            "reason": self.reason,
            "source": self.source,
            "previous_safety_state": self.previous_safety_state,
            "resulting_safety_state": self.resulting_safety_state,
            "disposition": self.disposition,
            "reason_code": self.reason_code,
            "command_digest": self.command_digest,
            "mode_path": list(self.mode_path),
            "venue_control": self.venue_control,
            "reconciliation_status": self.reconciliation_status,
            "epoch_opened": self.epoch_opened,
            "epoch_closed": self.epoch_closed,
            "fence_term": self.fence_term,
            "correlation_id": str(self.correlation_id),
            "causation_id": None if self.causation_id is None else str(self.causation_id),
            "event_id": str(self.event_id),
        }


@dataclass(frozen=True)
class BreakGlassLedger:
    """The journal-derived break-glass ledger: the open epoch plus every recorded command."""

    epoch: BreakGlassEpoch | None = None
    commands: dict[UUID, RecordedOperatorCommand] = field(default_factory=dict)

    def command(self, command_id: UUID) -> RecordedOperatorCommand | None:
        return self.commands.get(command_id)

    @property
    def records(self) -> list[RecordedOperatorCommand]:
        """Every recorded command in authoritative journal (arrival) order."""
        return list(self.commands.values())

    @property
    def open_epoch(self) -> BreakGlassEpoch | None:
        return self.epoch if self.epoch is not None and self.epoch.is_open else None


def derive_ledger(journal: EventJournal) -> BreakGlassLedger:
    """Rebuild the ledger from the authoritative journal (no cache, no guessing)."""
    commands: dict[UUID, RecordedOperatorCommand] = {}
    epoch: BreakGlassEpoch | None = None
    for event in journal.replay():
        if event.event_type != OPERATOR_COMMAND_EVENT_TYPE:
            continue
        payload = event.payload
        try:
            record = RecordedOperatorCommand(
                command_id=payload.command_id,
                command_type=str(payload.command_type),
                command_digest=str(payload.command_digest),
                mode_path=tuple(str(mode) for mode in payload.mode_path),
                requested_at=payload.requested_at,
                operator_label=str(payload.operator_label),
                operator_identity=(
                    None if payload.operator_identity is None else str(payload.operator_identity)
                ),
                reason=str(payload.reason),
                source=str(payload.source),
                previous_safety_state=str(payload.previous_safety_state),
                resulting_safety_state=str(payload.resulting_safety_state),
                disposition=str(payload.disposition),
                reason_code=str(payload.reason_code),
                venue_control=str(payload.venue_control),
                reconciliation_status=str(payload.reconciliation_status),
                epoch_opened=bool(payload.epoch_opened),
                epoch_closed=bool(payload.epoch_closed),
                fence_term=int(payload.fence_term),
                event_id=event.event_id,
                correlation_id=event.correlation_id,
                causation_id=event.causation_id,
            )
        except (AttributeError, TypeError, ValueError) as exc:
            raise BreakGlassStateError(
                f"a journaled operator command record is not usable: {exc}"
            ) from exc
        existing = commands.get(record.command_id)
        if existing is not None and existing.command_digest != record.command_digest:
            raise BreakGlassStateError(
                "the journal contains two different commands sharing one command_id; refusing to "
                "guess which one is authoritative."
            )
        commands.setdefault(record.command_id, record)
        epoch = _reduce_epoch(epoch, record)
    return BreakGlassLedger(epoch=epoch, commands=commands)


def _reduce_epoch(
    epoch: BreakGlassEpoch | None, record: RecordedOperatorCommand
) -> BreakGlassEpoch | None:
    """Apply one recorded command to the epoch (deterministic, order-dependent)."""
    if record.epoch_opened:
        path = _merge_path(("normal",), record.mode_path)
        epoch = BreakGlassEpoch(
            session_id=record.correlation_id,
            opened_at=record.requested_at,
            opened_by_command_id=record.command_id,
            opened_by=record.operator_label,
            opened_by_reason_code=record.reason_code,
            emergency_owner=record.operator_label,
            fence_term=record.fence_term,
            actions=(record.command_type,),
            escalation_path=path,
            reconciliation_status="pending",
            reconciliation_reason_code="reconciliation_required",
            provenance="journal_replay",
        )
        if record.reconciliation_status in ("resolved", "failed"):
            epoch = epoch.with_updates(
                reconciliation_status=record.reconciliation_status,
                reconciliation_reason_code=record.reason_code,
                reconciliation_checked_at=record.requested_at,
            )
        return epoch

    if epoch is None:
        return None

    actions = (*epoch.actions, record.command_type)
    path = _merge_path(epoch.escalation_path, record.mode_path)
    changes: dict[str, Any] = {
        "actions": actions,
        "escalation_path": path,
        "fence_term": max(epoch.fence_term, record.fence_term),
    }
    if record.accepted and record.command_type == "reconcile":
        changes["reconciliation_status"] = record.reconciliation_status
        changes["reconciliation_reason_code"] = record.reason_code
        changes["reconciliation_checked_at"] = record.requested_at
    if record.accepted and record.command_type == "acknowledge":
        changes["acknowledgement"] = OperatorAcknowledgement(
            operator_label=record.operator_label,
            operator_identity=record.operator_identity,
            acknowledged_at=record.requested_at,
            command_id=record.command_id,
            reconciliation_reference=epoch.reconciliation_reason_code,
        )
    if record.epoch_closed:
        changes["closed_at"] = record.requested_at
        changes["closed_by_command_id"] = record.command_id
    return epoch.with_updates(**changes)


def _merge_path(path: tuple[str, ...], modes: tuple[str, ...]) -> tuple[str, ...]:
    """Append the modes a command traversed, collapsing consecutive repeats."""
    merged = list(path)
    for mode in modes:
        if mode and (not merged or merged[-1] != mode):
            merged.append(mode)
    return tuple(merged)


def synthesise_epoch_from_safety_state(
    controller_mode: SafetyMode,
    *,
    now: datetime,
    operator_label: str,
) -> BreakGlassEpoch | None:
    """Build a fail-closed epoch when the safety state is post-break-glass without provenance."""
    if controller_mode not in (SafetyMode.DEGRADED, SafetyMode.RECOVERY_REQUIRED):
        return None
    return BreakGlassEpoch(
        session_id=uuid4(),
        opened_at=now,
        opened_by_command_id=uuid4(),
        opened_by=operator_label,
        opened_by_reason_code="breakglass_epoch_provenance_missing",
        emergency_owner="unknown",
        fence_term=0,
        actions=(),
        escalation_path=("normal", controller_mode.value),
        reconciliation_status="pending",
        reconciliation_reason_code="breakglass_epoch_provenance_missing",
        provenance="provenance_missing",
    )
