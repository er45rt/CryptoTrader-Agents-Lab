"""Strict mainnet read-only permission profile (Phase 15).

The official ``GET /sapi/v1/account/apiRestrictions`` response is treated as a strict signed
USER_DATA contract: HTTP success alone is never sufficient.

Required fields (all must be present and be real booleans - ``0``/``1`` or a string is malformed,
not "close enough"):

=======================  =========
field                    required
=======================  =========
enableReading            true
enableWithdrawals        false
enableSpotAndMarginTrading false
enableMargin             false
enableFutures            false
enableVanillaOptions     false
enableFixApiTrade        false
=======================  =========

Any missing field, malformed field, non-boolean value, unexpected writable capability or parsing
ambiguity fails closed. The profile is re-validated at every would-be execution boundary during a
dry-run, because the key's permissions can be changed externally without the key string changing.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from .errors import PermissionSchemaError, ReadOnlyPermissionError

__all__ = [
    "REQUIRED_PERMISSION_FIELDS",
    "WRITABLE_PERMISSION_MARKERS",
    "ApiRestrictions",
    "parse_api_restrictions",
    "validate_read_only_profile",
]

#: The mandatory read-only contract fields and the value each must hold.
REQUIRED_PERMISSION_FIELDS: dict[str, bool] = {
    "enableReading": True,
    "enableWithdrawals": False,
    "enableSpotAndMarginTrading": False,
    "enableMargin": False,
    "enableFutures": False,
    "enableVanillaOptions": False,
    "enableFixApiTrade": False,
}

#: Field-name markers that indicate a writable capability. Any such field must be present as a
#: boolean and must be false; an unexpected truthy one fails closed.
WRITABLE_PERMISSION_MARKERS: tuple[str, ...] = (
    "Withdraw",
    "Trading",
    "Trade",
    "Transfer",
    "Margin",
    "Futures",
    "Options",
    "Borrow",
)


@dataclass(frozen=True)
class ApiRestrictions:
    """The validated read-only permission profile plus the value-free observed fields."""

    enable_reading: bool
    enable_withdrawals: bool
    enable_spot_and_margin_trading: bool
    enable_margin: bool
    enable_futures: bool
    enable_vanilla_options: bool
    enable_fix_api_trade: bool
    observed_fields: tuple[str, ...] = ()
    recorded: Mapping[str, Any] = field(default_factory=dict, compare=False)

    @property
    def is_read_only(self) -> bool:
        return (
            self.enable_reading
            and not self.enable_withdrawals
            and not self.enable_spot_and_margin_trading
            and not self.enable_margin
            and not self.enable_futures
            and not self.enable_vanilla_options
            and not self.enable_fix_api_trade
        )

    @property
    def writable_capabilities(self) -> tuple[str, ...]:
        """The names of any enabled writable capabilities (empty for a valid profile)."""
        flags = {
            "enableWithdrawals": self.enable_withdrawals,
            "enableSpotAndMarginTrading": self.enable_spot_and_margin_trading,
            "enableMargin": self.enable_margin,
            "enableFutures": self.enable_futures,
            "enableVanillaOptions": self.enable_vanilla_options,
            "enableFixApiTrade": self.enable_fix_api_trade,
        }
        return tuple(sorted(name for name, enabled in flags.items() if enabled))

    def to_record(self) -> dict[str, object]:
        """A JSON-able, secret-free record (booleans and names only)."""
        return {
            "enable_reading": self.enable_reading,
            "enable_withdrawals": self.enable_withdrawals,
            "enable_spot_and_margin_trading": self.enable_spot_and_margin_trading,
            "enable_margin": self.enable_margin,
            "enable_futures": self.enable_futures,
            "enable_vanilla_options": self.enable_vanilla_options,
            "enable_fix_api_trade": self.enable_fix_api_trade,
            "is_read_only": self.is_read_only,
            "writable_capabilities": list(self.writable_capabilities),
            "observed_fields": list(self.observed_fields),
        }


def _require_bool(body: Mapping[str, Any], name: str) -> bool:
    value = body.get(name)
    if not isinstance(value, bool):
        raise PermissionSchemaError(
            f"the permission response field {name!r} is missing or is not a boolean; refusing to "
            "interpret an ambiguous permission profile."
        )
    return value


def parse_api_restrictions(body: Any) -> ApiRestrictions:
    """Parse and validate the strict read-only permission contract, or fail closed."""
    if not isinstance(body, Mapping):
        raise PermissionSchemaError(
            "the permission response was not a JSON object; refusing to interpret it."
        )
    parsed = {
        name: _require_bool(body, name) for name in REQUIRED_PERMISSION_FIELDS
    }

    # Any OTHER field that names a writable capability must be a boolean and must be false.
    for key, value in body.items():
        if not isinstance(key, str) or key in REQUIRED_PERMISSION_FIELDS:
            continue
        if not any(marker in key for marker in WRITABLE_PERMISSION_MARKERS):
            continue
        if not isinstance(value, bool):
            raise PermissionSchemaError(
                f"the permission response field {key!r} names a writable capability but is not a "
                "boolean; refusing to interpret an ambiguous permission profile."
            )
        if value:
            raise ReadOnlyPermissionError(
                f"the mainnet key reports an unexpected writable capability ({key!r}); a read-only "
                "dry-run refuses it."
            )

    recorded = {str(key): value for key, value in body.items() if isinstance(value, bool)}
    restrictions = ApiRestrictions(
        enable_reading=parsed["enableReading"],
        enable_withdrawals=parsed["enableWithdrawals"],
        enable_spot_and_margin_trading=parsed["enableSpotAndMarginTrading"],
        enable_margin=parsed["enableMargin"],
        enable_futures=parsed["enableFutures"],
        enable_vanilla_options=parsed["enableVanillaOptions"],
        enable_fix_api_trade=parsed["enableFixApiTrade"],
        observed_fields=tuple(sorted(str(key) for key in body)),
        recorded=recorded,
    )
    validate_read_only_profile(restrictions)
    return restrictions


def validate_read_only_profile(restrictions: ApiRestrictions) -> ApiRestrictions:
    """Fail closed unless the profile is exactly the read-only one."""
    if not isinstance(restrictions, ApiRestrictions):
        raise PermissionSchemaError("a validated ApiRestrictions profile is required.")
    if not restrictions.enable_reading:
        raise ReadOnlyPermissionError(
            "the mainnet key cannot read account data (enableReading is false); a read-only "
            "dry-run requires reading."
        )
    writable = restrictions.writable_capabilities
    if writable:
        raise ReadOnlyPermissionError(
            "the mainnet key is not read-only; refusing the dry-run because these capabilities are "
            f"enabled: {', '.join(writable)}."
        )
    return restrictions
