"""Mainnet read-only dry-run and future canary execution guards (Phase 15).

Phase 15 adds the final pre-live step that needs a real venue: an authenticated MAINNET READ-ONLY
dry-run driven by dedicated read-only credentials. It is NOT live trading authorization.

Guarantees:

* dedicated credentials only (``BINANCE_MAINNET_READONLY_API_KEY`` /
  ``BINANCE_MAINNET_READONLY_API_SECRET``); the testnet pair is never reused;
* a strict signed permission contract (``/sapi/v1/account/apiRestrictions``) that fails closed on a
  missing, malformed or writable profile, and is re-validated at every would-be execution boundary;
* an internal deterministic HARD NO-SUBMIT gate: order submission, cancellation and replacement are
  unreachable, and no configuration can silently downgrade the dry-run into live trading;
* dynamically-read symbol filters (never hard-coded), the locked owner-approved pilot envelope, the
  spread/execution price rules and the LIMIT+FOK canary entry policy;
* a durable soak state with the exit-criteria counters that must be satisfied before any verdict.

Scope: reads only. No mainnet order is ever submitted, no real capital is used, no withdrawal is
enabled, and no credential value is printed, journaled, stored or committed.
"""

from .client import MainnetReadOnlyClient
from .credentials import (
    API_KEY_ENV_VAR,
    API_SECRET_ENV_VAR,
    MAINNET_READONLY_ENV_VARS,
    MainnetReadOnlyCredentials,
    credentials_from_environment,
    normalize_credential_value,
)
from .dryrun import DryRunCycleResult, DryRunDisposition, MainnetDryRunPipeline, OmsDryRunOrder
from .endpoints import (
    MUTATION_PATH_MARKERS,
    READ_ONLY_PATHS,
    SPOT_MAINNET_REST_BASE,
    require_mainnet_base_url,
    require_read_only_path,
)
from .envelope import (
    CANARY_ENTRY_ORDER_TYPE,
    CANARY_ENTRY_TIME_IN_FORCE,
    EXECUTION_PRICE_RULES,
    FIRST_CANARY_TRADES_REVIEW_CHECKPOINT,
    INSUFFICIENT_EXECUTION_SAMPLE,
    LOCKED_PILOT_ENVELOPE,
    MIN_FOK_FILLS_FOR_OWNER_REVIEW,
    CanaryEntryOrder,
    EnvelopeActivation,
    ExecutionPriceRules,
    FillSlippageRecord,
    LockedPilotEnvelope,
    NoChasingPolicy,
    absolute_bound_price,
    assert_envelope_is_reference_only,
    build_canary_entry,
    classify_execution_sample,
    envelope_record,
    fill_slippage_record,
    realized_slippage_bps,
    require_admissible_spread,
    require_within_absolute_bound,
    spread_bps,
)
from .errors import (
    CanaryPolicyError,
    DryRunError,
    LockedEnvelopeError,
    MainnetConfigError,
    MainnetEndpointError,
    MainnetError,
    MainnetMutationForbiddenError,
    MainnetReadOnlyCredentialsRequiredError,
    OldSnapshotReuseError,
    PermissionSchemaError,
    ReadOnlyPermissionError,
    SymbolFilterError,
)
from .faults import (
    IMMEDIATE_REJECTION_FAULTS,
    POST_SEND_AMBIGUITY_FAULTS,
    AmbiguityInjectionResult,
    FaultClass,
    SendFault,
    classify_send_fault,
    inject_post_send_ambiguity,
    require_post_send_ambiguity,
)
from .filters import SymbolFilters, parse_symbol_filters
from .limitations import (
    DRY_RUN_RECONCILIATION_GAP,
    FOK_DOCUMENTED_LIMITATIONS,
    RESIDUAL_LIMITATIONS,
    documented_limitations,
)
from .nosubmit import (
    DRY_RUN_MODE,
    DryRunMode,
    HardNoSubmitGate,
    assert_no_mutation_surface,
    parse_dry_run_mode,
)
from .permissions import (
    REQUIRED_PERMISSION_FIELDS,
    ApiRestrictions,
    parse_api_restrictions,
    validate_read_only_profile,
)
from .soak import (
    MIN_CONTINUOUS_SECONDS,
    MIN_PIPELINE_CYCLES,
    SOAK_SCHEMA_VERSION,
    SoakCounters,
    SoakRecorder,
    SoakState,
    SoakStateStore,
    evaluate_exit_criteria,
)

__all__ = [
    "API_KEY_ENV_VAR",
    "API_SECRET_ENV_VAR",
    "CANARY_ENTRY_ORDER_TYPE",
    "CANARY_ENTRY_TIME_IN_FORCE",
    "DRY_RUN_MODE",
    "DRY_RUN_RECONCILIATION_GAP",
    "EXECUTION_PRICE_RULES",
    "FIRST_CANARY_TRADES_REVIEW_CHECKPOINT",
    "FOK_DOCUMENTED_LIMITATIONS",
    "INSUFFICIENT_EXECUTION_SAMPLE",
    "IMMEDIATE_REJECTION_FAULTS",
    "LOCKED_PILOT_ENVELOPE",
    "MAINNET_READONLY_ENV_VARS",
    "MIN_CONTINUOUS_SECONDS",
    "MIN_FOK_FILLS_FOR_OWNER_REVIEW",
    "MIN_PIPELINE_CYCLES",
    "MUTATION_PATH_MARKERS",
    "READ_ONLY_PATHS",
    "REQUIRED_PERMISSION_FIELDS",
    "RESIDUAL_LIMITATIONS",
    "SOAK_SCHEMA_VERSION",
    "SPOT_MAINNET_REST_BASE",
    "AmbiguityInjectionResult",
    "ApiRestrictions",
    "CanaryEntryOrder",
    "CanaryPolicyError",
    "DryRunCycleResult",
    "DryRunDisposition",
    "DryRunError",
    "DryRunMode",
    "EnvelopeActivation",
    "ExecutionPriceRules",
    "FaultClass",
    "FillSlippageRecord",
    "HardNoSubmitGate",
    "LockedEnvelopeError",
    "LockedPilotEnvelope",
    "MainnetConfigError",
    "MainnetDryRunPipeline",
    "MainnetEndpointError",
    "MainnetError",
    "MainnetMutationForbiddenError",
    "MainnetReadOnlyClient",
    "MainnetReadOnlyCredentials",
    "MainnetReadOnlyCredentialsRequiredError",
    "NoChasingPolicy",
    "OldSnapshotReuseError",
    "OmsDryRunOrder",
    "PermissionSchemaError",
    "POST_SEND_AMBIGUITY_FAULTS",
    "ReadOnlyPermissionError",
    "SendFault",
    "SoakCounters",
    "SoakRecorder",
    "SoakState",
    "SoakStateStore",
    "SymbolFilterError",
    "SymbolFilters",
    "assert_no_mutation_surface",
    "absolute_bound_price",
    "assert_envelope_is_reference_only",
    "build_canary_entry",
    "classify_execution_sample",
    "classify_send_fault",
    "credentials_from_environment",
    "documented_limitations",
    "envelope_record",
    "evaluate_exit_criteria",
    "fill_slippage_record",
    "inject_post_send_ambiguity",
    "normalize_credential_value",
    "parse_api_restrictions",
    "parse_dry_run_mode",
    "parse_symbol_filters",
    "realized_slippage_bps",
    "require_admissible_spread",
    "require_mainnet_base_url",
    "require_post_send_ambiguity",
    "require_read_only_path",
    "require_within_absolute_bound",
    "spread_bps",
    "validate_read_only_profile",
]
