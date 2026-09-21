"""Explicit event registry and decode path (Phase 3).

Design rules encoded here
-------------------------
* Registration is explicit: ``register(event_type, schema_version, model)`` or
  ``register_model(ModelClass)`` which derives the pair from the class's ``Literal``
  declarations. There is no dynamic import and no import-from-payload anywhere.
* Duplicate registration of the same ``(event_type, schema_version)`` fails loudly.
* Unknown ``event_type`` and unsupported ``schema_version`` raise distinct typed errors,
  so a consumer can never silently parse an unknown version as v1.
* Decoding validates the full envelope against the registered model and returns that
  model instance. Malformed payloads raise :class:`EventValidationError`.
* Registries are instance based (dependency-injectable); a lazily built default registry
  is provided for convenience and can be replaced or reset in tests.

This module contains no broker, dispatcher, consumer loop, or event bus.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from pydantic import ValidationError

from .base import FIRST_SCHEMA_VERSION, EventEnvelope, is_valid_event_type
from .contracts import V1_CONTRACTS
from .errors import (
    DuplicateEventRegistrationError,
    EventContractError,
    EventValidationError,
    UnknownEventTypeError,
    UnsupportedSchemaVersionError,
)

__all__ = [
    "EventRegistry",
    "build_default_registry",
    "decode_event",
    "get_default_registry",
    "reset_default_registry",
]

_MAX_REPORTED_ERRORS = 8


class EventRegistry:
    """A deterministic, instance-based registry of event contracts."""

    def __init__(self) -> None:
        self._models: dict[tuple[str, int], type[EventEnvelope]] = {}

    # --- Registration ---

    def register(
        self,
        event_type: str,
        schema_version: int,
        model: type[EventEnvelope],
    ) -> type[EventEnvelope]:
        """Register ``model`` for ``(event_type, schema_version)``.

        Raises:
            EventContractError: invalid event type, version, or model declaration.
            DuplicateEventRegistrationError: the pair is already registered.
        """
        if not is_valid_event_type(event_type):
            raise EventContractError(
                f"event_type '{event_type}' does not follow the naming convention "
                "(lowercase dot-separated identifiers, e.g. 'order.state.changed')."
            )
        if not isinstance(schema_version, int) or isinstance(schema_version, bool):
            raise EventContractError("schema_version must be an integer.")
        if schema_version < FIRST_SCHEMA_VERSION:
            raise EventContractError(
                f"schema_version must be >= {FIRST_SCHEMA_VERSION}; got {schema_version}."
            )
        if not (isinstance(model, type) and issubclass(model, EventEnvelope)):
            raise EventContractError("model must be an EventEnvelope subclass.")

        declared_type = self._declared_default(model, "event_type")
        declared_version = self._declared_default(model, "schema_version")
        if declared_type is not None and declared_type != event_type:
            raise EventContractError(
                f"Model {model.__name__} declares event_type '{declared_type}' but was "
                f"registered as '{event_type}'."
            )
        if declared_version is not None and declared_version != schema_version:
            raise EventContractError(
                f"Model {model.__name__} declares schema_version {declared_version} but was "
                f"registered as {schema_version}."
            )

        key = (event_type, schema_version)
        if key in self._models:
            raise DuplicateEventRegistrationError(event_type, schema_version)

        self._models[key] = model
        return model

    def register_model(self, model: type[EventEnvelope]) -> type[EventEnvelope]:
        """Register a contract using its declared ``event_type``/``schema_version``."""
        event_type = self._declared_default(model, "event_type")
        schema_version = self._declared_default(model, "schema_version")
        if not isinstance(event_type, str) or not event_type:
            raise EventContractError(
                f"Model {getattr(model, '__name__', model)!r} must declare a default "
                "'event_type' before it can be registered by model."
            )
        if not isinstance(schema_version, int) or isinstance(schema_version, bool):
            raise EventContractError(
                f"Model {getattr(model, '__name__', model)!r} must declare a default "
                "'schema_version' before it can be registered by model."
            )
        return self.register(event_type, schema_version, model)

    # --- Lookup / decoding ---

    def resolve(self, event_type: str, schema_version: int) -> type[EventEnvelope]:
        """Return the registered model, or raise a typed error."""
        if not is_valid_event_type(event_type):
            raise EventValidationError(
                f"Serialized event has an invalid 'event_type' value: {event_type!r}."
            )
        if not isinstance(schema_version, int) or isinstance(schema_version, bool):
            raise EventValidationError("Serialized event has a non-integer 'schema_version'.")

        model = self._models.get((event_type, schema_version))
        if model is not None:
            return model

        if event_type not in self.registered_event_types():
            raise UnknownEventTypeError(event_type, known_types=self.registered_event_types())
        raise UnsupportedSchemaVersionError(
            event_type,
            schema_version,
            supported_versions=self.supported_versions(event_type),
        )

    def decode(self, payload: str | bytes | Mapping[str, Any]) -> EventEnvelope:
        """Decode a serialized event into its registered model instance."""
        data = _coerce_mapping(payload)

        event_type = data.get("event_type")
        if not isinstance(event_type, str) or not event_type:
            raise EventValidationError(
                "Serialized event is missing a valid 'event_type' string.",
            )
        schema_version = data.get("schema_version")
        if not isinstance(schema_version, int) or isinstance(schema_version, bool):
            raise EventValidationError(
                "Serialized event is missing an integer 'schema_version'.",
            )

        model = self.resolve(event_type, schema_version)
        try:
            return model.model_validate(data)
        except ValidationError as exc:
            raise EventValidationError(
                f"Event '{event_type}' v{schema_version} failed validation.",
                details=_format_validation_error(exc),
            ) from exc

    # --- Introspection ---

    def registered_event_types(self) -> tuple[str, ...]:
        """All registered event types, sorted for determinism."""
        return tuple(sorted({event_type for event_type, _ in self._models}))

    def supported_versions(self, event_type: str) -> tuple[int, ...]:
        """Registered schema versions for one event type, sorted."""
        return tuple(sorted(v for t, v in self._models if t == event_type))

    def registered_pairs(self) -> tuple[tuple[str, int], ...]:
        """All registered ``(event_type, schema_version)`` pairs, sorted."""
        return tuple(sorted(self._models))

    @staticmethod
    def _declared_default(model: type[EventEnvelope], field_name: str) -> Any:
        field = getattr(model, "model_fields", {}).get(field_name)
        return None if field is None else field.default


def _coerce_mapping(payload: str | bytes | Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(payload, (str, bytes)):
        raw = payload.decode("utf-8") if isinstance(payload, bytes) else payload
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError) as exc:
            raise EventValidationError(
                "Serialized event is not valid JSON.",
                details=[str(exc)],
            ) from exc
    elif isinstance(payload, Mapping):
        parsed = dict(payload)
    else:
        raise EventValidationError(
            f"Serialized event must be JSON text or a mapping, got {type(payload).__name__}."
        )

    if not isinstance(parsed, dict):
        raise EventValidationError("Serialized event must decode to a JSON object.")
    return parsed


def _format_validation_error(error: ValidationError) -> list[str]:
    details: list[str] = []
    for item in error.errors()[:_MAX_REPORTED_ERRORS]:
        location = ".".join(str(part) for part in item.get("loc", ())) or "<root>"
        details.append(f"{location}: {item.get('msg')}")
    return details


def build_default_registry() -> EventRegistry:
    """Build a fresh registry containing every V1 contract in this build."""
    registry = EventRegistry()
    for contract in V1_CONTRACTS:
        registry.register_model(contract)
    return registry


_DEFAULT_REGISTRY: EventRegistry | None = None


def get_default_registry() -> EventRegistry:
    """Return the lazily built process default registry (replaceable in tests)."""
    global _DEFAULT_REGISTRY
    if _DEFAULT_REGISTRY is None:
        _DEFAULT_REGISTRY = build_default_registry()
    return _DEFAULT_REGISTRY


def reset_default_registry() -> None:
    """Drop the cached default registry (test helper; no global state is mutated)."""
    global _DEFAULT_REGISTRY
    _DEFAULT_REGISTRY = None


def decode_event(
    payload: str | bytes | Mapping[str, Any],
    *,
    registry: EventRegistry | None = None,
) -> EventEnvelope:
    """Decode a serialized event using ``registry`` (default: the process registry)."""
    active = registry if registry is not None else get_default_registry()
    return active.decode(payload)
