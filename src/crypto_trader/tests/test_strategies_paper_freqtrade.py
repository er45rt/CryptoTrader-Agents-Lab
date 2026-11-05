"""D29: live-paper sessions (offline, injected data) and the Freqtrade adapter."""
from __future__ import annotations

import json
import os
import sqlite3
import textwrap
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from crypto_trader.strategies import StrategyError, load_strategy
from crypto_trader.strategies.__main__ import main as cli
from crypto_trader.strategies.fast import run_fast_backtest
from crypto_trader.strategies.freqtrade_adapter import DEFAULT_FT_PYTHON
from crypto_trader.strategies.paper_live import create_paper_session, paper_cycle
from crypto_trader.strategies.runtime import MarketSpec, RunSettings

from crypto_trader.tests.test_strategies_plugin import synthetic_bars

STRAT = textwrap.dedent("""
    from crypto_trader.strategies import Signal, Strategy
    class Cross(Strategy):
        name = "paper-cross"; timeframe = "4h"; warmup_bars = 12
        def on_bar(self, bars):
            f, s = bars.indicator("sma", 4), bars.indicator("sma", 12)
            return Signal.long(1) if f[-1] > s[-1] else Signal.flat()
""")


def _session(tmp_path: Path):
    bars = synthetic_bars(140)
    spec_file = tmp_path / "cross.py"
    spec_file.write_text(STRAT)
    folder = tmp_path / "session"
    create_paper_session(folder, strategy_spec=str(spec_file), market=MarketSpec(),
                         settings=RunSettings(capital_quote=Decimal("1000")), now=bars[60].close_time)

    def fetch(symbol, tf, *, start, now, cafile=None):
        return [b for b in bars if b.open_time >= start and b.close_time <= now]

    def fetch_open(symbol, tf, *, now, cafile=None):
        forming = [b for b in bars if b.open_time <= now < b.close_time]
        return (forming[0].open_time, forming[0].open) if forming else None

    fetch.open = fetch_open
    return bars, spec_file, folder, fetch


def _events(folder: Path) -> int:
    return sqlite3.connect(folder / "journal.db").execute("select count(*) from events").fetchone()[0]


def test_paper_warmup_never_trades_and_replay_is_idempotent(tmp_path):
    bars, _, folder, fetch = _session(tmp_path)
    first = paper_cycle(folder, fetch=fetch, fetch_open=fetch.open, now=bars[60].close_time)
    assert first["live_bars"] == 0 and "fills" not in first          # only history so far
    second = paper_cycle(folder, fetch=fetch, fetch_open=fetch.open, now=bars[90].close_time)
    n = _events(folder)
    again = paper_cycle(folder, fetch=fetch, fetch_open=fetch.open, now=bars[90].close_time)  # restart / repeat: nothing new
    assert again["new_bars"] == 0 and _events(folder) == n
    assert again["fills"] == second["fills"] and again["equity_quote"] == second["equity_quote"]
    later = paper_cycle(folder, fetch=fetch, fetch_open=fetch.open, now=bars[139].close_time)
    assert later["live_bars"] == 139 - 60 and later["fills"] >= second["fills"]
    lines = (folder / "bars.jsonl").read_text().splitlines()
    assert len(lines) == len({json.loads(l)["date"] for l in lines})   # append-only, no duplicates


def test_paper_trades_when_every_bar_is_seen_on_time(tmp_path):
    bars, spec_file, folder, fetch = _session(tmp_path)
    for k in range(60, 110):                    # one cycle per bar, right after it closes
        st = paper_cycle(folder, fetch=fetch, fetch_open=fetch.open, now=bars[k].close_time + timedelta(minutes=1))
    assert st["fills"] > 0 and st["safety_blocked"] == 0 and st["missed_bars"] == 0


def test_bars_first_seen_late_are_missed_never_traded_in_the_past(tmp_path):
    bars, spec_file, folder, fetch = _session(tmp_path)
    paper_cycle(folder, fetch=fetch, fetch_open=fetch.open, now=bars[60].close_time)
    # the process was down for ~30 bars: they arrive together, long after their execution windows
    st = paper_cycle(folder, fetch=fetch, fetch_open=fetch.open, now=bars[100].close_time + timedelta(minutes=1))
    assert st["missed_bars"] >= 30
    report = json.loads((folder / "report.json").read_text())
    missed = [d for d in report["decisions"] if d["rationale"] == "missed:bar-arrived-late"]
    assert len(missed) >= 30
    traded = [d for d in report["decisions"] if d["action"] in {"buy", "sell"}]
    late_refs = {d["market_reference"] for d in missed}
    assert not any(d["market_reference"] in late_refs for d in traded)


def test_paper_refuses_a_changed_strategy_file(tmp_path):
    bars, spec_file, folder, fetch = _session(tmp_path)
    paper_cycle(folder, fetch=fetch, fetch_open=fetch.open, now=bars[70].close_time)
    spec_file.write_text(STRAT + "\n# edited\n")
    with pytest.raises(StrategyError):
        paper_cycle(folder, fetch=fetch, fetch_open=fetch.open, now=bars[80].close_time)


