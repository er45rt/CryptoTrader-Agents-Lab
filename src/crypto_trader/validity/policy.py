"""Explicit, strategy/config-driven validity policy (Phase 6).

Mandatory thresholds are never defaulted: missing or invalid configuration fails closed.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from .errors import ValidityConfigError

__all__ = ["ValidityPolicy", "load_policy"]

_ALLOWED_KEYS = {"policy_id", "max_age_seconds", "max_market_move_bps", "valid_until_seconds"}


@dataclass(frozen=True)
class ValidityPolicy:
    """Explicit thresholds for one strategy's decisions."""

    policy_id: str
    max_age_seconds: float
    max_market_move_bps: Decimal
    valid_until_seconds: float | None = None

    def __post_init__(self) -> None:
        if not self.policy_id:
            raise ValidityConfigError("policy_id is required.")
        if self.max_age_seconds <= 0:
            raise ValidityConfigError("max_age_seconds must be greater than zero.")
        if not isinstance(self.max_market_move_bps, Decimal) or self.max_market_move_bps <= 0:
            raise ValidityConfigError("max_market_move_bps must be a positive Decimal.")
        if self.valid_until_seconds is not None and self.valid_until_seconds <= 0:
            raise ValidityConfigError("valid_until_seconds must be greater than zero when set.")


def load_policy(config: Mapping[str, Any]) -> ValidityPolicy:
    """Build a policy from explicit configuration, failing closed on anything missing."""
    if not isinstance(config, Mapping):
        raise ValidityConfigError("validity policy configuration must be a mapping.")

    unknown = set(config) - _ALLOWED_KEYS
    if unknown:
        raise ValidityConfigError(f"unknown validity policy keys: {sorted(unknown)}")

    if "policy_id" not in config:
        raise ValidityConfigError("missing mandatory validity policy key 'policy_id'.")
    if "max_age_seconds" not in config:
        raise ValidityConfigError(
            "missing mandatory validity policy key 'max_age_seconds' (STALE threshold)."
        )
    if "max_market_move_bps" not in config:
        raise ValidityConfigError(
            "missing mandatory validity policy key 'max_market_move_bps' (MARKET_MOVED tolerance)."
        )

    try:
        max_age = float(config["max_age_seconds"])
    except (TypeError, ValueError) as exc:
        raise ValidityConfigError("max_age_seconds must be numeric.") from exc

    try:
        max_move = Decimal(str(config["max_market_move_bps"]))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValidityConfigError("max_market_move_bps must be a decimal number.") from exc

    valid_until: float | None = None
    if "valid_until_seconds" in config and config["valid_until_seconds"] is not None:
        try:
            valid_until = float(config["valid_until_seconds"])
        except (TypeError, ValueError) as exc:
            raise ValidityConfigError("valid_until_seconds must be numeric when set.") from exc

    return ValidityPolicy(
        policy_id=str(config["policy_id"]),
        max_age_seconds=max_age,
        max_market_move_bps=max_move,
        valid_until_seconds=valid_until,
    )
