"""Binance SPOT TESTNET adapter (Phase 11) - infrastructure layer.

Scope: authenticated **Spot Testnet** only.

* mainnet/futures/margin endpoints are blocked at construction time;
* credentials come from ``BINANCE_TESTNET_API_KEY`` / ``BINANCE_TESTNET_API_SECRET`` only, are
  never logged, journaled, persisted or repr'd, and require the environment variables solely;
* reads retry with deterministic backoff; writes never blind-retry - an unknown write outcome is
  ``AmbiguousOrderError`` (``UNKNOWN_PENDING_RECONCILIATION``) and goes through Phase 8 recovery;
* ``newClientOrderId`` is the idempotency key, so a duplicate submission cannot create a second
  venue order, including across restarts;
* the runtime exposes the Phase 4 execution port, the Phase 8 venue view and honours the Phase 9
  safety controller.

Testnet success never authorizes mainnet trading.
"""

from .backfill import (
    BackfillEvent,
    TradeBackfillPlan,
    plan_trade_backfill,
    trade_backfill_plan_fingerprint,
    trade_backfill_source_revision,
)
from .client import BinanceSpotTestnetClient, PreparedTestnetOrder, WithdrawalPolicy
from .clock import MAX_ABS_CLOCK_OFFSET_MS, MAX_RECV_WINDOW_MS, TestnetClock
from .credentials import (
    API_KEY_ENV_VAR,
    API_SECRET_ENV_VAR,
    TESTNET_ENV_VARS,
    TestnetCredentials,
    credentials_from_environment,
    normalize_credential_value,
)
from .endpoints import (
    MAINNET_HOST_MARKERS,
    SPOT_TESTNET_REST_BASE,
    assert_no_mainnet_reference,
    is_testnet_host,
    require_testnet_base_url,
)
from .errors import (
    AmbiguousOrderError,
    AttemptJournalRequiredError,
    BinanceTestnetError,
    ClockDriftError,
    MainnetForbiddenError,
    RateLimitBanError,
    RateLimitError,
    TestnetCredentialsRequiredError,
    TestnetEndpointError,
    TestnetRejectedError,
    TestnetSymbolError,
    TradeBackfillError,
    UnsafeTestnetAccountError,
    UnsupportedOrderTypeError,
    VenueProtocolRejectionError,
)
from .models import (
    STATUS_TO_STATE,
    TestnetAccountState,
    TestnetEnvironment,
    TestnetFill,
    VenueTrade,
    VenueAssetBalance,
    VenueAccountSnapshot,
    MarkEvidence,
    TestnetOrder,
    TestnetOrderRequest,
    TestnetSymbolRules,
    format_decimal,
)
from .rate_limit import RateLimitBudget, RetryPolicy, TokenBucket
from .runtime import TestnetOrderRuntime, TestnetVenueStateSource
from .signing import redact_query_string, sign_query_string, signed_query_string
from .translate import (
    PRODUCER,
    canonical_fill_event_id,
    deterministic_order_uuid,
    testnet_order_to_phase3_events,
    venue_trade_event_id,
)
from .transport import (
    API_KEY_HEADER,
    HttpRequest,
    HttpResponse,
    PreSendTransportError,
    RequestsTransport,
    ScriptedTransport,
    Transport,
    TransportError,
    TransportTimeout,
)

__all__ = [
    "API_KEY_ENV_VAR",
    "API_KEY_HEADER",
    "API_SECRET_ENV_VAR",
    "AmbiguousOrderError",
    "AttemptJournalRequiredError",
    "BackfillEvent",
    "BinanceSpotTestnetClient",
    "BinanceTestnetError",
    "ClockDriftError",
    "HttpRequest",
    "HttpResponse",
    "MAINNET_HOST_MARKERS",
    "MAX_ABS_CLOCK_OFFSET_MS",
    "MAX_RECV_WINDOW_MS",
    "MainnetForbiddenError",
    "PRODUCER",
    "PreSendTransportError",
    "PreparedTestnetOrder",
    "RateLimitBanError",
    "RateLimitBudget",
    "RateLimitError",
    "RequestsTransport",
    "RetryPolicy",
    "SPOT_TESTNET_REST_BASE",
    "STATUS_TO_STATE",
    "ScriptedTransport",
    "TESTNET_ENV_VARS",
    "TestnetAccountState",
    "TestnetClock",
    "TestnetCredentials",
    "TestnetCredentialsRequiredError",
    "TestnetEndpointError",
    "TestnetEnvironment",
    "TestnetFill",
    "VenueTrade",
    "VenueAssetBalance",
    "VenueAccountSnapshot",
    "MarkEvidence",
    "TestnetOrder",
    "TestnetOrderRequest",
    "TestnetOrderRuntime",
    "TestnetRejectedError",
    "TestnetSymbolError",
    "TestnetSymbolRules",
    "TestnetVenueStateSource",
    "TradeBackfillError",
    "TradeBackfillPlan",
    "TokenBucket",
    "Transport",
    "TransportError",
    "TransportTimeout",
    "UnsafeTestnetAccountError",
    "UnsupportedOrderTypeError",
    "VenueProtocolRejectionError",
    "WithdrawalPolicy",
    "assert_no_mainnet_reference",
    "credentials_from_environment",
    "canonical_fill_event_id",
    "deterministic_order_uuid",
    "venue_trade_event_id",
    "format_decimal",
    "is_testnet_host",
    "normalize_credential_value",
    "plan_trade_backfill",
    "redact_query_string",
    "require_testnet_base_url",
    "sign_query_string",
    "signed_query_string",
    "testnet_order_to_phase3_events",
    "trade_backfill_plan_fingerprint",
    "trade_backfill_source_revision",
]
