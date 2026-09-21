"""Transport-agnostic, immutable event envelope (Phase 3).

Scope
-----
This module defines *contracts and semantics only*. There is no broker, no event bus,
no persistence, no dispatch, and no business logic here. Future components (OMS, Risk,
Simulator, Execution, Recovery, Event Bus) are expected to exchange these envelopes.

Delivery model (documented contract)
------------------------------------
The future system assumes **at-least-once** delivery. Exactly-once delivery is *not*
claimed. Consumers must therefore be idempotent and de-duplicate on :attr:`event_id`:

* ``event_id`` is the canonical identity of one logical event.
* A publisher retrying delivery of the *same logical event* MUST reuse the same
  ``event_id`` (re-serialize the same instance rather than building a new one).
* Two separately created logical events receive distinct ``event_id`` values, even when
  their payloads are identical.

Ordering (documented contract)
------------------------------
There is no global ordering guarantee across events or event types. ``sequence_number``
is optional at this layer and is meaningful only within the scope of the entity it
belongs to (for example a single order). Future consumers may detect sequence gaps;
gap recovery and sequencing storage are explicitly out of scope for Phase 3.

Versioning (documented contract)
--------------------------------
``schema_version`` starts at 1. Breaking changes require a new schema version; a
consumer must never silently parse an unknown version as v1 (see
:mod:`crypto_trader.events.registry`).
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

__all__ = [
    "DOMAIN_EVENT_TYPE_PATTERN",
    "EVENT_PRODUCER_PATTERN",
    "FIRST_SCHEMA_VERSION",
    "EventEnvelope",
    "DomainEvent",
    "EventPayload",
    "is_valid_event_type",
]

#: Event types are lowercase, dot-separated, machine-readable identifiers such as
#: ``order.state.changed``. Display labels are never part of the contract.
DOMAIN_EVENT_TYPE_PATTERN = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$")

#: Producer identifiers name the emitting component, e.g. ``agent.risk_analyst``.
EVENT_PRODUCER_PATTERN = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*$")

_SYMBOL_PATTERN = re.compile(r"^[A-Z0-9]{5,20}$")
_STRATEGY_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,127}$")

#: The first schema version of every event type.
FIRST_SCHEMA_VERSION = 1


def is_valid_event_type(event_type: str) -> bool:
    """Return True when ``event_type`` follows the documented naming convention."""
    return bool(isinstance(event_type, str) and DOMAIN_EVENT_TYPE_PATTERN.match(event_type))


class EventPayload(BaseModel):
    """Base class for typed event payloads.

    Payloads carry stage-specific data only. Entity/workflow context (symbol, order id,
    strategy id, sequencing) lives on the envelope so it stays uniform across stages.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")


class EventEnvelope(BaseModel):
    """Immutable event envelope shared by every stage of the future pipeline.

    Attributes:
        event_id: Canonical identity of this logical event (UUID, auto-generated).
        event_type: Stable machine-readable type (see :data:`DOMAIN_EVENT_TYPE_PATTERN`).
        schema_version: Positive integer contract version, starting at 1.
        occurred_at: Timezone-aware timestamp, normalized to UTC.
        correlation_id: Workflow trace id, constant across a causal chain.
        causation_id: ``event_id`` of the event that directly caused this one.
        producer: Component that emitted the event.
        strategy_id: Optional workflow/strategy context.
        order_id: Optional order context (system order identity, not a venue id).
        symbol: Optional market symbol context, e.g. ``ETHUSDT``.
        sequence_number: Optional per-entity ordering hint (positive when present).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    event_id: UUID = Field(
        default_factory=uuid.uuid4,
        description="Canonical event identity; reused when the same event is re-delivered.",
    )
    event_type: str = Field(description="Stable dot-separated machine-readable event type.")
    schema_version: int = Field(
        default=FIRST_SCHEMA_VERSION,
        ge=FIRST_SCHEMA_VERSION,
        description="Contract version for this event type; starts at 1.",
    )
    occurred_at: datetime = Field(description="Timezone-aware event time, normalized to UTC.")
    correlation_id: UUID = Field(description="Workflow trace id, constant across a causal chain.")
    causation_id: UUID | None = Field(
        default=None, description="event_id of the event that directly caused this one."
    )
    producer: str = Field(description="Emitting component, e.g. 'agent.trading_plan_strategist'.")

    # Entity / workflow context (optional at the base layer).
    strategy_id: str | None = Field(default=None, description="Strategy or workflow identifier.")
    order_id: UUID | None = Field(default=None, description="System order identity.")
    symbol: str | None = Field(default=None, description="Market symbol, e.g. ETHUSDT.")
    sequence_number: int | None = Field(
        default=None,
        ge=1,
        description="Optional per-entity ordering hint; scoped to the entity, not global.",
    )

    @field_validator("event_type")
    @classmethod
    def _validate_event_type(cls, value: str) -> str:
        if not is_valid_event_type(value):
            raise ValueError(
                "event_type must be lowercase dot-separated identifiers "
                "(for example 'order.state.changed')."
            )
        return value

    @field_validator("producer")
    @classmethod
    def _validate_producer(cls, value: str) -> str:
        if not isinstance(value, str) or not EVENT_PRODUCER_PATTERN.match(value):
            raise ValueError(
                "producer must be a lowercase dotted identifier (for example 'agent.risk_analyst')."
            )
        return value

    @field_validator("symbol")
    @classmethod
    def _validate_symbol(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not _SYMBOL_PATTERN.match(value):
            raise ValueError("symbol must be an uppercase market symbol such as ETHUSDT.")
        return value

    @field_validator("strategy_id")
    @classmethod
    def _validate_strategy_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not _STRATEGY_ID_PATTERN.match(value):
            raise ValueError(
                "strategy_id must be 1-128 characters of letters, digits, '_', '-' or '.'."
            )
        return value

    @field_validator("occurred_at")
    @classmethod
    def _validate_occurred_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError(
                "occurred_at must be timezone-aware; naive datetimes are rejected rather than "
                "guessed. Use an explicit offset or UTC."
            )
        return value.astimezone(timezone.utc)

    @model_validator(mode="after")
    def _reject_self_causation(self) -> "EventEnvelope":
        if self.causation_id is not None and self.causation_id == self.event_id:
            raise ValueError("causation_id must not equal event_id (self-causation).")
        return self

    # --- Convenience helpers (no transport behaviour) ---

    def to_json(self) -> str:
        """Serialize to JSON. Decimals/UUIDs/timestamps serialize as strings."""
        return self.model_dump_json()

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-compatible mapping."""
        return self.model_dump(mode="json")


class DomainEvent(EventEnvelope):
    """Base for typed V1 domain contracts: a fixed ``event_type``/``schema_version``
    plus a typed ``payload``.

    Concrete contracts declare ``event_type`` and ``schema_version`` as ``Literal``
    defaults and annotate ``payload`` with their payload model, which lets the registry
    bind the two without any dynamic imports.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    payload: EventPayload = Field(description="Stage-specific typed payload.")
    schema_version: int = Field(default=FIRST_SCHEMA_VERSION, ge=FIRST_SCHEMA_VERSION)
