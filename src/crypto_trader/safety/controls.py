"""Deterministic operational safety controls (Phase 9).

The controller is a small deterministic state machine: the same starting state plus the same
request always produces the same transition. It never reads the wall clock, never performs I/O
and never resumes trading on its own.

Guarantees:

* the kill switch is deterministic, observable, restart-persistent and fail-closed;
* an unreadable/corrupt/unknown persisted state restores into ``recovery_required`` with the kill
  switch engaged - it never restores into ``normal``;
* repeated identical requests are idempotent no-ops (``changed is False``), so re-delivering a
  halt/cancel action never produces a second effect;
* a break-glass use forces ``degraded`` then ``recovery_required``; new trading stays prohibited
  until an explicit operator transition and reconciliation.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

from .errors import SafetyConfigError, SafetyStateError
from .modes import SafetyMode

__all__ = [
    "SAFETY_STATE_SCHEMA_VERSION",
    "SafetyController",
    "SafetyTransition",
    "restore_safety_controller",
]

#: Schema version of the persisted safety state (not of any event).
SAFETY_STATE_SCHEMA_VERSION = 1

_TRIGGERS = frozenset({"operator", "risk", "recovery", "break_glass", "system"})
_REQUIRED_STATE_KEYS = frozenset(
    {"schema_version", "mode", "kill_switch_engaged", "reason_code", "reason", "sequence",
     "session_id"}
)


@dataclass(frozen=True)
class SafetyTransition:
    """One requested/realized safety-mode transition."""

    correlation_id: UUID
    mode: SafetyMode
    previous_mode: SafetyMode
    reason_code: str
    reason: str
    trigger: str
    kill_switch_engaged: bool
    sequence: int
    changed: bool

    @property
    def is_escalation(self) -> bool:
        return self.mode.severity > self.previous_mode.severity

    @property
    def is_de_escalation(self) -> bool:
        return self.mode.severity < self.previous_mode.severity

    def to_record(self) -> dict[str, Any]:
        """A JSON-able observability record (contains no secrets)."""
        return {
            "correlation_id": str(self.correlation_id),
            "mode": self.mode.value,
            "previous_mode": self.previous_mode.value,
            "reason_code": self.reason_code,
            "reason": self.reason,
            "trigger": self.trigger,
            "kill_switch_engaged": self.kill_switch_engaged,
            "sequence": self.sequence,
            "changed": self.changed,
            "allow_new_risk": self.mode.allows_new_risk,
            "requires_cancel_open": self.mode.requires_cancel_open,
        }


@dataclass
class SafetyController:
    """Deterministic safety-mode state machine with restart persistence."""

    correlation_id: UUID = field(default_factory=uuid4)
    _mode: SafetyMode = SafetyMode.NORMAL
    _kill_switch_engaged: bool = False
    _reason_code: str = "session_started"
    _reason: str = "safety controller initialized in normal mode"
    _sequence: int = 0
    _history: list[SafetyTransition] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not isinstance(self._mode, SafetyMode):
            raise SafetyConfigError("mode must be a SafetyMode.")
        if not self._reason_code or not self._reason:
            raise SafetyConfigError("a safety state requires a reason code and reason.")

    # --- reads ---

    @property
    def mode(self) -> SafetyMode:
        return self._mode

    @property
    def kill_switch_engaged(self) -> bool:
        return self._kill_switch_engaged

    @property
    def reason_code(self) -> str:
        return self._reason_code

    @property
    def reason(self) -> str:
        return self._reason

    @property
    def sequence(self) -> int:
        return self._sequence

    @property
    def history(self) -> tuple[SafetyTransition, ...]:
        return tuple(self._history)

    def allow_new_risk(self) -> bool:
        """Whether the AI/strategy path may create new exposure right now."""
        return self._mode.allows_new_risk and not self._kill_switch_engaged

    def allow_exposure_increase(self) -> bool:
        return self._mode.allows_exposure_increase and not self._kill_switch_engaged

    def requires_cancel_open(self) -> bool:
        return self._mode.requires_cancel_open or self._kill_switch_engaged

    def permits_live_execution(self) -> bool:
        return self._mode.permits_live_execution

    # --- transitions ---

    def set_mode(
        self,
        mode: SafetyMode,
        *,
        reason_code: str,
        reason: str,
        trigger: str = "operator",
    ) -> SafetyTransition:
        """Request a mode change. Idempotent: re-requesting the current state is a no-op."""
        if not isinstance(mode, SafetyMode):
            raise SafetyStateError("mode must be a SafetyMode.")
        if trigger not in _TRIGGERS:
            raise SafetyStateError(f"unknown safety trigger {trigger!r}.")
        if not isinstance(reason_code, str) or not reason_code.strip():
            raise SafetyStateError("a safety transition requires a reason code.")
        if not isinstance(reason, str) or not reason.strip():
            raise SafetyStateError("a safety transition requires a reason.")

        changed = mode is not self._mode
        previous = self._mode
        if changed:
            self._mode = mode
            self._sequence += 1
        self._reason_code = reason_code
        self._reason = reason
        transition = SafetyTransition(
            correlation_id=self.correlation_id,
            mode=self._mode,
            previous_mode=previous,
            reason_code=reason_code,
            reason=reason,
            trigger=trigger,
            kill_switch_engaged=self._kill_switch_engaged,
            sequence=self._sequence,
            changed=changed,
        )
        self._history.append(transition)
        return transition

    def engage_kill_switch(
        self,
        *,
        reason_code: str,
        reason: str,
        trigger: str = "operator",
    ) -> SafetyTransition:
        """Engage the kill switch and halt new risk. Idempotent while already engaged."""
        changed = not self._kill_switch_engaged or self._mode is not SafetyMode.HALT_NEW_RISK
        previous = self._mode
        self._kill_switch_engaged = True
        self._mode = SafetyMode.HALT_NEW_RISK
        if changed:
            self._sequence += 1
        self._reason_code = reason_code
        self._reason = reason
        transition = SafetyTransition(
            correlation_id=self.correlation_id,
            mode=self._mode,
            previous_mode=previous,
            reason_code=reason_code,
            reason=reason,
            trigger=trigger,
            kill_switch_engaged=True,
            sequence=self._sequence,
            changed=changed,
        )
        self._history.append(transition)
        return transition

    def release_kill_switch(self, *, reason_code: str, reason: str) -> SafetyTransition:
        """Explicitly release the kill switch. Never happens automatically."""
        changed = self._kill_switch_engaged
        previous = self._mode
        self._kill_switch_engaged = False
        if changed:
            self._sequence += 1
        self._reason_code = reason_code
        self._reason = reason
        transition = SafetyTransition(
            correlation_id=self.correlation_id,
            mode=self._mode,
            previous_mode=previous,
            reason_code=reason_code,
            reason=reason,
            trigger="operator",
            kill_switch_engaged=False,
            sequence=self._sequence,
            changed=changed,
        )
        self._history.append(transition)
        return transition

    def engage_break_glass(
        self,
        *,
        reason_code: str,
        reason: str,
    ) -> tuple[SafetyTransition, ...]:
        """Force the post-break-glass posture: ``degraded`` then ``recovery_required``.

        Returns every transition that actually changed state. New trading stays prohibited until
        reconciliation completes and an operator explicitly returns the system to ``normal``.
        """
        if self._mode is SafetyMode.RECOVERY_REQUIRED and self._kill_switch_engaged:
            # Already in the post-break-glass posture: idempotent no-op, no downgrade.
            return ()
        self._kill_switch_engaged = True
        degraded: SafetyTransition | None = None
        if self._mode is not SafetyMode.DEGRADED:
            degraded = self.set_mode(
                SafetyMode.DEGRADED,
                reason_code=f"break_glass:{reason_code}",
                reason=f"break-glass engaged: {reason}",
                trigger="break_glass",
            )
        recovery = self.set_mode(
            SafetyMode.RECOVERY_REQUIRED,
            reason_code=f"break_glass_recovery:{reason_code}",
            reason=f"break-glass requires reconciliation: {reason}",
            trigger="break_glass",
        )
        return tuple(item for item in (degraded, recovery) if item.changed)

    # --- persistence ---

    def to_persisted(self) -> dict[str, Any]:
        """Serialize the safety state for durable storage (plain JSON primitives)."""
        return {
            "schema_version": SAFETY_STATE_SCHEMA_VERSION,
            "mode": self._mode.value,
            "kill_switch_engaged": self._kill_switch_engaged,
            "reason_code": self._reason_code,
            "reason": self._reason,
            "sequence": self._sequence,
            "session_id": str(self.correlation_id),
        }


def restore_safety_controller(persisted: Mapping[str, Any] | None) -> SafetyController:
    """Restore a controller from persisted state, failing CLOSED.

    A missing, incomplete, unreadable or unknown-version record restores into
    ``recovery_required`` with the kill switch engaged. It never restores into ``normal``.
    """
    if persisted is None:
        return SafetyController(
            correlation_id=uuid4(),
            _mode=SafetyMode.RECOVERY_REQUIRED,
            _kill_switch_engaged=True,
            _reason_code="safety_state_missing",
            _reason="no persisted safety state found; starting fail-closed",
        )
    if not isinstance(persisted, Mapping):
        return _fail_closed_controller(
            uuid4(), "safety_state_unreadable", "persisted safety state is not a mapping"
        )

    try:
        keys = set(persisted)
        if keys != _REQUIRED_STATE_KEYS:
            raise ValueError(f"unexpected persisted safety state keys: {sorted(keys)}")
        if int(persisted["schema_version"]) != SAFETY_STATE_SCHEMA_VERSION:
            raise ValueError("unsupported safety state schema version")
        mode = SafetyMode(str(persisted["mode"]))
        kill_switch = persisted["kill_switch_engaged"]
        if not isinstance(kill_switch, bool):
            raise ValueError("kill_switch_engaged must be a boolean")
        reason_code = str(persisted["reason_code"])
        reason = str(persisted["reason"])
        sequence = int(persisted["sequence"])
        session_id = UUID(str(persisted["session_id"]))
        if sequence < 0 or not reason_code or not reason:
            raise ValueError("invalid safety state values")
    except (ValueError, KeyError, TypeError) as exc:
        return _fail_closed_controller(
            uuid4(), "safety_state_corrupt", f"persisted safety state rejected: {exc}"
        )

    # Fail closed on an internally inconsistent record: recovery/degraded can never be reached
    # implicitly, but they must never be silently dropped either.
    if mode in {SafetyMode.DEGRADED, SafetyMode.RECOVERY_REQUIRED} and not kill_switch:
        return _fail_closed_controller(
            session_id,
            "safety_state_inconsistent",
            "post-break-glass mode without the kill switch engaged",
        )

    return SafetyController(
        correlation_id=session_id,
        _mode=mode,
        _kill_switch_engaged=kill_switch,
        _reason_code=reason_code,
        _reason=reason,
        _sequence=sequence,
    )


def _fail_closed_controller(session_id: UUID, reason_code: str, reason: str) -> SafetyController:
    return SafetyController(
        correlation_id=session_id,
        _mode=SafetyMode.RECOVERY_REQUIRED,
        _kill_switch_engaged=True,
        _reason_code=reason_code,
        _reason=reason,
    )
