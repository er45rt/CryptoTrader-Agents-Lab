"""D29 strategy plug-in layer: contract, look-ahead safety, loader, data integrity, engine path."""
from __future__ import annotations

import math
import textwrap
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from crypto_trader.strategies import Bar, BarHistory, Signal, Strategy, StrategyError, list_strategies, load_strategy
from crypto_trader.strategies import indicators
from crypto_trader.strategies.fast import run_fast_backtest
from crypto_trader.strategies.ohlcv import bars_from_rows, check_bars, load_bars
from crypto_trader.strategies.runtime import RunSettings, run_backtest

T0 = datetime(2024, 1, 1, tzinfo=timezone.utc)


def synthetic_bars(n: int = 120, tf: str = "4h", seed: int = 7) -> list[Bar]:
    step = timedelta(hours=4) if tf == "4h" else timedelta(hours=1)
    price = 30000.0
    out = []
    x = seed
    for i in range(n):
        x = (x * 1103515245 + 12345) % (2 ** 31)
        drift = math.sin(i / 9.0) * 0.012 + ((x % 1000) / 1000 - 0.5) * 0.01
        o = price
        c = max(100.0, o * (1 + drift))
        h, l = max(o, c) * 1.004, min(o, c) * 0.996
        out.append(Bar(open_time=T0 + i * step, open=Decimal(f"{o:.2f}"), high=Decimal(f"{h:.2f}"),
                       low=Decimal(f"{l:.2f}"), close=Decimal(f"{c:.2f}"), volume=Decimal("10"), timeframe=tf))
        price = c
    return out


class Cross(Strategy):
    name = "test-cross"
    timeframe = "4h"
    warmup_bars = 12

    def on_bar(self, bars):
        f, s = bars.indicator("sma", 4), bars.indicator("sma", 12)
        return Signal.long(1, "up") if f[-1] > s[-1] else Signal.flat("down")


class Hold(Strategy):
    name = "test-hold"
    timeframe = "4h"

    def on_bar(self, bars):
        return Signal.long(1)


# --- contract -----------------------------------------------------------------------------------

def test_signal_is_bounded_spot_long_only():
    with pytest.raises(StrategyError):
        Signal(Decimal("1.5"))
    with pytest.raises(StrategyError):
        Signal(Decimal("-0.1"))
    assert Signal.long(0.5).target == Decimal("0.5")


def test_strategy_class_validation():
    class NoName(Strategy):
        timeframe = "4h"

        def on_bar(self, bars):
            return None

    with pytest.raises(StrategyError):
        NoName.validate_class()

    class NoBody(Strategy):
        name = "x"

    with pytest.raises(StrategyError):
        NoBody.validate_class()


def test_bar_rejects_inconsistent_ohlc():
    with pytest.raises(StrategyError):
        Bar(open_time=T0, open=Decimal(10), high=Decimal(9), low=Decimal(8), close=Decimal(10),
            volume=Decimal(1), timeframe="4h")


# --- look-ahead safety ----------------------------------------------------------------------------

def test_bar_history_refuses_an_unclosed_bar():
    bars = synthetic_bars(5)
    with pytest.raises(StrategyError):
        BarHistory(bars, as_of=bars[-1].close_time - timedelta(seconds=1))
    assert len(BarHistory(bars, as_of=bars[-1].close_time)) == 5


@pytest.mark.parametrize("name,args", [("sma", (5,)), ("ema", (5,)), ("rsi", (7,)), ("atr", (5,)),
                                        ("donchian_high", (6,)), ("bollinger_lower", (8,)), ("macd_signal", ()),
                                        ("returns", (3,))])
def test_indicators_are_causal(name, args):
    bars = synthetic_bars(80)
    full = indicators.compute(name, BarHistory(bars, bars[-1].close_time), *args)
    for cut in (20, 41, 63):
        part = indicators.compute(name, BarHistory(bars[:cut], bars[cut - 1].close_time), *args)
        for a, b in zip(part, full[:cut]):
            assert (math.isnan(a) and math.isnan(b)) or a == pytest.approx(b, rel=1e-12)


# --- data integrity --------------------------------------------------------------------------------

def test_check_bars_detects_gaps_and_order():
    bars = synthetic_bars(10)
    with pytest.raises(StrategyError):
        check_bars(bars[:3] + bars[4:])
    check_bars(bars[:3] + bars[4:], allow_gaps=True)
    with pytest.raises(StrategyError):
        check_bars([bars[1], bars[0]])


def test_load_bars_hard_cutoff(tmp_path):
    rows = [{"date": b.open_time.isoformat(), "open": b.open, "high": b.high, "low": b.low, "close": b.close,
             "volume": b.volume} for b in synthetic_bars(30)]
    path = tmp_path / "d.csv"
    import csv
    with path.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    cutoff = T0 + timedelta(hours=4 * 20)
    bars = load_bars(path, "4h", not_after=cutoff)
    assert len(bars) == 20 and all(b.open_time < cutoff for b in bars)


