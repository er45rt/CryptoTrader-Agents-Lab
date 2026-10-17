"""Typed errors for the operational break-glass control path (Phase 13).

Every failure mode is explicit and fail-closed: a malformed command, a corrupt persisted state, a
stale writer fence or an unconfirmed venue control never degrades into a silent success.
"""

from __future__ import annotations

__all__ = [
    "BreakGlassActiveError",
    "BreakGlassCommandError",
    "BreakGlassConfigError",
    "BreakGlassError",
    "BreakGlassIncompleteError",
    "BreakGlassRefusedError",
    "BreakGlassStateError",
    "MalformedOperatorCommandError",
    "OwnershipStateError",
    "StaleWriterFenceError",
    "VenueControlError",
]


class BreakGlassError(RuntimeError):
    """Base class for every operational break-glass failure."""


class BreakGlassConfigError(BreakGlassError):
    """Mandatory configuration is missing or invalid (fail closed)."""


class BreakGlassCommandError(BreakGlassError):
    """An operator command is not usable as delivered."""


class MalformedOperatorCommandError(BreakGlassCommandError):
    """The command payload is malformed, unknown or carries credential-shaped text."""


class BreakGlassIncompleteError(BreakGlassError):
    """A command failed after its first side effect: the venue cancel, the lease takeover or the
    journal record may already be in effect, so the failure must never read as "nothing changed"."""


class BreakGlassRefusedError(BreakGlassError):
    """The command was understood but refused by the deterministic safety policy."""


class BreakGlassActiveError(BreakGlassError):
    """Runtime/AI-sourced control was refused because a break-glass epoch is open."""


class BreakGlassStateError(BreakGlassError):
    """Persisted break-glass state is missing, corrupt or internally inconsistent."""


class OwnershipStateError(BreakGlassError):
    """The writer-ownership record is missing or corrupt (fail closed: no writer may trade)."""


class StaleWriterFenceError(OwnershipStateError):
    """A writer presented a fencing token that has been superseded by an emergency takeover."""


class VenueControlError(BreakGlassError):
    """Venue-side emergency control could not be completed deterministically."""
