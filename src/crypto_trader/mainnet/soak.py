"""Durable soak state for the mainnet read-only dry-run (Phase 15).

The soak counters are the durable evidence for the exit criteria: they survive a restart and are
written atomically. A restart performed deliberately for the recovery scenario is allowed and
recorded as a planned restart; an unexpected crash or a bug that needs corrective intervention
resets the continuous-soak qualification window.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from .errors import MainnetConfigError, MainnetError

__all__ = [
    "MIN_CONTINUOUS_SECONDS",
    "MIN_PIPELINE_CYCLES",
    "SOAK_SCHEMA_VERSION",
    "SoakCounters",
    "SoakRecorder",
    "SoakState",
    "SoakStateStore",
    "evaluate_exit_criteria",
]

SOAK_SCHEMA_VERSION = 1

#: Criterion 1: at least 72 continuous hours of dry-run operation.
MIN_CONTINUOUS_SECONDS = 72 * 60 * 60

#: Criterion 2: at least 50 complete pipeline cycles.
MIN_PIPELINE_CYCLES = 50


@dataclass(frozen=True)
class SoakCounters:
    """The durable counters reported by the soak."""

    completed_pipeline_cycles: int = 0
    validity_rejections: int = 0
    halt_exercises: int = 0
    reconciliation_exercises: int = 0
    restart_recovery_exercises: int = 0
    stale_fence_rejections: int = 0
    ambiguity_fault_injections: int = 0
    secret_leaks: int = 0
    mainnet_mutation_attempts: int = 0
    manual_corrective_interventions: int = 0

    def with_updates(self, **changes: int) -> "SoakCounters":
        unknown = set(changes) - set(self.to_record())
        if unknown:
            raise MainnetConfigError(f"unknown soak counters: {sorted(unknown)}")
        for name, value in changes.items():
            if not isinstance(value, int) or value < 0:
                raise MainnetConfigError(f"soak counter {name!r} must be a non-negative integer.")
        return replace(self, **changes)

    def to_record(self) -> dict[str, int]:
        return {
            "completed_pipeline_cycles": self.completed_pipeline_cycles,
            "validity_rejections": self.validity_rejections,
            "halt_exercises": self.halt_exercises,
            "reconciliation_exercises": self.reconciliation_exercises,
            "restart_recovery_exercises": self.restart_recovery_exercises,
            "stale_fence_rejections": self.stale_fence_rejections,
            "ambiguity_fault_injections": self.ambiguity_fault_injections,
            "secret_leaks": self.secret_leaks,
            "mainnet_mutation_attempts": self.mainnet_mutation_attempts,
            "manual_corrective_interventions": self.manual_corrective_interventions,
        }

    @classmethod
    def from_record(cls, payload: Any) -> "SoakCounters":
        if not isinstance(payload, dict):
            raise MainnetError("the persisted soak counters are not a JSON object.")
        known = cls()
        values: dict[str, int] = {}
        for name in known.to_record():
            if name not in payload:
                raise MainnetError(f"persisted soak counter {name!r} is missing.")
            raw = payload[name]
            if not isinstance(raw, int) or isinstance(raw, bool) or raw < 0:
                raise MainnetError(f"persisted soak counter {name!r} is not a non-negative integer.")
            values[name] = raw
        return cls(**values)


@dataclass(frozen=True)
class SoakState:
    """The complete durable soak state."""

    soak_start_time: datetime
    last_heartbeat: datetime | None = None
    planned_restarts: int = 0
    journal_deterministic: bool = False
    counters: SoakCounters = field(default_factory=SoakCounters)
    schema_version: int = SOAK_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != SOAK_SCHEMA_VERSION:
            raise MainnetError(
                f"unsupported soak state schema {self.schema_version}; refusing to guess."
            )
        for name in ("soak_start_time", "last_heartbeat"):
            value = getattr(self, name)
            if value is not None and (value.tzinfo is None or value.utcoffset() is None):
                raise MainnetError(f"{name} must be timezone-aware.")
        if self.planned_restarts < 0:
            raise MainnetError("planned_restarts must be non-negative.")

    def continuous_runtime_seconds(self, now: datetime) -> float:
        return max(0.0, (now - self.soak_start_time).total_seconds())

    def to_record(self, now: datetime | None = None) -> dict[str, object]:
        stamp = now or self.last_heartbeat or self.soak_start_time
        return {
            "schema_version": self.schema_version,
            "soak_start_time": self.soak_start_time.astimezone(timezone.utc).isoformat(),
            "last_heartbeat": (
                None
                if self.last_heartbeat is None
                else self.last_heartbeat.astimezone(timezone.utc).isoformat()
            ),
            "continuous_runtime": self.continuous_runtime_seconds(stamp),
            "planned_restarts": self.planned_restarts,
            "journal_deterministic": self.journal_deterministic,
            **self.counters.to_record(),
        }

    @classmethod
    def from_record(cls, payload: Any) -> "SoakState":
        if not isinstance(payload, dict):
            raise MainnetError("the persisted soak state is not a JSON object.")
        try:
            for name in ("schema_version", "planned_restarts"):
                if type(payload[name]) is not int:
                    raise ValueError(f"{name} must be an integer")
            if not isinstance(payload["journal_deterministic"], bool):
                raise ValueError("journal_deterministic must be a boolean")
            return cls(
                schema_version=payload["schema_version"],
                soak_start_time=datetime.fromisoformat(str(payload["soak_start_time"])),
                last_heartbeat=(
                    None
                    if payload.get("last_heartbeat") is None
                    else datetime.fromisoformat(str(payload["last_heartbeat"]))
                ),
                planned_restarts=payload["planned_restarts"],
                journal_deterministic=payload["journal_deterministic"],
                counters=SoakCounters.from_record(payload),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise MainnetError(f"the persisted soak state is not usable: {exc}") from exc


class SoakStateStore:
    """Durable, atomically written soak state."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        if not self._path.name:
            raise MainnetConfigError("the soak state store requires an explicit file path.")

    @property
    def path(self) -> Path:
        return self._path

    def exists(self) -> bool:
        return self._path.exists()

    def load(self) -> SoakState | None:
        if not self._path.exists():
            return None
        try:
            payload = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise MainnetError(f"the soak state could not be read: {exc}") from exc
        return SoakState.from_record(payload)

    def save(self, state: SoakState) -> None:
        payload = json.dumps(state.to_record(), indent=2, sort_keys=True)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        handle, temporary = tempfile.mkstemp(
            prefix=self._path.name, suffix=".tmp", dir=str(self._path.parent)
        )
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self._path)
        except OSError as exc:
            raise MainnetError(f"the soak state could not be written: {exc}") from exc
        finally:
            if os.path.exists(temporary):  # pragma: no cover - only on a failed replace
                os.unlink(temporary)


