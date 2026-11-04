"""Strategy plug-in layer (D29): write a Strategy subclass, load it by spec, backtest it, paper-trade it.

The engine core (paper, risk, validity, safety, execution, persistence) is used unchanged.
See STRATEGIES_GUIDE.md at the repository root.
"""
from .api import TIMEFRAMES, Bar, BarHistory, Signal, Strategy, StrategyError
from .loader import LoadedStrategy, list_strategies, load_strategy

__all__ = ["TIMEFRAMES", "Bar", "BarHistory", "Signal", "Strategy", "StrategyError", "LoadedStrategy",
           "list_strategies", "load_strategy"]
