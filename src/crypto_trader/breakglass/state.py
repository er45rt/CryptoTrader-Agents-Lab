"""Restart persistence for the operational safety state (Phase 13).

The Phase 9 :class:`SafetyController` is the only safety state; this module gives it a durable
local home and, crucially, a *journal-derived* fallback:

* the state file is a cache written after the journal append, so a crash between the two is
  harmless - the journal is authoritative and the state is rebuilt from it;
* a state file that disagrees with the journal, or that cannot be read, fails CLOSED into
  ``recovery_required`` with the kill switch engaged - never into ``normal`` and never silently
  "repaired";
* a genuinely new session (no state file, no journaled safety event) starts ``normal``; that is the
  only path that produces ``normal`` without an operator restore.
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
from uuid import UUID, uuid4

from crypto_trader.events import SAFETY_EVENT_TYPE
from crypto_trader.persistence import EventJournal
from crypto_trader.safety import SafetyController, SafetyMode, restore_safety_controller

from .errors import BreakGlassStateError

__all__ = ["SafetyStateLoad", "SafetyStateStore", "replay_safety_state"]


@dataclass(frozen=True)
class SafetyStateLoad:
    """Where the restored safety state came from, and whether it was forced fail-closed."""

    controller: SafetyController
    source: Literal["state_file", "journal_replay", "fresh_session", "fail_closed"]
    detail: str
    forced_fail_closed: bool = False


class SafetyStateStore:
    """Durable safety-state cache backed by one atomically written JSON file."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)

    @property
    def path(self) -> Path:
        return self._path

    def exists(self) -> bool:
        return self._path.exists()

    def save(self, controller: SafetyController) -> None:
        if not isinstance(controller, SafetyController):
            raise BreakGlassStateError("a SafetyController is required.")
        payload = json.dumps(controller.to_persisted(), indent=2, sort_keys=True)
        self._write(payload)

    def load(self, journal: EventJournal) -> SafetyStateLoad:
        """Restore the safety state, preferring the cache and falling back to the journal."""
        raw_state = self._read_raw()
        journal_state = replay_safety_state(journal)

        if raw_state is _UNREADABLE:
            controller = restore_safety_controller("unreadable safety state file")
            return SafetyStateLoad(
                controller=controller,
                source="fail_closed",
                detail="the persisted safety state could not be read; restored fail-closed",
                forced_fail_closed=True,
            )

        if raw_state is not None:
            controller = restore_safety_controller(raw_state)
            inconsistency = _inconsistency(controller, journal_state)
            if inconsistency is not None:
                forced = restore_safety_controller({"unexpected_state_file": inconsistency})
                return SafetyStateLoad(
                    controller=forced,
                    source="fail_closed",
                    detail=inconsistency,
                    forced_fail_closed=True,
                )
            return SafetyStateLoad(
                controller=controller,
                source="state_file",
                detail="safety state restored from the persisted cache and cross-checked against "
                "the journal",
            )

        if journal_state is not None:
            return SafetyStateLoad(
                controller=journal_state,
                source="journal_replay",
                detail="the safety-state cache was absent; the state was rebuilt from the "
                "authoritative journal",
            )

        return SafetyStateLoad(
            controller=SafetyController(correlation_id=uuid4()),
            source="fresh_session",
            detail="no persisted safety state and no journaled safety event: a new session starts "
            "in normal mode",
        )

    def _read_raw(self) -> Mapping[str, Any] | None | object:
        if not self._path.exists():
            return None
        try:
            payload = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return _UNREADABLE
        if not isinstance(payload, Mapping):
            return _UNREADABLE
        return payload

    def _write(self, payload: str) -> None:
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
            raise BreakGlassStateError(f"the safety state could not be written: {exc}") from exc
        finally:
            if os.path.exists(temporary):  # pragma: no cover - only on a failed replace
                os.unlink(temporary)


class _Unreadable:
    """Sentinel distinguishing "no state file" from "state file present but unusable"."""


_UNREADABLE = _Unreadable()


def replay_safety_state(journal: EventJournal) -> SafetyController | None:
    """Rebuild the safety state from the authoritative journal (``None`` when nothing is recorded).

    Replay is deterministic and idempotent: the same journal always produces the same mode, kill
    switch and reason, so a restart with a lost state cache restores exactly what the journal says.
    """
    controller: SafetyController | None = None
    sequence = 0
    session_id: UUID | None = None
    for event in journal.replay():
        if event.event_type != SAFETY_EVENT_TYPE:
            continue
        payload = event.payload
        try:
            mode = SafetyMode(payload.mode)
            kill_switch = bool(payload.kill_switch_engaged)
            reason_code = str(payload.reason_code)
            reason = str(payload.reason)
        except (AttributeError, ValueError) as exc:
            raise BreakGlassStateError(
                f"a journaled safety state change is not usable: {exc}"
            ) from exc
        sequence += 1
        session_id = event.correlation_id
        controller = SafetyController(
            correlation_id=session_id,
            _mode=mode,
            _kill_switch_engaged=kill_switch,
            _reason_code=reason_code,
            _reason=reason,
            _sequence=sequence,
        )
    return controller


def _inconsistency(
    controller: SafetyController, journal_state: SafetyController | None
) -> str | None:
    """Return a fail-closed reason when the cache contradicts the authoritative journal."""
    if journal_state is None:
        return None
    if controller.mode is not journal_state.mode:
        return (
            f"safety state cache says {controller.mode.value} but the journal says "
            f"{journal_state.mode.value}"
        )
    if controller.kill_switch_engaged != journal_state.kill_switch_engaged:
        return (
            "safety state cache and journal disagree about the kill switch "
            f"(cache={controller.kill_switch_engaged}, journal={journal_state.kill_switch_engaged})"
        )
    return None