def evaluate_exit_criteria(
    state: SoakState, now: datetime
) -> tuple[bool, tuple[str, ...]]:
    """Evaluate the 12 dry-run exit criteria mechanically and return the unmet ones."""
    counters = state.counters
    unmet: list[str] = []
    if state.continuous_runtime_seconds(now) < MIN_CONTINUOUS_SECONDS:
        unmet.append("continuous_runtime_below_72h")
    if counters.completed_pipeline_cycles < MIN_PIPELINE_CYCLES:
        unmet.append("completed_pipeline_cycles_below_50")
    if counters.validity_rejections < 1:
        unmet.append("validity_rejection_not_observed")
    if counters.halt_exercises < 1:
        unmet.append("halt_new_risk_not_exercised")
    if counters.reconciliation_exercises < 1:
        unmet.append("reconciliation_not_exercised")
    if counters.restart_recovery_exercises < 1:
        unmet.append("restart_recovery_not_exercised")
    if counters.stale_fence_rejections < 1:
        unmet.append("stale_fence_rejection_not_exercised")
    if counters.ambiguity_fault_injections < 1:
        unmet.append("post_send_ambiguity_not_exercised")
    if counters.secret_leaks != 0:
        unmet.append("secret_leak_observed")
    if counters.mainnet_mutation_attempts != 0:
        unmet.append("mainnet_mutation_attempt_observed")
    if counters.manual_corrective_interventions != 0:
        unmet.append("manual_corrective_intervention_required")
    if not state.journal_deterministic:
        unmet.append("journal_replay_determinism_not_confirmed")
    return (not unmet, tuple(unmet))


