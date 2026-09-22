"""Dedicated mainnet READ-ONLY credentials (Phase 15).

Rules enforced here:

* credentials come from ``BINANCE_MAINNET_READONLY_API_KEY`` /
  ``BINANCE_MAINNET_READONLY_API_SECRET`` and from nowhere else - the Phase 11 testnet pair is
  never read, never reused and never accepted here;
* nothing is written to disk, journaled, printed or returned in a repr/str/dict;
* ``__repr__``/``__str__`` are redacted and ``to_redacted_dict()`` never contains a value;
* a missing credential raises a typed error whose message names the environment variables only.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from os import environ

from .errors import MainnetReadOnlyCredentialsRequiredError

__all__ = [
    "API_KEY_ENV_VAR",
    "API_SECRET_ENV_VAR",
    "MAINNET_READONLY_ENV_VARS",
    "REDACTED",
    "MainnetReadOnlyCredentials",
    "credentials_from_environment",
    "normalize_credential_value",
]

API_KEY_ENV_VAR = "BINANCE_MAINNET_READONLY_API_KEY"
API_SECRET_ENV_VAR = "BINANCE_MAINNET_READONLY_API_SECRET"
MAINNET_READONLY_ENV_VARS = (API_KEY_ENV_VAR, API_SECRET_ENV_VAR)

REDACTED = "<redacted-mainnet-readonly-credential>"


@dataclass(frozen=True)
class MainnetReadOnlyCredentials:
    """A dedicated Binance MAINNET read-only key pair.

    Both value fields are excluded from ``repr`` so an accidental ``print``, ``logger.info`` or test
    failure diff can never expose them.
    """

    api_key: str = field(repr=False)
    api_secret: str = field(repr=False)

    def __post_init__(self) -> None:
        for name, value in (("api_key", self.api_key), ("api_secret", self.api_secret)):
            if not isinstance(value, str) or not value.strip():
                raise MainnetReadOnlyCredentialsRequiredError(
                    f"{name} is empty; set {API_KEY_ENV_VAR} and {API_SECRET_ENV_VAR} in the "
                    "environment (never in a file, on the command line or in chat)."
                )

    def __repr__(self) -> str:
        return (
            "MainnetReadOnlyCredentials(api_key=<redacted>, api_secret=<redacted>, "
            "venue='binance-spot-mainnet-readonly')"
        )

    def __str__(self) -> str:
        return self.__repr__()

    @property
    def venue_label(self) -> str:
        """An explicit, non-secret identity marker for records and reports."""
        return "binance-spot-mainnet-readonly"

    @property
    def permission_scope(self) -> str:
        return "read_only"

    def to_redacted_dict(self) -> dict[str, str]:
        """A shape-preserving mapping with no credential content."""
        return {
            "api_key": REDACTED,
            "api_secret": REDACTED,
            "venue": self.venue_label,
            "permission_scope": self.permission_scope,
            "source": "environment",
        }

    def with_secret(self) -> str:
        """The secret, for signing only. Never log or persist the return value."""
        return self.api_secret


def credentials_from_environment(
    env: Mapping[str, str] | None = None,
) -> MainnetReadOnlyCredentials:
    """Load the dedicated mainnet read-only credentials, or fail closed."""
    source = environ if env is None else env
    api_key = normalize_credential_value(source.get(API_KEY_ENV_VAR))
    api_secret = normalize_credential_value(source.get(API_SECRET_ENV_VAR))
    missing = [
        name
        for name, value in ((API_KEY_ENV_VAR, api_key), (API_SECRET_ENV_VAR, api_secret))
        if not value
    ]
    if missing:
        raise MainnetReadOnlyCredentialsRequiredError(
            "dedicated mainnet read-only credentials are required: "
            + ", ".join(missing)
            + ". Set them in the environment only; never paste a value into chat, a file or a "
            "commit."
        )
    return MainnetReadOnlyCredentials(api_key=api_key, api_secret=api_secret)


def normalize_credential_value(raw: str | None) -> str:
    """Trim surrounding whitespace and one layer of surrounding quotes (value-free)."""
    if raw is None:
        return ""
    value = raw.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        value = value[1:-1].strip()
    return value
