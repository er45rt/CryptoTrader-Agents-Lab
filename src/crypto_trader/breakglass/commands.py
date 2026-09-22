"""Operator break-glass commands: deterministic parsing, validation and fingerprinting (Phase 13).

The control channel is a narrow operator surface. Nothing in this module (or anywhere else in this
package) accepts an LLM/agent-produced decision: a command is an explicitly typed operator request
with an operator label, a reason, a control-channel identifier and an idempotency key.

Rules enforced here:

* an unknown/malformed/blank command is refused deterministically - never coerced to a default;
* credential-shaped free text is refused, so a credential cannot be written into the journal;
* ``command_digest`` covers the command's *semantic* content (type, operator, reason, source,
  correlation) and deliberately excludes the delivery timestamp, so re-delivering the same
  ``command_id`` later still matches (one logical effect) while a different request reusing the
  same id is a conflict.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any
from uuid import UUID

from crypto_trader.safety import SafetyMode

from .errors import MalformedOperatorCommandError

__all__ = [
    "ACTION_TARGET_MODES",
    "BREAK_GLASS_ACTIONS",
    "LIFECYCLE_COMMANDS",
    "OperatorCommand",
    "OperatorCommandType",
    "command_digest",
    "parse_operator_command",
]


class OperatorCommandType(str, Enum):
    """The complete operational break-glass command vocabulary (mirrors the event contract)."""

    HALT_NEW_RISK = "halt_new_risk"
    CANCEL_OPEN = "cancel_open"
    REDUCE_ONLY = "reduce_only"
    ENTER_DEGRADED = "enter_degraded"
    FORCE_RECOVERY_REQUIRED = "force_recovery_required"
    RECONCILE = "reconcile"
    ACKNOWLEDGE = "acknowledge"
    RESTORE_NORMAL = "restore_normal"

    @property
    def is_break_glass_action(self) -> bool:
        """True for the five operational actions; False for the lifecycle commands."""
        return self in BREAK_GLASS_ACTIONS


#: The five operational break-glass actions this phase supports. There is deliberately NO
#: liquidation action: the break-glass path never market-sells a portfolio.
BREAK_GLASS_ACTIONS = frozenset(
    {
        OperatorCommandType.HALT_NEW_RISK,
        OperatorCommandType.CANCEL_OPEN,
        OperatorCommandType.REDUCE_ONLY,
        OperatorCommandType.ENTER_DEGRADED,
        OperatorCommandType.FORCE_RECOVERY_REQUIRED,
    }
)

#: The lifecycle commands that gate any return to ``normal``.
LIFECYCLE_COMMANDS = frozenset(
    {
        OperatorCommandType.RECONCILE,
        OperatorCommandType.ACKNOWLEDGE,
        OperatorCommandType.RESTORE_NORMAL,
    }
)

#: The explicit target safety mode of each break-glass action. ``REDUCE_ONLY`` is a deterministic
#: policy mode (new exposure is impossible); it is NOT an exchange-native flag, and no
#: exchange-native reduce-only flag is invented for Binance Spot.
ACTION_TARGET_MODES: dict[OperatorCommandType, SafetyMode] = {
    OperatorCommandType.HALT_NEW_RISK: SafetyMode.HALT_NEW_RISK,
    OperatorCommandType.CANCEL_OPEN: SafetyMode.CANCEL_OPEN,
    OperatorCommandType.REDUCE_ONLY: SafetyMode.REDUCE_ONLY,
    OperatorCommandType.ENTER_DEGRADED: SafetyMode.DEGRADED,
    OperatorCommandType.FORCE_RECOVERY_REQUIRED: SafetyMode.RECOVERY_REQUIRED,
}

#: Fragments that must never appear in operator free text. A credential pasted into a reason field
#: would otherwise be journaled verbatim; the check is deterministic and value-free.
_CREDENTIAL_MARKERS = (
    "api_key",
    "api-key",
    "apikey",
    "api_secret",
    "apisecret",
    "secret_key",
    "secretkey",
    "x-mbx",
    "signature=",
    "bearer ",
)

_MAX_TEXT_LENGTH = 512


@dataclass(frozen=True)
class OperatorCommand:
    """One validated operator break-glass command (the unit of audit and idempotency)."""

    command_id: UUID
    command_type: OperatorCommandType
    requested_at: datetime
    operator_label: str
    reason: str
    source: str
    operator_identity: str | None = None
    correlation_id: UUID | None = None
    causation_id: UUID | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.command_id, UUID):
            raise MalformedOperatorCommandError("command_id must be a UUID.")
        if not isinstance(self.command_type, OperatorCommandType):
            raise MalformedOperatorCommandError("command_type must be an OperatorCommandType.")
        for name, value in (
            ("operator_label", self.operator_label),
            ("reason", self.reason),
            ("source", self.source),
        ):
            _require_safe_text(name, value)
        if self.operator_identity is not None:
            _require_safe_text("operator_identity", self.operator_identity)
        if self.requested_at.tzinfo is None or self.requested_at.utcoffset() is None:
            raise MalformedOperatorCommandError("requested_at must be timezone-aware.")
        if self.causation_id is not None and self.causation_id == self.command_id:
            raise MalformedOperatorCommandError("causation_id must not equal command_id.")

    @property
    def digest(self) -> str:
        """The deterministic fingerprint of this command's semantic content."""
        return command_digest(self)

    @property
    def target_mode(self) -> SafetyMode | None:
        """The safety mode a break-glass action requests (``None`` for lifecycle commands)."""
        return ACTION_TARGET_MODES.get(self.command_type)

    def to_record(self) -> dict[str, Any]:
        """A JSON-able, secret-free description of the delivered command."""
        return {
            "command_id": str(self.command_id),
            "command_type": self.command_type.value,
            "requested_at": self.requested_at.astimezone(timezone.utc).isoformat(),
            "operator_label": self.operator_label,
            "operator_identity": self.operator_identity,
            "reason": self.reason,
            "source": self.source,
            "correlation_id": None if self.correlation_id is None else str(self.correlation_id),
            "causation_id": None if self.causation_id is None else str(self.causation_id),
            "command_digest": self.digest,
        }


