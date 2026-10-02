"""Production runtime boundary and writer fencing (Phase 14).

Phase 14 closes the two technical blockers from the Final Owner Review:

1. there was no production endpoint path - now there is an explicit, testnet-first environment
   selection with a production/mainnet path that exists but is DISABLED and refused before any
   credential or request;
2. the normal-writer lease and the persisted safety state were never consulted at a production
   decision boundary - now the runtime claims the Phase 13 normal lease at startup and re-validates
   the held fence plus the persisted safety posture immediately before every venue submission.

It reuses the real components (Phase 11 Spot Testnet execution port, Phase 8 recovery/
reconciliation, Phase 9 safety controller, Phase 13 break-glass ownership/fencing, Phase 5 journal,
Phase 6 validity and Phase 7 Hard Risk) and adds no event contract.

Scope: Binance SPOT TESTNET only. No production/mainnet order, no real capital, no withdrawal and
no credential value are ever involved. Phase 14 readiness is NOT live authorization.
"""

from .environment import (
    DEFAULT_ENVIRONMENT,
    ENVIRONMENT_ENV_VAR,
    MAINNET_ENABLED,
    EnvironmentDecision,
    TradingEnvironment,
    assert_endpoint_matches_environment,
    endpoint_for,
    parse_environment,
    resolve_environment,
)
from .errors import (
    EnvironmentEndpointMismatchError,
    MainnetDisabledError,
    ProductionConfigError,
    ProductionFenceError,
    ProductionRecoveryRequiredError,
    ProductionRuntimeError,
    ProductionSafetyBlockedError,
    ProductionStartupError,
    ProductionStateError,
)
from .fence import FenceCheck, ProductionFence
from .equity import EquityChainState, load_equity_chain
from .runtime import (
    PRODUCTION_PRODUCER,
    IntentPreparation,
    ProductionExecutionOutcome,
    ProductionIntent,
    ProductionRuntime,
    ProductionStartupReport,
    ProductionSubmitResult,
    production_control,
)

__all__ = [
    "DEFAULT_ENVIRONMENT",
    "ENVIRONMENT_ENV_VAR",
    "MAINNET_ENABLED",
    "PRODUCTION_PRODUCER",
    "EnvironmentDecision",
    "EnvironmentEndpointMismatchError",
    "EquityChainState",
    "FenceCheck",
    "IntentPreparation",
    "MainnetDisabledError",
    "ProductionConfigError",
    "ProductionExecutionOutcome",
    "ProductionFence",
    "ProductionFenceError",
    "ProductionIntent",
    "ProductionRecoveryRequiredError",
    "ProductionRuntime",
    "ProductionRuntimeError",
    "ProductionSafetyBlockedError",
    "ProductionStartupError",
    "ProductionStartupReport",
    "ProductionStateError",
    "ProductionSubmitResult",
    "TradingEnvironment",
    "assert_endpoint_matches_environment",
    "endpoint_for",
    "load_equity_chain",
    "parse_environment",
    "production_control",
    "resolve_environment",
]