def test_naive_timestamps_are_refused():
    with pytest.raises(StrategyError):
        bars_from_rows([{"date": "2024-01-01T00:00:00", "open": 1, "high": 1, "low": 1, "close": 1, "volume": 0}], "4h")


# --- loader (install / remove / swap without engine edits) ---------------------------------------

def test_loader_file_and_class_specs(tmp_path):
    f = tmp_path / "mine.py"
    f.write_text(textwrap.dedent("""
        from crypto_trader.strategies import Signal, Strategy
        class A(Strategy):
            name = "a"; timeframe = "4h"
            def on_bar(self, bars): return Signal.flat()
        class B(Strategy):
            name = "b"; timeframe = "1h"
            def on_bar(self, bars): return None
    """))
    with pytest.raises(StrategyError):
        load_strategy(str(f))                        # two classes -> must name one
    assert load_strategy(f"{f}:B").cls.name == "b"
    loaded = load_strategy(f"{f}:A")
    assert loaded.source_sha256 and loaded.instantiate().name == "a"
    names = sorted(x["name"] for x in list_strategies(tmp_path) if x["ok"])
    assert names == ["a", "b"]
    f.unlink()                                       # removal = delete the file
    assert list_strategies(tmp_path) == []


def test_loader_reports_broken_files(tmp_path):
    (tmp_path / "bad.py").write_text("raise RuntimeError('boom')\n")
    (tmp_path / "noclass.py").write_text("x = 1\n")
    result = list_strategies(tmp_path)
    assert all(not x.get("ok", True) for x in result if x["spec"].endswith("bad.py"))
    with pytest.raises(StrategyError):
        load_strategy(str(tmp_path / "noclass.py"))


def test_wrong_return_type_is_refused():
    class Bad(Strategy):
        name = "bad"
        timeframe = "4h"

        def on_bar(self, bars):
            return 1

    with pytest.raises(StrategyError):
        run_fast_backtest(Bad(), synthetic_bars(20))


def test_timeframe_mismatch_is_refused():
    with pytest.raises(StrategyError):
        run_fast_backtest(Cross(), synthetic_bars(40, tf="1h"))


# --- engine path ------------------------------------------------------------------------------------

def test_engine_and_fast_preview_agree_exactly(tmp_path):
    bars = synthetic_bars(90)
    settings = RunSettings(capital_quote=Decimal("1000"))
    for strat in (Cross, Hold):
        engine = run_backtest(strat(), bars, settings=settings, out_dir=tmp_path / strat.name)
        fast = run_fast_backtest(strat(), bars, settings=settings)
        assert engine.metrics["fills"] == fast.metrics["fills"] > 0
        # the engine rounds each fee to the quote precision (8 dp); the preview does not
        assert engine.metrics["fees_quote"] == pytest.approx(fast.metrics["fees_quote"], abs=1e-6)
        for (ta, a), (tb, b) in zip(engine.equity_curve, fast.equity_curve):
            assert ta == tb and a == pytest.approx(b, abs=1e-6)
        assert Decimal(engine.metrics["engine_equity_quote"]) == pytest.approx(
            Decimal(str(engine.metrics["final_equity_quote"])), abs=Decimal("0.000001"))
        assert (tmp_path / strat.name / "backtest_result.json").exists()
        assert engine.metrics["risk_counts"].get("allowed", 0) == engine.metrics["fills"]


def test_buy_and_hold_through_engine_makes_one_fill_and_charges_fees(tmp_path):
    bars = synthetic_bars(60)
    r = run_backtest(Hold(), bars, settings=RunSettings(capital_quote=Decimal("500")), out_dir=tmp_path)
    assert r.metrics["fills"] == 1 and r.metrics["fees_quote"] > 0
    assert r.benchmark["net_return"] == pytest.approx(r.metrics["net_return"], abs=0.02)


def test_engine_hard_risk_is_in_the_path(tmp_path):
    """An engine order-size limit below the strategy's order size must make hard risk reject it."""
    bars = synthetic_bars(90)
    tight = run_backtest(Cross(), bars, settings=RunSettings(
        capital_quote=Decimal("1000"), risk_overrides={"max_order_notional_quote": "100"}), out_dir=tmp_path / "t")
    loose = run_backtest(Cross(), bars, settings=RunSettings(capital_quote=Decimal("1000")), out_dir=tmp_path / "l")
    assert tight.metrics["risk_counts"].get("rejected", 0) > 0 and tight.metrics["fills"] == 0
    assert loose.metrics["risk_counts"].get("rejected", 0) == 0 and loose.metrics["fills"] > 0


def test_output_folder_is_never_reused(tmp_path):
    run_backtest(Hold(), synthetic_bars(20), out_dir=tmp_path)
    with pytest.raises(StrategyError):
        run_backtest(Hold(), synthetic_bars(20), out_dir=tmp_path)
