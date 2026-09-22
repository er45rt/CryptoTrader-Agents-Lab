"""Shadow and paper trading modes (Phase 10).

Engine-independent: nothing in this package imports Nautilus, CrewAI, the LLM layer or a venue
client. The simulated execution substrate is injected through the
:class:`~crypto_trader.paper.simulation.ExecutionSimulator` protocol, whose Nautilus
implementation lives in ``crypto_trader.execution.nautilus``.

Both modes run the real decision path (public market data -> injected AI/strategy -> validity ->
deterministic hard risk -> safety gate) and journal every stage. SHADOW stops before order
placement and records what would have happened; PAPER places only into the simulated substrate.
ZERO authenticated exchange trading: no credential, no signature, no live venue.
"""

from .accounting import PositionLedger, PositionState
from .config import SessionConfig, load_session_config
from .decisions import (
    DecisionSource,
    ScriptedDecisionSequence,
    ScriptedDecisionSource,
    StrategyDecision,
)
from .errors import PaperConfigError, PaperError, PaperStateError, SimulationError
from .feed import (
    FrozenMarketDataFeed,
    MarketDataFeed,
    MarketObservation,
    PublicMarketDataFeed,
)
from .modes import (
    QUEUE_AWARE_BOOK_TYPE,
    QueueAwareSettings,
    TradingMode,
    require_tested_queue_aware_settings,
    tested_queue_aware_settings,
)
from .report import DecisionRecord, FillSummary, OrderSummary, RunReport, report_from_journal
from .session import TradingSession
from .simulation import ExecutionSimulator, ScriptedSimulationRunner, SimulationOutcome

__all__ = [
    "DecisionRecord",
    "DecisionSource",
    "ExecutionSimulator",
    "FillSummary",
    "FrozenMarketDataFeed",
    "MarketDataFeed",
    "MarketObservation",
    "OrderSummary",
    "PaperConfigError",
    "PaperError",
    "PaperStateError",
    "PositionLedger",
    "PositionState",
    "PublicMarketDataFeed",
    "QUEUE_AWARE_BOOK_TYPE",
    "QueueAwareSettings",
    "RunReport",
    "ScriptedDecisionSequence",
    "ScriptedDecisionSource",
    "ScriptedSimulationRunner",
    "SessionConfig",
    "SimulationError",
    "SimulationOutcome",
    "StrategyDecision",
    "TradingMode",
    "TradingSession",
    "load_session_config",
    "report_from_journal",
    "require_tested_queue_aware_settings",
    "tested_queue_aware_settings",
]
