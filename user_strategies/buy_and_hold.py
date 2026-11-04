"""Example plug-in: buy once and hold. Useful as a sanity check of the pipeline."""
from crypto_trader.strategies import Signal, Strategy


class BuyAndHold(Strategy):
    name = "buy-and-hold"
    version = "1"
    timeframe = "4h"
    warmup_bars = 1

    def on_bar(self, bars):
        return Signal.long(1, "hold")
