"""Demo plug-in for proving live PAPER trading quickly: SMA(5)/SMA(15) cross on 5-minute bars. Not for profit."""
from crypto_trader.strategies import Signal, Strategy


class SmaCross5m(Strategy):
    name = "sma-cross-5m-demo"
    version = "1"
    timeframe = "5m"
    warmup_bars = 15

    def on_bar(self, bars):
        fast = bars.indicator("sma", 5)
        slow = bars.indicator("sma", 15)
        return Signal.long(1, "fast>slow") if fast[-1] > slow[-1] else Signal.flat("fast<=slow")
