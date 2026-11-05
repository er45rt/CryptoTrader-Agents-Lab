"""D29 strategy plug-in layer: contract, look-ahead safety, loader, data integrity, engine path."""
from __future__ import annotations

import json
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


def test_shared_view_matches_plain_history_and_refuses_unclosed_bars():
    from crypto_trader.strategies.api import _SharedSeries

    bars = synthetic_bars(80)
    shared = _SharedSeries(bars)
    for cut in (20, 41, 80):
        view = BarHistory._view(shared, cut, bars[cut - 1].close_time)
        plain = BarHistory(bars[:cut], bars[cut - 1].close_time)
        assert view.bars == plain.bars and view.close == plain.close
        for name, args in (("sma", (5,)), ("rsi", (7,)), ("macd_signal", ())):
            for a, b in zip(view.indicator(name, *args), plain.indicator(name, *args), strict=True):
                assert (math.isnan(a) and math.isnan(b)) or a == pytest.approx(b, rel=1e-12)
        shared.learn(view)                                   # later views get these as prefixes
        again = BarHistory._view(shared, cut, bars[cut - 1].close_time)
        assert again.indicator("sma", 5) == view.indicator("sma", 5)
    with pytest.raises(StrategyError):
        BarHistory._view(shared, 30, bars[29].close_time - timedelta(seconds=1))


def test_sol_d30_1_an_earlier_unclosed_bar_is_refused():
    """Sol D30 r1 #1: a 1d bar followed by 1h bars; at 02:00 the 1d bar (closing next day) is still open."""
    from crypto_trader.strategies.api import _SharedSeries

    def bar(t, tf):
        return Bar(open_time=T0 + t, open=Decimal(100), high=Decimal(101), low=Decimal(99), close=Decimal(100),
                   volume=Decimal(1), timeframe=tf)

    mixed = [bar(timedelta(0), "1d"), bar(timedelta(hours=1), "1h"), bar(timedelta(hours=2), "1h")]
    shared = _SharedSeries(mixed)
    for n in (2, 3):
        with pytest.raises(StrategyError):
            BarHistory._view(shared, n, mixed[n - 1].close_time)
        with pytest.raises(StrategyError):
            BarHistory(mixed[:n], mixed[n - 1].close_time)          # same answer as the plain constructor

    class Daily(Strategy):
        name = "daily"
        timeframe = "1d"
        warmup_bars = 2
        seen: list = []

        def on_bar(self, bars):
            Daily.seen.append(bars.bars)
            return Signal.flat()

    with pytest.raises(StrategyError):          # Sol's end-to-end reproduction: refused, nothing delivered
        run_fast_backtest(Daily(), mixed)
    assert Daily.seen == []


def test_sol_d30_r2_bars_out_of_order_are_refused():
    """Sol D30 r2: reversed bars must be refused, never filled before the decision."""
    from crypto_trader.strategies.api import _SharedSeries

    bars = synthetic_bars(5)
    with pytest.raises(StrategyError):
        _SharedSeries([bars[1], bars[0]])
    with pytest.raises(StrategyError):
        _SharedSeries([bars[0], bars[0]])

    class Always(Strategy):
        name = "always"
        timeframe = "4h"

        def on_bar(self, bars):
            return Signal.long(1)

    with pytest.raises(StrategyError):
        run_fast_backtest(Always(), [bars[1], bars[0]])


def test_sol_d30_2_the_view_given_to_a_strategy_holds_nothing_from_later_bars():
    """Sol D30 r1 #2: no attribute of the view may reach the full-run series."""
    from crypto_trader.strategies.api import _SharedSeries

    bars = synthetic_bars(50)
    shared = _SharedSeries(bars)
    warm = BarHistory._view(shared, 50, bars[-1].close_time)
    warm.indicator("sma", 5)
    assert warm.close
    shared.learn(warm)                                       # full-run arrays now exist engine-side
    view = BarHistory._view(shared, 2, bars[1].close_time)
    # _read / _depth (D31 decision trace) hold only the names of keys the strategy asked for and a counter
    assert set(vars(view)) == {"_bars", "as_of", "_cache", "_read", "_depth"}
    assert view._read == {} and view._depth == 0
    assert len(view._bars) == 2 and all(len(v) == 2 for v in view._cache.values())
    assert not any(isinstance(v, _SharedSeries) for v in vars(view).values())


