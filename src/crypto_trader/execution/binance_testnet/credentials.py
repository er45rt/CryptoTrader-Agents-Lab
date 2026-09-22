"""Testnet credentials loaded ONLY from the environment (Phase 11).

Rules enforced here:

* credentials come from ``BINANCE_TESTNET_API_KEY`` / ``BINANCE_TESTNET_API_SECRET`` (the only two
  names Phase 11 may read);
* surrounding whitespace and one optional layer of surrounding quotes are removed on load - a
  padded value would otherwise be sent verbatim in the ``X-MBX-APIKEY`` header and rejected by the
  venue as ``-2014`` (value-free normalisation: the raw value is never logged);
* nothing is ever written to disk, journaled, printed or returned in a repr/str/dict;
* ``TestnetCredentials.__repr__`` and ``__str__`` are redacted, and ``to_redacted_dict()`` never
  contains a value;
* a missing credential raises :class:`TestnetCredentialsRequiredError` whose message names the
  environment variables only.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from os import environ

from .errors import TestnetCredentialsRequiredError

__all__ = [
    "API_KEY_ENV_VAR",
    "API_SECRET_ENV_VAR",
    "TESTNET_ENV_VARS",
    "TestnetCredentials",
    "credentials_from_environment",
    "normalize_credential_value",
]

API_KEY_ENV_VAR = "BINANCE_TESTNET_API_KEY"
API_SECRET_ENV_VAR = "BINANCE_TESTNET_API_SECRET"
TESTNET_ENV_VARS = (API_KEY_ENV_VAR, API_SECRET_ENV_VAR)

REDACTED = "<redacted-testnet-credential>"


@dataclass(frozen=True)
class TestnetCredentials:
    """Binance Spot Testnet key pair.

    The two value fields are excluded from ``repr`` so an accidental ``print(credentials)``,
    ``logger.info(credentials)`` or test failure diff can never expose them.
    """

    api_key: str = field(repr=False)
    api_secret: str = field(repr=False)

    def __post_init__(self) -> None:
        for name, value in (("api_key", self.api_key), ("api_secret", self.api_secret)):
            if not isinstance(value, str) or not value.strip():
                raise TestnetCredentialsRequiredError(
                    f"{name} is empty; set {API_KEY_ENV_VAR} and {API_SECRET_ENV_VAR} in the "
                    "environment (never in a file or on the command line)."
                )

    def __repr__(self) -> str:
        return "TestnetCredentials(api_key=<redacted>, api_secret=<redacted>, venue='binance-spot-testnet')"

    def __str__(self) -> str:
        return self.__repr__()

    @property
    def venue_label(self) -> str:
        """An explicit, non-secret identity marker for journals and reports."""
        return "binance-spot-testnet"

    def to_redacted_dict(self) -> dict[str, str]:
        """A shape-preserving mapping with no credential content."""
        return {
            "api_key": REDACTED,
            "api_secret": REDACTED,
            "venue": self.venue_label,
            "source": "environment",
        }

    def with_secret(self) -> str:
        """The secret, for signing only. Never log or persist the return value."""
        return self.api_secret


def credentials_from_environment(env: Mapping[str, str] | None = None) -> TestnetCredentials:
    """Load testnet credentials from the environment, or fail closed."""
    source = environ if env is None else env
    api_key = normalize_credential_value(source.get(API_KEY_ENV_VAR))
    api_secret = normalize_credential_value(source.get(API_SECRET_ENV_VAR))
    missing = [
        name
        for name, value in ((API_KEY_ENV_VAR, api_key), (API_SECRET_ENV_VAR, api_secret))
        if not value
    ]
    if missing:
        raise TestnetCredentialsRequiredError(
            "Binance Spot Testnet credentials are required: "
            + ", ".join(missing)
            + ". Set them in the environment only; do not paste them into chat or commit them."
        )
    return TestnetCredentials(api_key=api_key, api_secret=api_secret)


def normalize_credential_value(raw: str | None) -> str:
    """Trim surrounding whitespace and one layer of surrounding quotes.

    Never logs or returns a fingerprint of the value: callers only ever receive the usable token.
    """
    if raw is None:
        return ""
    value = raw.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        value = value[1:-1].strip()
    return value
