"""Explicit, configuration-driven Hard Risk limits (Phase 7).

UNITS ARE EXPLICIT AND MANDATORY - no implicit conversions anywhere in this layer:

* ``*_quote`` fields are :class:`decimal.Decimal` amounts in the QUOTE currency (e.g. USDT).
* ``*_pct`` fields are :class:`decimal.Decimal` values expressed as PERCENT (0-100).
* counts (``max_open_orders``, ``max_open_positions``) are integers.

Missing or invalid configuration fails closed: there are no default capital numbers.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from .errors import RiskConfigError

__all__ = ["RiskLimits", "load_limits"]

_REQUIRED_DECIMAL_QUOTE = (
    "max_order_notional_quote",
    "max_symbol_exposure_quote",
    "max_gross_exposure_quote",
    "max_net_exposure_quote",
    "max_strategy_exposure_quote",
    "loss_limit_quote",
)
_REQUIRED_DECIMAL_PCT = ("max_drawdown_pct", "concentration_limit_pct")
_REQUIRED_INT = ("max_open_orders", "max_open_positions")
_OPTIONAL_BOOL = ("reduce_only_mode", "trading_halted")
_ALLOWED_KEYS = {
    "limits_version",
    *_REQUIRED_DECIMAL_QUOTE,
    *_REQUIRED_DECIMAL_PCT,
    *_REQUIRED_INT,
    *_OPTIONAL_BOOL,
}


@dataclass(frozen=True)
class RiskLimits:
    """Deterministic rule set. All monetary values are quote-currency Decimals."""

    limits_version: str
    max_order_notional_quote: Decimal
    max_symbol_exposure_quote: Decimal
    max_gross_exposure_quote: Decimal
    max_net_exposure_quote: Decimal
    max_strategy_exposure_quote: Decimal
    loss_limit_quote: Decimal
    max_drawdown_pct: Decimal
    concentration_limit_pct: Decimal
    max_open_orders: int
    max_open_positions: int
    reduce_only_mode: bool = False
    trading_halted: bool = False

    def __post_init__(self) -> None:
        if not self.limits_version:
            raise RiskConfigError("limits_version is required.")
        for name in (*_REQUIRED_DECIMAL_QUOTE, *_REQUIRED_DECIMAL_PCT):
            value = getattr(self, name)
            if not isinstance(value, Decimal) or value <= 0:
                raise RiskConfigError(f"{name} must be a positive Decimal.")
        for name in _REQUIRED_INT:
            value = getattr(self, name)
            if not isinstance(value, int) or value <= 0:
                raise RiskConfigError(f"{name} must be a positive integer.")
        if not isinstance(self.reduce_only_mode, bool) or not isinstance(self.trading_halted, bool):
            raise RiskConfigError("reduce_only_mode and trading_halted must be booleans.")


def _decimal(value: Any, *, name: str) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise RiskConfigError(f"{name} must be a decimal number.") from exc


def _strict_bool(value: object, *, name: str) -> bool:
    """Accept only real booleans or explicit 'true'/'false' strings."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered == "true":
            return True
        if lowered == "false":
            return False
    raise RiskConfigError(f"{name} must be a boolean or 'true'/'false'; got {value!r}.")


def load_limits(config: Mapping[str, Any]) -> RiskLimits:
    """Build the rule set from explicit configuration, failing closed on anything missing."""
    if not isinstance(config, Mapping):
        raise RiskConfigError("risk limits configuration must be a mapping.")
    unknown = set(config) - _ALLOWED_KEYS
    if unknown:
        raise RiskConfigError(f"unknown risk limits keys: {sorted(unknown)}")
    missing = [key for key in _ALLOWED_KEYS if key not in _OPTIONAL_BOOL and key not in config]
    if missing:
        raise RiskConfigError(f"missing mandatory risk limits keys: {sorted(missing)}")

    integers: dict[str, int] = {}
    for name in _REQUIRED_INT:
        raw = config[name]
        if isinstance(raw, bool) or not isinstance(raw, int):
            raise RiskConfigError(f"{name} must be an integer.")
        integers[name] = raw

    decimals = {
        name: _decimal(config[name], name=name)
        for name in (*_REQUIRED_DECIMAL_QUOTE, *_REQUIRED_DECIMAL_PCT)
    }
    return RiskLimits(
        limits_version=str(config["limits_version"]),
        **decimals,
        **integers,
        reduce_only_mode=_strict_bool(
            config.get("reduce_only_mode", False), name="reduce_only_mode"
        ),
        trading_halted=_strict_bool(
            config.get("trading_halted", False), name="trading_halted"
        ),
    )