def test_shared_view_never_slices_a_non_builtin_indicator(monkeypatch):
    from crypto_trader.strategies.api import _SharedSeries

    bars = synthetic_bars(40)
    peek = lambda b, *a: tuple(b.close[-1] for _ in b.close)   # deliberately non-causal  # noqa: E731
    monkeypatch.setitem(indicators.REGISTRY, "peek", peek)
    shared = _SharedSeries(bars)
    view = BarHistory._view(shared, 10, bars[9].close_time)
    assert view.indicator("peek")[0] == view.close[-1]      # computed on the 10 visible bars only
    view.indicator("sma", 3)
    shared.learn(view)
    later = BarHistory._view(shared, 10, bars[9].close_time)
    assert ("peek", (), ()) not in later._cache             # never computed over the whole run
    monkeypatch.setitem(indicators.REGISTRY, "sma", peek)   # built-in replaced after it was learned
    later = BarHistory._view(shared, 10, bars[9].close_time)
    assert later.indicator("sma", 3)[0] == later.close[-1]  # not served from the full-run cache


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
        # the engine marks equity at its last observation = the second-to-last bar (the last bar's
        # decision has no later book to execute against, so it is not observed)
        assert float(engine.metrics["engine_equity_quote"]) == pytest.approx(engine.equity_curve[-2][1], abs=1e-6)
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


# --- D31 decision trace -----------------------------------------------------------------------------------

def test_trace_records_only_what_the_strategy_itself_read():
    bars = synthetic_bars(30)
    view = BarHistory(bars, bars[-1].close_time)
    view.indicator("sma", 5)               # sma reads close internally: that must NOT count as the strategy's read
    assert list(view._read) == [("sma", (5,), ())]
    assert view.close[-1] > 0              # an explicit read does count
    trace = view._trace()
    assert list(trace) == ["sma(5)", "close"]
    assert trace["sma(5)"] == pytest.approx(sum(float(b.close) for b in bars[-5:]) / 5)
    early = BarHistory(bars[:3], bars[2].close_time)
    early.indicator("sma", 5)
    assert early._trace() == {"sma(5)": None}   # not enough data yet: no value is invented


def test_engine_backtest_writes_the_decision_chain_and_traces(tmp_path):
    bars = synthetic_bars(120)
    out = tmp_path / "run"
    run_backtest(Cross(), bars, settings=RunSettings(capital_quote=Decimal("1000")), out_dir=out)
    chain = json.loads((out / "decisions.json").read_text())
    traces = [json.loads(l) for l in (out / "decision_trace.jsonl").read_text().splitlines()]
    assert {"decisions", "orders", "fills"} <= set(chain)
    by_ref = {t["ref"]: t for t in traces}
    decided = [d for d in chain["decisions"] if d["rationale"] != "warmup"]
    assert decided and all(d["market_reference"] in by_ref for d in decided)
    assert not any(d["market_reference"] in by_ref for d in chain["decisions"] if d["rationale"] == "warmup")
    for d in decided:                        # the trace agrees with the journal-derived decision, field by field
        t = by_ref[d["market_reference"]]
        assert (t["action"], t["rationale"]) == (d["action"], d["rationale"])
        assert t["ai_involved"] is False and t["decision_source"] == "strategy-plugin"
        assert set(t["read"]) == {"sma(4)", "sma(12)"}
    # values are the ones the strategy saw on that bar (prefix only), not values computed later over the full run
    k = next(i for i, b in enumerate(bars) if b.open_time.isoformat() == traces[10]["bar_open"])
    expect = sum(float(b.close) for b in bars[k - 3:k + 1]) / 4
    assert traces[10]["read"]["sma(4)"] == pytest.approx(expect)
    if (lines := (out / "decision_trace.jsonl").read_text().splitlines()):
        assert len(lines) == len({json.loads(l)["ref"] for l in lines})


def test_trace_writer_is_append_only_and_flags_a_changed_replay(tmp_path):
    from crypto_trader.strategies.runtime import write_decision_traces

    path = tmp_path / "t.jsonl"
    a = {"ref": "r1", "signal": {"target": "1", "reason": "up"}, "read": {"sma(4)": 1.5}, "action": "buy",
         "rationale": "strategy:up", "quantity": "0.1", "tradable": True, "price": "100"}
    assert write_decision_traces(path, [a], recorded_at="t0")["appended"] == 1
    again = write_decision_traces(path, [a], recorded_at="t1")
    assert again == {"appended": 0, "mismatches": [], "mismatch_count": 0}
    changed = dict(a, read={"sma(4)": 9.9})
    res = write_decision_traces(path, [changed], recorded_at="t2")
    assert res["appended"] == 0 and res["mismatches"] == ["r1"]
    rows = [json.loads(l) for l in path.read_text().splitlines()]
    assert len(rows) == 1 and rows[0]["read"] == {"sma(4)": 1.5} and rows[0]["recorded_at"] == "t0"   # never rewritten
    path.write_text(path.read_text() + '{"ref": "r2", "tor')                                          # torn tail
    assert write_decision_traces(path, [dict(a, ref="r3")], recorded_at="t3")["appended"] == 1
    assert "error" in write_decision_traces(tmp_path, [a], recorded_at="t4")                          # a folder: reported, not raised