class SoakRecorder:
    """Read-modify-write the durable soak state; every mutation is persisted immediately."""

    def __init__(self, store: SoakStateStore, *, clock) -> None:
        if not callable(clock):
            raise MainnetConfigError("the soak recorder requires a clock.")
        self._store = store
        self._clock = clock

    def _now(self) -> datetime:
        now = self._clock()
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
            raise MainnetConfigError("the soak clock must return timezone-aware time.")
        return now.astimezone(timezone.utc)

    def state(self) -> SoakState:
        """Load the durable state, creating a fresh one on first use (survives restart)."""
        existing = self._store.load()
        return existing if existing is not None else SoakState(soak_start_time=self._now())

    def _commit(self, state: SoakState) -> SoakState:
        updated = replace(state, last_heartbeat=self._now())
        self._store.save(updated)
        return updated

    def bump(self, name: str, *, amount: int = 1) -> SoakState:
        state = self.state()
        counters = state.counters
        current = state.counters.to_record()[name]
        return self._commit(replace(state, counters=counters.with_updates(**{name: current + amount})))

    # --- the reported events -------------------------------------------------

    def record_cycle(self, *, completed: bool, validity_rejected: bool = False) -> SoakState:
        state = self.state()
        counters = state.counters.with_updates(
            completed_pipeline_cycles=(
                state.counters.completed_pipeline_cycles + (1 if completed else 0)
            ),
            validity_rejections=(
                state.counters.validity_rejections + (1 if validity_rejected else 0)
            ),
        )
        return self._commit(replace(state, counters=counters))

    def record_halt_exercise(self) -> SoakState:
        return self.bump("halt_exercises")

    def record_reconciliation_exercise(self) -> SoakState:
        return self.bump("reconciliation_exercises")

    def record_restart_recovery_exercise(self) -> SoakState:
        return self.bump("restart_recovery_exercises")

    def record_stale_fence_rejection(self) -> SoakState:
        return self.bump("stale_fence_rejections")

    def record_ambiguity_fault_injection(self) -> SoakState:
        return self.bump("ambiguity_fault_injections")

    def record_secret_leak(self) -> SoakState:
        return self.bump("secret_leaks")

    def record_mutation_attempt(self) -> SoakState:
        return self.bump("mainnet_mutation_attempts")

    def note_planned_restart(self) -> SoakState:
        state = self.state()
        return self._commit(
            replace(state, planned_restarts=state.planned_restarts + 1)
        )

    def note_manual_intervention(self) -> SoakState:
        """A corrective intervention resets the continuous-soak qualification window."""
        state = self.state()
        counters = state.counters.with_updates(
            manual_corrective_interventions=(
                state.counters.manual_corrective_interventions + 1
            )
        )
        return self._commit(
            replace(state, soak_start_time=self._now(), counters=counters)
        )

    def resume(self, *, planned: bool) -> SoakState:
        """Resume after a restart; an unplanned one resets the continuous window."""
        if planned:
            self.note_planned_restart()
            return self.bump("restart_recovery_exercises")
        return self.note_manual_intervention()

    def set_journal_deterministic(self, value: bool) -> SoakState:
        state = self.state()
        return self._commit(replace(state, journal_deterministic=bool(value)))

    def snapshot(self) -> dict[str, object]:
        state = self.state()
        met, unmet = evaluate_exit_criteria(state, self._now())
        record = state.to_record(self._now())
        record["exit_criteria_met"] = met
        record["exit_criteria_unmet"] = list(unmet)
        return record

    def exit_criteria(self) -> tuple[bool, tuple[str, ...]]:
        return evaluate_exit_criteria(self.state(), self._now())
