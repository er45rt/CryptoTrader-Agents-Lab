"""Explicit trading-environment selection for the production runtime (Phase 14).

Phase 14 closes the architectural gap "no production endpoint path exists" WITHOUT authorising
production trading:

* the environment is an explicit enum (never a free-form host string);
* the default is testnet, and an unknown value fails closed rather than guessing;
* a production/mainnet endpoint path EXISTS but is disabled by construction: requesting it raises
  before any client is built, any credential is read or any request is attempted;
* a future phase may enable it only through the explicit owner-authorization gate, which is a
  reviewed source change to :data:`MAINNET_ENABLED`, not a runtime setting;
* an environment and an endpoint host can never be silently confused: a testnet environment
  refuses a mainnet host and a mainnet environment refuses a testnet host.

No credential value is read, logged, stored or returned anywhere in this module.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from urllib.parse import urlparse

from crypto_trader.execution.binance_testnet import (
    MAINNET_HOST_MARKERS,
    SPOT_TESTNET_REST_BASE,
    is_testnet_host,
    require_testnet_base_url,
)

from .errors import (
    EnvironmentEndpointMismatchError,
    MainnetDisabledError,
    ProductionConfigError,
)

__all__ = [
    "DEFAULT_ENVIRONMENT",
    "ENVIRONMENT_ENV_VAR",
    "MAINNET_ENABLED",
    "EnvironmentDecision",
    "TradingEnvironment",
    "assert_endpoint_matches_environment",
    "endpoint_for",
    "parse_environment",
    "resolve_environment",
]

#: Optional explicit selection. Unset (or blank) means :data:`DEFAULT_ENVIRONMENT`.
ENVIRONMENT_ENV_VAR = "CRYPTO_TRADER_ENV"

#: Production is testnet-first. Nothing in this phase changes this default.
DEFAULT_ENVIRONMENT = "testnet"

#: The owner-authorization gate for a production/live environment.
#:
#: Phase 14 ships the selection and the endpoint path but keeps production DISABLED. Enabling it is
#: a future, owner-authorized source change to this constant; there is no runtime flag, environment
#: variable or configuration file that can turn it on.
MAINNET_ENABLED = False

_TESTNET_ALIASES = frozenset({"testnet", "test_net", "spot_testnet"})
_MAINNET_ALIASES = frozenset({"mainnet", "main_net", "live", "production"})


class TradingEnvironment(str, Enum):
    """The complete, explicit environment vocabulary."""

    TESTNET = "testnet"
    MAINNET = "mainnet"


@dataclass(frozen=True)
class EnvironmentDecision:
    """The resolved environment, its endpoint and the reviewable reason for the outcome."""

    requested: str
    environment: TradingEnvironment
    base_url: str
    mainnet_authorized: bool
    notes: tuple[str, ...] = ()

    @property
    def is_testnet(self) -> bool:
        return self.environment is TradingEnvironment.TESTNET

    def to_record(self) -> dict[str, object]:
        """A JSON-able, secret-free observability record."""
        return {
            "requested": self.requested,
            "environment": self.environment.value,
            "base_url": self.base_url,
            "mainnet_authorized": self.mainnet_authorized,
            "notes": list(self.notes),
        }


def parse_environment(value: str | TradingEnvironment | None) -> TradingEnvironment:
    """Resolve an explicit environment value, failing closed on anything unknown."""
    if value is None:
        return TradingEnvironment(DEFAULT_ENVIRONMENT)
    if isinstance(value, TradingEnvironment):
        return value
    if not isinstance(value, str) or not value.strip():
        raise ProductionConfigError(
            "the trading environment must be a non-empty value such as 'testnet'."
        )
    normalised = value.strip().lower().replace("-", "_")
    if normalised in _TESTNET_ALIASES:
        return TradingEnvironment.TESTNET
    if normalised in _MAINNET_ALIASES:
        return TradingEnvironment.MAINNET
    raise ProductionConfigError(
        f"unknown trading environment {value!r}; refusing to guess between testnet and a "
        "production endpoint."
    )


def assert_endpoint_matches_environment(
    environment: TradingEnvironment, base_url: str
) -> str:
    """Fail closed when an environment and an endpoint host are confused for one another."""
    if not isinstance(base_url, str) or not base_url.strip():
        raise ProductionConfigError("a REST base URL is required.")
    candidate = base_url.strip()
    lowered = candidate.lower()
    if environment is TradingEnvironment.TESTNET:
        if any(marker in lowered for marker in MAINNET_HOST_MARKERS):
            raise EnvironmentEndpointMismatchError(
                "a production host was supplied for the testnet environment; refusing to use it."
            )
        if not is_testnet_host(candidate):
            raise EnvironmentEndpointMismatchError(
                "the testnet environment requires a documented testnet host; refusing to use "
                "another host."
            )
        return require_testnet_base_url(candidate)
    if environment is TradingEnvironment.MAINNET:
        if is_testnet_host(candidate):
            raise EnvironmentEndpointMismatchError(
                "a testnet host was supplied for the production environment; refusing to use it."
            )
        return candidate.rstrip("/")
    raise ProductionConfigError(f"unknown trading environment {environment!r}.")


def endpoint_for(
    environment: TradingEnvironment,
    *,
    mainnet_authorized: bool = False,
    mainnet_base_url: str | None = None,
) -> str:
    """The endpoint for an environment, or a fail-closed refusal.

    The testnet environment always resolves to the documented Spot Testnet host. The production
    environment is unreachable in Phase 14: it is refused before any URL, credential or request is
    touched, and even after a future authorization it must be given an explicit mainnet host that
    matches the documented production allowlist.
    """
    if environment is TradingEnvironment.TESTNET:
        return require_testnet_base_url(SPOT_TESTNET_REST_BASE)
    if environment is TradingEnvironment.MAINNET:
        if not MAINNET_ENABLED:
            raise MainnetDisabledError(
                "the production environment is disabled in this phase: the runtime is "
                "testnet-capable only. No credential was read and no request was attempted."
            )
        if not mainnet_authorized:
            raise MainnetDisabledError(
                "the production environment requires an explicit owner authorization gate; none "
                "was supplied."
            )
        if not isinstance(mainnet_base_url, str) or not mainnet_base_url.strip():
            raise ProductionConfigError(
                "an authorized production environment requires an explicit base URL."
            )
        host = (urlparse(mainnet_base_url.strip()).hostname or "").lower()
        if host not in MAINNET_HOST_MARKERS:
            raise EnvironmentEndpointMismatchError(
                f"{host!r} is not a documented production host; refusing to use it."
            )
        return mainnet_base_url.strip().rstrip("/")
    raise ProductionConfigError(f"unknown trading environment {environment!r}.")


def resolve_environment(
    requested: str | TradingEnvironment | None = None,
    *,
    mainnet_authorized: bool = False,
    mainnet_base_url: str | None = None,
    environ: Mapping[str, str] | None = None,
) -> EnvironmentDecision:
    """Resolve the environment: explicit argument, then configuration, then the testnet default."""
    source = os.environ if environ is None else environ
    raw = requested if requested is not None else source.get(ENVIRONMENT_ENV_VAR)
    environment = parse_environment(raw)
    base_url = endpoint_for(
        environment,
        mainnet_authorized=mainnet_authorized,
        mainnet_base_url=mainnet_base_url,
    )
    base_url = assert_endpoint_matches_environment(environment, base_url)
    notes = (
        "explicit environment selection; testnet and production endpoints cannot be confused",
        "the production environment is disabled by default and requires a future owner "
        "authorization gate",
        "environment resolution reads no credential",
    )
    return EnvironmentDecision(
        requested=DEFAULT_ENVIRONMENT if raw is None else str(raw).strip(),
        environment=environment,
        base_url=base_url,
        mainnet_authorized=bool(mainnet_authorized) and MAINNET_ENABLED,
        notes=notes,
    )
