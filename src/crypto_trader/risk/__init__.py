"""Deterministic, non-LLM Hard Risk engine (Phase 7).

Engine-independent: no Nautilus, CrewAI, or LLM imports. Units are explicit everywhere:
``*_quote`` values are quote-currency Decimals, ``*_pct`` values are percent Decimals, counts
are integers. Missing mandatory configuration fails closed.
"""

from .engine import HardRiskEngine, RiskDecision, RiskRequest, RiskRuleResult
from .errors import RiskConfigError, RiskError, RiskStateError
from .journaling import record_risk_decision
from .limits import RiskLimits, load_limits
from .state import PendingIntent, PendingIntentRegistry, RiskStateSnapshot, Side

__all__ = [
    "HardRiskEngine",
    "PendingIntent",
    "PendingIntentRegistry",
    "RiskConfigError",
    "RiskDecision",
    "RiskError",
    "RiskLimits",
    "RiskRequest",
    "RiskRuleResult",
    "RiskStateError",
    "RiskStateSnapshot",
    "Side",
    "load_limits",
    "record_risk_decision",
]