def test_paper_session_folder_is_never_reused(tmp_path):
    _, spec_file, folder, _ = _session(tmp_path)
    with pytest.raises(StrategyError):
        create_paper_session(folder, strategy_spec=str(spec_file), market=MarketSpec(), settings=RunSettings())


def test_cli_check_and_list(tmp_path, capsys):
    f = tmp_path / "cross.py"
    f.write_text(STRAT)
    assert cli(["check", str(f)]) == 0 and '"paper-cross"' in capsys.readouterr().out
    assert cli(["list", str(tmp_path)]) == 0 and "paper-cross" in capsys.readouterr().out
    assert cli(["check", str(tmp_path / "missing.py")]) == 2


# --- Freqtrade adapter (needs a Python with freqtrade; skipped otherwise) ---------------------------

FT_PY = os.environ.get("CT_FREQTRADE_PYTHON", DEFAULT_FT_PYTHON)
needs_ft = pytest.mark.skipif(not Path(FT_PY).is_file(), reason="no Freqtrade Python available")

FT_BASE = textwrap.dedent("""
    from freqtrade.strategy import IStrategy
    class {name}(IStrategy):
        INTERFACE_VERSION = 3
        timeframe = "4h"
        stoploss = -0.99
        minimal_roi = {{"0": 10}}
        startup_candle_count = 3
        {extra}
        def populate_indicators(self, df, md):
            return df
        def populate_entry_trend(self, df, md):
            df.loc[{entry}, "enter_long"] = 1
            return df
        def populate_exit_trend(self, df, md):
            df.loc[{exit}, "exit_long"] = 1
            return df
""")


def _ft(tmp_path, name, entry, exit_, extra=""):
    f = tmp_path / f"{name}.py"
    f.write_text(FT_BASE.format(name=name, entry=entry, exit=exit_, extra=extra))
    return f"freqtrade:{f}:{name}"


@needs_ft
def test_freqtrade_adapter_runs_an_unmodified_strategy(tmp_path):
    spec = _ft(tmp_path, "Momo", 'df["close"] > df["close"].shift(1)', 'df["close"] < df["close"].shift(1)')
    loaded = load_strategy(spec)
    assert loaded.cls.timeframe == "4h" and loaded.cls.name == "freqtrade-Momo"
    r = run_fast_backtest(loaded.instantiate(), synthetic_bars(60), settings=RunSettings(capital_quote=Decimal("1000")))
    assert r.metrics["fills"] > 0


@needs_ft
def test_freqtrade_adapter_blocks_look_ahead(tmp_path):
    """A strategy that peeks at the NEXT close must not profit: the adapter only reads the last row."""
    cheat = _ft(tmp_path, "Cheat", 'df["close"].shift(-1) > df["close"]', 'df["close"].shift(-1) < df["close"]')
    r = run_fast_backtest(load_strategy(cheat).instantiate(), synthetic_bars(60),
                          settings=RunSettings(capital_quote=Decimal("1000")))
    assert r.metrics["fills"] == 0                    # the future is never visible on the last row


@needs_ft
@pytest.mark.parametrize("extra", ["can_short = True",
                                   "def custom_stoploss(self, *a, **k):\n            return -0.1"])
def test_freqtrade_adapter_refuses_unsupported_features(tmp_path, extra):
    spec = _ft(tmp_path, "Unsup", 'df["close"] > 0', 'df["close"] < 0', extra=extra)
    with pytest.raises(StrategyError):
        load_strategy(spec)


# --- Sol D29 round-1 regressions -------------------------------------------------------------------

def test_tradable_window_is_strict_and_short():
    from crypto_trader.strategies.paper_live import MAX_SIGNAL_LATENCY, _tradable
    bars = synthetic_bars(3)
    b = bars[0]
    step = b.close_time - b.open_time
    window = min(step, MAX_SIGNAL_LATENCY)
    seen = [b.close_time + window - timedelta(seconds=1), b.close_time + window, b.close_time + step]
    assert _tradable([b, b, b], seen) == [True, False, False]       # exact boundary is NOT tradable
    assert _tradable([b], [None]) == [False]                          # unknown arrival time: fail closed
    assert _tradable([b], [b.close_time - timedelta(seconds=1)]) == [False]   # seen before it closed


def test_bar_seen_exactly_one_bar_late_never_trades(tmp_path):
    """Sol's reproduction: two closed bars both first seen at the second bar's close."""
    bars, spec_file, folder, fetch = _session(tmp_path)
    for k in range(60, 80):
        paper_cycle(folder, fetch=fetch, fetch_open=fetch.open, now=bars[k].close_time + timedelta(minutes=1))
    st = paper_cycle(folder, fetch=fetch, fetch_open=fetch.open, now=bars[81].close_time)   # bar 80 seen 1 bar late
    report = json.loads((folder / "report.json").read_text())
    by_ref = {d["market_reference"]: d for d in report["decisions"]}
    stamp = bars[80].open_time.strftime("@%Y%m%dT%H%M%SZ")
    late = [r for r in by_ref if r.endswith(stamp)]
    assert late and by_ref[late[0]]["rationale"] == "missed:bar-arrived-late"
    assert st["missed_bars"] >= 1


