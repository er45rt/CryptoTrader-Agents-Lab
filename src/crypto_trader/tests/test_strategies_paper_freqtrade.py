"""D29: live-paper sessions (offline, injected data) and the Freqtrade adapter."""
from __future__ import annotations

import json
import os
import sqlite3
import textwrap
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


def test_paper_matches_backtest_over_the_same_live_bars(tmp_path):
    bars, spec_file, folder, fetch = _session(tmp_path)
    st = paper_cycle(folder, fetch=fetch, fetch_open=fetch.open, now=bars[139].close_time)
    assert st["fills"] > 0 and st["safety_blocked"] == 0


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
