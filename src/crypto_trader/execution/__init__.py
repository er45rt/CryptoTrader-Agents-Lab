"""Engine-independent execution boundary (Phase 4).

The application/domain layer depends only on this package. The Nautilus implementation lives
under :mod:`crypto_trader.execution.nautilus` and is imported explicitly by infrastructure
wiring, never by domain code. A regression test enforces that ``crypto_trader.events``,
``crypto_trader.llm``, ``crypto_trader.crew`` and ``crypto_trader.tools`` contain no Nautilus
imports or types.
"""

from .errors import (
    NonDurableExecutionError,
    EngineVersionError,
    ExecutionBoundaryError,
    QueueAwareConfigurationError,
    RuntimeStateError,
    TranslationError,
    UnsupportedExecutionRequestError,
)
from .fake import InMemoryExecutionPort
from .ports import (
    BookLevel,
    DurableAttemptJournal,
    DurableVenueWritePort,
    ExecutionCapability,
    ExecutionObservation,
    ExecutionOrderType,
    ExecutionPort,
    ExecutionRequest,
    ExecutionRuntimeState,
    ExecutionSide,
    ExecutionTimeInForce,
    InstrumentSpec,
    MarketBookUpdate,
    MarketTrade,
    OrderLifecycleState,
    PreparedVenueWrite,
)

__all__ = [
    "BookLevel",
    "DurableAttemptJournal",
    "DurableVenueWritePort",
    "EngineVersionError",
    "ExecutionBoundaryError",
    "ExecutionCapability",
    "ExecutionObservation",
    "ExecutionOrderType",
    "ExecutionPort",
    "ExecutionRequest",
    "ExecutionRuntimeState",
    "ExecutionSide",
    "ExecutionTimeInForce",
    "InMemoryExecutionPort",
    "InstrumentSpec",
    "MarketBookUpdate",
    "MarketTrade",
    "OrderLifecycleState",
    "PreparedVenueWrite",
    "QueueAwareConfigurationError",
    "NonDurableExecutionError",
    "RuntimeStateError",
    "TranslationError",
    "UnsupportedExecutionRequestError",
]
