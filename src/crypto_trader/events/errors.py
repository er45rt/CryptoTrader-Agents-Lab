"""Typed errors for the event contract layer.

These errors are deliberately explicit so that future transport adapters (broker,
file, in-memory) can react to a *category* of failure instead of string matching.
No error message ever contains payload data beyond the offending type/version.
"""

from __future__ import annotations

from typing import Iterable

__all__ = [
    "DuplicateEventRegistrationError",
    "EventContractError",
    "EventValidationError",
    "UnknownEventTypeError",
    "UnsupportedSchemaVersionError",
]


class EventContractError(Exception):
    """Base class for every event-contract failure."""


class EventValidationError(EventContractError):
    """The serialized event is malformed or fails model validation."""

    def __init__(self, message: str, *, details: list[str] | None = None) -> None:
        super().__init__(message)
        self.details = details or []


class UnknownEventTypeError(EventContractError):
    """The event type is not registered in the registry that decoded it."""

    def __init__(self, event_type: str, *, known_types: Iterable[str] = ()) -> None:
        self.event_type = event_type
        self.known_types = tuple(sorted(known_types))
        known = ", ".join(self.known_types) if self.known_types else "<none>"
        super().__init__(f"Unknown event_type '{event_type}'. Registered event types: {known}.")


class UnsupportedSchemaVersionError(EventContractError):
    """The event type is known but the requested schema version is not supported."""

    def __init__(
        self,
        event_type: str,
        schema_version: int,
        *,
        supported_versions: Iterable[int] = (),
    ) -> None:
        self.event_type = event_type
        self.schema_version = schema_version
        self.supported_versions = tuple(sorted(supported_versions))
        supported = ", ".join(str(v) for v in self.supported_versions) or "<none>"
        super().__init__(
            f"Unsupported schema_version {schema_version} for event_type '{event_type}'. "
            f"Supported versions: {supported}. Unknown versions are never parsed as v1."
        )


class DuplicateEventRegistrationError(EventContractError):
    """The same (event_type, schema_version) pair was registered twice."""

    def __init__(self, event_type: str, schema_version: int) -> None:
        self.event_type = event_type
        self.schema_version = schema_version
        super().__init__(
            f"Duplicate event registration for '{event_type}' schema_version {schema_version}."
        )
