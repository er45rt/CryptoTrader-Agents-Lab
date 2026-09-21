"""Engine-independent execution boundary (Phase 4).

The application/domain layer depends only on this package. The Nautilus implementation lives
under :mod:`crypto_trader.execution.nautilus` and is imported explicitly by infrastructure
wiring, never by domain code. A regression test enforces that ``crypto_trader.events``,
``crypto_trader.llm``, ``crypto_trader.crew`` and ``crypto_trader.tools`` contain no Nautilus
imports or types.
"""

from .errors import (
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
)

__all__ = [
    "BookLevel",
    "EngineVersionError",
    "ExecutionBoundaryError",
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
    "QueueAwareConfigurationError",
    "RuntimeStateError",
    "TranslationError",
    "UnsupportedExecutionRequestError",
]
