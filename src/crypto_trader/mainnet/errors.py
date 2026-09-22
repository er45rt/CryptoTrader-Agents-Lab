"""Typed errors for the Phase 15 mainnet read-only dry-run.

Every failure mode is explicit and fail-closed. In particular, a request that would mutate mainnet
state is refused by an internal typed error, never by relying on the venue to reject it.
"""

from __future__ import annotations

__all__ = [
    "CanaryPolicyError",
    "DryRunError",
    "LockedEnvelopeError",
    "MainnetConfigError",
    "MainnetEndpointError",
    "MainnetError",
    "MainnetMutationForbiddenError",
    "MainnetReadOnlyCredentialsRequiredError",
    "OldSnapshotReuseError",
    "PermissionSchemaError",
    "ReadOnlyPermissionError",
    "SymbolFilterError",
]


class MainnetError(RuntimeError):
    """Base class for every Phase 15 failure."""


class MainnetConfigError(MainnetError):
    """Mandatory configuration is missing, unknown or contradictory (fail closed)."""


class MainnetReadOnlyCredentialsRequiredError(MainnetError):
    """The dedicated mainnet read-only credential pair is not present in the environment.

    The message names the two environment variables only and never contains a value.
    """


class MainnetEndpointError(MainnetError):
    """The configured endpoint is not an accepted mainnet read-only endpoint."""


class MainnetMutationForbiddenError(MainnetError):
    """An attempt was made to mutate mainnet state during a read-only dry-run.

    This is the internal hard no-submit guarantee. It is raised before any transport call, so the
    guarantee never depends on the venue rejecting an order.
    """


class ReadOnlyPermissionError(MainnetError):
    """The mainnet key does not satisfy the strict read-only permission profile."""


class PermissionSchemaError(ReadOnlyPermissionError):
    """The permission response was missing a field, malformed, or ambiguous."""


class SymbolFilterError(MainnetError):
    """A symbol filter was missing, malformed or inconsistent (fail closed)."""


class LockedEnvelopeError(MainnetError):
    """The locked pilot envelope (future canary limits) was treated as trading permission."""


class CanaryPolicyError(MainnetError):
    """A future canary order policy rule was violated."""


class OldSnapshotReuseError(CanaryPolicyError):
    """A market snapshot id was reused for a re-price/re-submit (no-chasing violation)."""


class DryRunError(MainnetError):
    """The dry-run pipeline could not complete deterministically."""
