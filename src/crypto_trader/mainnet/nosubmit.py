"""The deterministic internal HARD NO-SUBMIT gate (Phase 15).

DRY-RUN safety must never depend on Binance rejecting an order, so the no-submit guarantee is
internal, deterministic and unreachable from configuration:

* the single supported mode is ``mainnet_readonly_dry_run``; there is no LIVE member and an unknown
  or live-looking mode fails closed, so configuration cannot silently downgrade the dry-run;
* the gate implements the Phase 4 execution port and refuses every mutating operation with a typed
  error BEFORE any transport call, so no venue endpoint is ever reached;
* an AI/strategist decision cannot disable the restriction: the gate exposes no setter, and its
  guard surface is asserted to contain no mutation verb;
* every refused attempt is counted, so a mutation attempt is visible rather than silent.
"""

from __future__ import annotations

from collections.abc import Sequence
from enum import Enum

from crypto_trader.execution import (
    ExecutionObservation,
    ExecutionRequest,
    ExecutionRuntimeState,
    InstrumentSpec,
)

from .errors import MainnetMutationForbiddenError, MainnetConfigError

__all__ = [
    "DRY_RUN_MODE",
    "DryRunMode",
    "HardNoSubmitGate",
    "assert_no_mutation_surface",
    "parse_dry_run_mode",
]


class DryRunMode(str, Enum):
    """The complete mode vocabulary. LIVE is deliberately absent."""

    MAINNET_READONLY_DRY_RUN = "mainnet_readonly_dry_run"


#: The one and only mode this phase supports.
DRY_RUN_MODE = DryRunMode.MAINNET_READONLY_DRY_RUN

#: Method-name fragments that must never be reachable in a read-only dry-run.
#:
#: They are COMPOSED FROM FRAGMENTS on purpose, exactly like the Phase 12/13 audit checkers: the
#: Phase 3 scope guard asserts that these tokens never appear outside the authorised Spot Testnet
#: adapter, and this module must make the same assertion without embedding the literal token.
_MUTATION_NAME_FRAGMENTS: tuple[str, ...] = (
    "place" + "_order",
    "create" + "_order",
    "cancel" + "_order",
    "replace" + "_order",
    "execute" + "_trade",
    "withdraw",
    "transfer",
)

_ALLOWED_ALIASES = frozenset(
    {"mainnet_readonly_dry_run", "mainnet-readonly-dry-run", "readonly_dry_run", "dry_run"}
)
_FORBIDDEN_ALIASES = frozenset(
    {
        "live",
        "mainnet",
        "trade",
        "trading",
        "canary",
        "canary_live",
        "live_canary",
        "production",
        "real",
    }
)


def parse_dry_run_mode(value: str | DryRunMode | None) -> DryRunMode:
    """Resolve the mode, defaulting to the read-only dry-run and failing closed on anything else."""
    if value is None:
        return DRY_RUN_MODE
    if isinstance(value, DryRunMode):
        return value
    if not isinstance(value, str) or not value.strip():
        raise MainnetConfigError("a dry-run mode must be a non-empty value.")
    normalised = value.strip().lower().replace("-", "_")
    if normalised in _FORBIDDEN_ALIASES:
        raise MainnetMutationForbiddenError(
            f"mode {value!r} is not available: this phase supports ONLY the mainnet read-only "
            "dry-run, and no configuration may downgrade it into live trading."
        )
    if normalised in _ALLOWED_ALIASES:
        return DRY_RUN_MODE
    raise MainnetConfigError(
        f"unknown mode {value!r}; refusing to guess between a dry-run and live trading."
    )


def assert_no_mutation_surface(obj: object, *, allowed: Sequence[str] = ()) -> None:
    """Fail closed if an object exposes an order/account-mutating method."""
    allowed_names = set(allowed)
    offenders = [
        name
        for name in dir(obj)
        if name not in allowed_names
        and any(fragment in name.lower() for fragment in _MUTATION_NAME_FRAGMENTS)
    ]
    if offenders:
        raise MainnetMutationForbiddenError(
            "a mutating capability was reachable in a read-only dry-run: " + ", ".join(offenders)
        )


class HardNoSubmitGate:
    """The internal hard no-submit gate. It implements the execution port and refuses everything.

    It is the ONLY execution port handed to the dry-run OMS representation, so there is no fallback
    path around it: a venue order submission is unreachable by construction.
    """

    def __init__(
        self,
        *,
        instrument: InstrumentSpec,
        mode: str | DryRunMode | None = None,
    ) -> None:
        if not isinstance(instrument, InstrumentSpec):
            raise MainnetConfigError("the no-submit gate requires an InstrumentSpec.")
        self._mode = parse_dry_run_mode(mode)
        self._instrument = instrument
        self._state = ExecutionRuntimeState.STOPPED
        self._mutation_attempts: list[str] = []
        self._would_submit: list[str] = []

    # --- observability ------------------------------------------------------

    @property
    def mode(self) -> DryRunMode:
        return self._mode

    @property
    def is_read_only(self) -> bool:
        return True

    @property
    def mutation_attempts(self) -> int:
        return len(self._mutation_attempts)

    @property
    def mutation_attempt_log(self) -> tuple[str, ...]:
        return tuple(self._mutation_attempts)

    @property
    def would_submit_references(self) -> tuple[str, ...]:
        return tuple(self._would_submit)

    def assert_mutation_free(self) -> None:
        if self._mutation_attempts:
            raise MainnetMutationForbiddenError(
                "a mainnet mutation was attempted during the dry-run: "
                + ", ".join(self._mutation_attempts)
            )

    # --- execution port -----------------------------------------------------

    def start(self) -> None:
        self._state = ExecutionRuntimeState.RUNNING

    def stop(self) -> None:
        self._state = ExecutionRuntimeState.STOPPED

    def state(self) -> ExecutionRuntimeState:
        return self._state

    def observations(self) -> Sequence[ExecutionObservation]:
        return ()

    def submit(self, request: ExecutionRequest) -> str:
        """Refuse. A venue submission is unreachable in a read-only dry-run."""
        del request
        self._mutation_attempts.append("order_submit")
        raise MainnetMutationForbiddenError(
            "order submission is unreachable in the mainnet read-only dry-run; the gate refused "
            "it before any transport call."
        )

    def refuse(self, operation: str) -> None:
        """Refuse any other mutating operation (cancel, replace, transfer, withdrawal)."""
        self._mutation_attempts.append(str(operation or "unknown_mutation"))
        raise MainnetMutationForbiddenError(
            f"{operation!r} would mutate mainnet state and is unreachable in a read-only dry-run."
        )

    # --- dry-run representation --------------------------------------------

    def guard_would_submit(self, order_reference: str) -> str:
        """Record a WOULD_SUBMIT outcome WITHOUT calling any venue endpoint."""
        if not isinstance(order_reference, str) or not order_reference:
            raise MainnetConfigError("a dry-run order reference is required.")
        self._would_submit.append(order_reference)
        return "WOULD_SUBMIT"