def command_digest(command: OperatorCommand) -> str:
    """Fingerprint the semantic content of ``command`` (delivery time excluded, by design)."""
    operator = command.operator_identity or command.operator_label
    material = "\u0000".join(
        (
            command.command_type.value,
            operator,
            command.operator_label,
            command.reason.strip(),
            command.source.strip(),
            "" if command.correlation_id is None else str(command.correlation_id),
        )
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def parse_operator_command(
    payload: Mapping[str, Any],
    *,
    delivered_at: datetime,
) -> OperatorCommand:
    """Validate a delivered command mapping, or fail closed with a typed error.

    ``delivered_at`` is used only when the delivery omits ``requested_at``; it never changes the
    command's semantic fingerprint.
    """
    if not isinstance(payload, Mapping):
        raise MalformedOperatorCommandError("an operator command must be a mapping.")

    command_type_raw = payload.get("command_type", payload.get("command"))
    if isinstance(command_type_raw, OperatorCommandType):
        command_type = command_type_raw
    elif isinstance(command_type_raw, str):
        try:
            command_type = OperatorCommandType(
                command_type_raw.strip().lower().replace("-", "_")
            )
        except ValueError as exc:
            raise MalformedOperatorCommandError(
                f"unknown break-glass command type {command_type_raw!r}; refusing to guess."
            ) from exc
    else:
        raise MalformedOperatorCommandError("an operator command requires a command_type.")

    command_id = _require_uuid(payload.get("command_id"), "command_id")
    requested_at = _require_timestamp(payload.get("requested_at"), "requested_at", delivered_at)
    operator_label = _require_text(payload.get("operator_label", payload.get("operator")),
                                   "operator_label")
    reason = _require_text(payload.get("reason"), "reason")
    source = _require_text(payload.get("source"), "source")
    operator_identity = payload.get("operator_identity")
    if operator_identity is not None and not isinstance(operator_identity, str):
        raise MalformedOperatorCommandError("operator_identity must be text when present.")
    correlation_id = (
        None
        if payload.get("correlation_id") is None
        else _require_uuid(payload.get("correlation_id"), "correlation_id")
    )
    causation_id = (
        None
        if payload.get("causation_id") is None
        else _require_uuid(payload.get("causation_id"), "causation_id")
    )

    return OperatorCommand(
        command_id=command_id,
        command_type=command_type,
        requested_at=requested_at,
        operator_label=operator_label,
        operator_identity=operator_identity,
        reason=reason,
        source=source,
        correlation_id=correlation_id,
        causation_id=causation_id,
    )


def _require_uuid(value: Any, name: str) -> UUID:
    if isinstance(value, UUID):
        return value
    if isinstance(value, str):
        try:
            return UUID(value.strip())
        except ValueError as exc:
            raise MalformedOperatorCommandError(f"{name} is not a valid UUID.") from exc
    raise MalformedOperatorCommandError(f"{name} is required and must be a UUID.")


def _require_timestamp(value: Any, name: str, fallback: datetime) -> datetime:
    if value is None:
        if fallback.tzinfo is None or fallback.utcoffset() is None:
            raise MalformedOperatorCommandError("the delivery clock must be timezone-aware.")
        return fallback.astimezone(timezone.utc)
    if isinstance(value, datetime):
        stamp = value
    elif isinstance(value, str):
        text = value.strip().replace("Z", "+00:00")
        try:
            stamp = datetime.fromisoformat(text)
        except ValueError as exc:
            raise MalformedOperatorCommandError(f"{name} is not an ISO-8601 timestamp.") from exc
    else:
        raise MalformedOperatorCommandError(f"{name} must be an ISO-8601 timestamp.")
    if stamp.tzinfo is None or stamp.utcoffset() is None:
        raise MalformedOperatorCommandError(f"{name} must be timezone-aware.")
    return stamp.astimezone(timezone.utc)


def _require_text(value: Any, name: str) -> str:
    if not isinstance(value, str):
        raise MalformedOperatorCommandError(f"{name} is required and must be text.")
    text = value.strip()
    _require_safe_text(name, text)
    return text


def _require_safe_text(name: str, value: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise MalformedOperatorCommandError(f"{name} is required and must not be blank.")
    if len(value) > _MAX_TEXT_LENGTH:
        raise MalformedOperatorCommandError(f"{name} exceeds {_MAX_TEXT_LENGTH} characters.")
    lowered = value.lower()
    for marker in _CREDENTIAL_MARKERS:
        if marker in lowered:
            raise MalformedOperatorCommandError(
                f"{name} looks like it contains credential material; break-glass commands never "
                "carry secrets and nothing secret is ever journaled."
            )

