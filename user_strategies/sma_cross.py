"""Example plug-in: long when the fast SMA is above the slow SMA. For demonstration, not for profit."""
from crypto_trader.strategies import Signal, Strategy


class SmaCross(Strategy):
    name = "sma-cross"
    version = "1"
    timeframe = "4h"
    warmup_bars = 50
    description = "Long when SMA(fast) > SMA(slow), else flat."

    def on_bar(self, bars):
        fast = bars.indicator("sma", int(self.params.get("fast", 20)))
        slow = bars.indicator("sma", int(self.params.get("slow", 50)))
        return Signal.long(1, "fast>slow") if fast[-1] > slow[-1] else Signal.flat("fast<=slow")
