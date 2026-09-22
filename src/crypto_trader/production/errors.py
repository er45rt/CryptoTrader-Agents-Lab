"""Typed errors for the Phase 14 production runtime boundary.

Every failure mode is explicit and fail-closed: an environment confusion, a disabled
production-endpoint request, an unsafe startup posture, unresolved order state and a superseded
writer token never degrade into a silent submission.

``StaleWriterFenceError`` is deliberately NOT converted into any error here: the caller must be
able to tell "my writer lease was taken over" from "the persisted posture forbids new risk".
"""

from __future__ import annotations

__all__ = [
    "EnvironmentEndpointMismatchError",
    "MainnetDisabledError",
    "ProductionConfigError",
    "ProductionFenceError",
    "ProductionRecoveryRequiredError",
    "ProductionRuntimeError",
    "ProductionSafetyBlockedError",
    "ProductionStartupError",
    "ProductionStateError",
]


class ProductionRuntimeError(RuntimeError):
    """Base class for every Phase 14 production-runtime failure."""


class ProductionConfigError(ProductionRuntimeError):
    """Mandatory configuration is missing, unknown or self-contradictory (fail closed)."""


class MainnetDisabledError(ProductionRuntimeError):
    """A production/mainnet environment was requested while it is disabled.

    The environment selection and the endpoint path exist, but mainnet stays disabled by default
    and unreachable in Phase 14. This is raised BEFORE any client is constructed, before any
    credential is read and before any request could be attempted.
    """


class EnvironmentEndpointMismatchError(ProductionConfigError):
    """A trading environment and an endpoint host were mixed (testnet vs mainnet)."""


class ProductionStartupError(ProductionRuntimeError):
    """Startup refused: the persisted posture does not permit a normal trading writer."""


class ProductionRecoveryRequiredError(ProductionStartupError):
    """Startup refused because unresolved order state requires reconciliation first."""


class ProductionStateError(ProductionRuntimeError):
    """The writer lease or the persisted safety state is absent, corrupt or unusable."""


class ProductionSafetyBlockedError(ProductionRuntimeError):
    """The final safety/fence gate refused a new-risk submission."""


class ProductionFenceError(ProductionSafetyBlockedError):
    """The runtime holds no usable writer fence, so it may not submit anything."""