def test_truncated_bars_tail_is_dropped_but_inner_corruption_refuses(tmp_path):
    bars, spec_file, folder, fetch = _session(tmp_path)
    paper_cycle(folder, fetch=fetch, fetch_open=fetch.open, now=bars[62].close_time + timedelta(minutes=1))
    path = folder / "bars.jsonl"
    good = path.read_text()
    path.write_text(good + '{"date": "2024-01-11T08:00:00+00:00", "op')        # crash mid-append
    paper_cycle(folder, fetch=fetch, fetch_open=fetch.open, now=bars[63].close_time + timedelta(minutes=1))
    assert all(json.loads(l) for l in path.read_text().splitlines())
    lines = path.read_text().splitlines()
    lines[3] = "not json"
    path.write_text("\n".join(lines) + "\n")
    with pytest.raises(StrategyError):
        paper_cycle(folder, fetch=fetch, fetch_open=fetch.open, now=bars[64].close_time + timedelta(minutes=1))


TRAIL = textwrap.dedent("""
    from freqtrade.strategy import IStrategy
    class Trail(IStrategy):
        INTERFACE_VERSION = 3
        timeframe = "4h"
        stoploss = -0.10
        trailing_stop = True
        minimal_roi = {"0": 10}
        startup_candle_count = 1
        def populate_indicators(self, df, md):
            return df
        def populate_entry_trend(self, df, md):
            df.loc[df["close"] == 100, "enter_long"] = 1
            return df
        def populate_exit_trend(self, df, md):
            return df
""")


@needs_ft
def test_freqtrade_trailing_peak_starts_at_entry_not_at_the_signal_close(tmp_path):
    """Sol's reproduction: signal close 100, next open 50, next close 55, 10% trailing -> no exit."""
    from crypto_trader.strategies import Bar, BarHistory
    f = tmp_path / "Trail.py"
    f.write_text(TRAIL)
    strat = load_strategy(f"freqtrade:{f}:Trail").instantiate()
    t0 = synthetic_bars(1)[0].open_time
    step = timedelta(hours=4)
    rows = [(80, 80, 80, 80), (90, 100, 90, 100), (50, 56, 50, 55), (55, 56, 54, 55)]
    bars = [Bar(open_time=t0 + i * step, open=Decimal(o), high=Decimal(h), low=Decimal(l), close=Decimal(c),
                volume=Decimal(1), timeframe="4h") for i, (o, h, l, c) in enumerate(rows)]
    signals = [strat.on_bar(BarHistory(bars[: i + 1], bars[i].close_time)) for i in range(len(bars))]
    assert signals[1].reason == "ft:enter_long"
    assert all(s.target == 1 for s in signals[2:]), [s.reason for s in signals]


def test_paper_decision_trace_is_written_once_per_bar_and_matches_the_journal(tmp_path):
    """D31: the decision center's per-bar evidence is recorded while deciding, append-only, and agrees with
    the journal-derived report; replaying the same bars (every cycle) adds nothing and reports no mismatch."""
    bars, _, folder, fetch = _session(tmp_path)
    paper_cycle(folder, fetch=fetch, fetch_open=fetch.open, now=bars[60].close_time)
    for k in range(61, 75):
        st = paper_cycle(folder, fetch=fetch, fetch_open=fetch.open, now=bars[k].close_time + timedelta(minutes=1))
    st = paper_cycle(folder, fetch=fetch, fetch_open=fetch.open, now=bars[90].close_time + timedelta(minutes=1))  # 15 late bars
    path = folder / "decision_trace.jsonl"
    rows = [json.loads(l) for l in path.read_text().splitlines()]
    assert len(rows) == len({r["ref"] for r in rows})
    assert st["trace"].get("mismatch_count") == 0 and "error" not in st["trace"]
    before = path.read_text()
    again = paper_cycle(folder, fetch=fetch, fetch_open=fetch.open, now=bars[90].close_time + timedelta(minutes=2))
    assert path.read_text() == before and again["trace"]["appended"] == 0
    report = json.loads((folder / "report.json").read_text())
    by_ref = {r["ref"]: r for r in rows}
    live = [d for d in report["decisions"] if d["rationale"] not in ("warmup", "pre-session-history")]
    assert live and all(d["market_reference"] in by_ref for d in live)
    for d in live:
        t = by_ref[d["market_reference"]]
        assert (t["action"], t["rationale"]) == (d["action"], d["rationale"]) and t["ai_involved"] is False
        if d["rationale"] == "missed:bar-arrived-late":
            assert t["tradable"] is False
    assert any(t["tradable"] is False for t in rows) and any(t["tradable"] for t in rows)
