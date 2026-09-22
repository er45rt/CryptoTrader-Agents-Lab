"""Explicit, configuration-driven reconciliation policy (Phase 8).

There are no ambiguous defaults here. Unknown or missing configuration fails closed:
reconciliation must behave the same way every restart.

Fail-closed invariants enforced at construction:

* ``unknown_external_order_policy`` must be ``"escalate"``. A venue order we do not know about is
  never silently adopted or ignored in this phase.
* ``halt_on_ambiguity`` must be ``True``. An unresolved/ambiguous order always blocks new risk.
* ``allow_resolution_from_venue`` may be turned off to force every venue discrepancy through an
  operator instead of applying automatic corrections.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .errors import RecoveryConfigError

__all__ = ["ReconciliationPolicy", "load_policy"]

_ALLOWED_KEYS = {
    "policy_id",
    "unknown_external_order_policy",
    "halt_on_ambiguity",
    "allow_resolution_from_venue",
}

#: The only supported handling of an order the venue reports but our journal does not know.
SUPPORTED_UNKNOWN_EXTERNAL_ORDER_POLICY = "escalate"


@dataclass(frozen=True)
class ReconciliationPolicy:
    """Deterministic reconciliation configuration. Fail closed on anything unsupported."""

    policy_id: str
    unknown_external_order_policy: str = SUPPORTED_UNKNOWN_EXTERNAL_ORDER_POLICY
    halt_on_ambiguity: bool = True
    allow_resolution_from_venue: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.policy_id, str) or not self.policy_id.strip():
            raise RecoveryConfigError("reconciliation requires a non-empty policy_id.")
        if self.unknown_external_order_policy != SUPPORTED_UNKNOWN_EXTERNAL_ORDER_POLICY:
            raise RecoveryConfigError(
                "unknown_external_order_policy must be "
                f"{SUPPORTED_UNKNOWN_EXTERNAL_ORDER_POLICY!r}; refusing to adopt or ignore "
                "unrecognised venue orders."
            )
        if self.halt_on_ambiguity is not True:
            raise RecoveryConfigError(
                "halt_on_ambiguity must be True: an unresolved order must always block new risk."
            )
        if not isinstance(self.allow_resolution_from_venue, bool):
            raise RecoveryConfigError("allow_resolution_from_venue must be a boolean.")


def load_policy(config: Mapping[str, Any]) -> ReconciliationPolicy:
    """Build a :class:`ReconciliationPolicy` from an explicit configuration mapping."""
    if not isinstance(config, Mapping):
        raise RecoveryConfigError("reconciliation policy configuration must be a mapping.")
    unknown = set(config) - _ALLOWED_KEYS
    if unknown:
        raise RecoveryConfigError(f"unknown reconciliation policy keys: {sorted(unknown)}")
    if "policy_id" not in config:
        raise RecoveryConfigError("reconciliation policy requires 'policy_id'.")
    return ReconciliationPolicy(
        policy_id=config["policy_id"],
        unknown_external_order_policy=config.get(
            "unknown_external_order_policy", SUPPORTED_UNKNOWN_EXTERNAL_ORDER_POLICY
        ),
        halt_on_ambiguity=config.get("halt_on_ambiguity", True),
        allow_resolution_from_venue=config.get("allow_resolution_from_venue", True),
    )
