"""Demo mode (owner decision D32): a PAPER session replayed offline from LOCAL real historical bars.

Nothing here is a second engine. A demo session is created with `create_paper_session` and advanced with the
unchanged `paper_cycle` (same TradingSession, validity, risk, safety, Nautilus simulated execution, journal and
D31 decision trace). The only difference from live paper trading is the price source: instead of Binance, the
cycle is given a replay feed that returns the bars of a local file closed at the simulated clock. Each step moves
the clock to one minute after the next bar's close, so every bar is "seen on time" exactly as it would be live.

  * offline: no network call is possible from a demo (the live fetchers are never passed in);
  * the sealed confirmation period is never read: bars are loaded with not_after=SEALED_FROM;
  * no orders anywhere: paper execution is simulated (MAINNET_ENABLED stays False);
  * every demo lives in its own folder under <workspace>/demo/; "reset" starts a NEW demo, old ones are kept;
    a demo is deleted only by an explicit, confirmed request.

Folder: the paper files (session.json, bars.jsonl, journal.db, status.json, report.json, decision_trace.jsonl)
plus demo.json (replay settings + clock, rewritten atomically) and equity.jsonl (append-only: equity after each step).
"""
from __future__ import annotations

import json
import os
import re
import shutil
import threading
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from ..api import TIMEFRAMES, StrategyError
from ..loader import load_strategy
from ..ohlcv import load_bars
from ..paper_live import create_paper_session, paper_cycle
from ..runtime import MarketSpec, RunSettings

SEALED_FROM = datetime(2025, 9, 7, tzinfo=timezone.utc)
DEFAULT_CAPITAL = Decimal("10000")
SPEEDS = {"1": 3.0, "2": 1.5, "5": 0.5, "max": 0.0}      # pause between steps (seconds); the engine adds its own time
_NAME = re.compile(r"^demo-[0-9]{8}T[0-9]{6}-[0-9a-f]{4}$")


def _atomic(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _market_from_file(path: Path) -> MarketSpec:
    m = re.match(r"^([A-Z0-9]+)_([A-Z0-9]+)-", path.name)
    if not m:
        return MarketSpec()
    return MarketSpec(symbol=m.group(1) + m.group(2), base_currency=m.group(1), quote_currency=m.group(2))


class _Replay:
    """The local price source: only bars closed at the simulated clock, never anything later."""

    def __init__(self, bars: list[Any]) -> None:
        self.bars = bars

    def fetch(self, symbol: str, tf: str, *, start: datetime, now: datetime, cafile: str | None = None) -> list[Any]:
        return [b for b in self.bars if b.open_time >= start and b.close_time <= now]

    def fetch_open(self, symbol: str, tf: str, *, now: datetime, cafile: str | None = None) -> tuple[datetime, Decimal] | None:
        forming = [b for b in self.bars if b.open_time <= now < b.close_time]
        return (forming[0].open_time, forming[0].open) if forming else None


class DemoManager:
    def __init__(self, root: Path) -> None:
        self.base = Path(root) / "demo"
        self.base.mkdir(parents=True, exist_ok=True)
        self._threads: dict[str, threading.Thread] = {}
        self._stop: dict[str, threading.Event] = {}
        self._lock = threading.Lock()
        self._replays: dict[str, _Replay] = {}

    # --- lifecycle ------------------------------------------------------------------------------------------
    def folder(self, name: str) -> Path:
        if not _NAME.match(name or ""):
            raise StrategyError("اسم الجلسة التجريبية غير صالح.")
        f = self.base / name
        if not (f / "demo.json").exists():
            raise StrategyError("الجلسة التجريبية غير موجودة.")
        return f

    def create(self, *, spec: str, data: Path, start: datetime | None = None, capital: Any = DEFAULT_CAPITAL,
               settings: RunSettings | None = None, params: dict[str, Any] | None = None, speed: str = "2") -> dict[str, Any]:
        loaded = load_strategy(spec)
        tf = loaded.cls.timeframe
        bars = load_bars(data, tf, not_after=SEALED_FROM, allow_gaps=True)
        warm = loaded.cls.warmup_bars + 5
        if len(bars) < warm + 10:
            raise StrategyError("ملف البيانات ما فيه شموع كافية للتجربة.")
        if start is None:                                 # default: the last ~500 bars before the sealed period
            first = max(warm, len(bars) - 500)
        else:
            first = next((i for i, b in enumerate(bars) if b.open_time >= start), None)
            if first is None or first > len(bars) - 10:
                raise StrategyError("تاريخ البداية متأخر؛ ما يكفي شموع بعده قبل 2025-09-07.")
            first = max(first, warm)
        settings = settings or RunSettings(capital_quote=Decimal(str(capital)))
        name = "demo-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + os.urandom(2).hex()
        folder = self.base / name
        clock0 = bars[first - 1].close_time            # the session starts when bar first-1 has closed
        create_paper_session(folder, strategy_spec=spec, market=_market_from_file(Path(data)), settings=settings,
                             params=params or {}, now=clock0)
        doc = {"name": name, "data": str(data), "timeframe": tf, "strategy": loaded.cls.name, "spec": spec,
               "first_index": first, "cursor": first - 1, "last_index": len(bars) - 1, "speed": speed if speed in SPEEDS else "2",
               "state": "paused", "created_at": datetime.now(timezone.utc).isoformat(),
               "replay_from": bars[first].open_time.isoformat(), "replay_to": bars[-1].close_time.isoformat(),
               "capital": str(settings.capital_quote)}
        _atomic(folder / "demo.json", json.dumps(doc, indent=1))
        (folder / "equity.jsonl").touch()
        self._replays[name] = _Replay(bars)
        self._step(name, clock=clock0)                    # warm-up history only (no live bar yet)
        return self.status(name)

    def _doc(self, name: str) -> dict[str, Any]:
        return json.loads((self.folder(name) / "demo.json").read_text(encoding="utf-8"))

    def _replay(self, name: str, doc: dict[str, Any]) -> _Replay:
        if name not in self._replays:
            self._replays[name] = _Replay(load_bars(doc["data"], doc["timeframe"], not_after=SEALED_FROM, allow_gaps=True))
        return self._replays[name]

    def _step(self, name: str, *, clock: datetime | None = None) -> dict[str, Any]:
        """Advance the clock by one bar and run ONE unchanged paper cycle at that clock."""
        folder = self.folder(name)
        doc = self._doc(name)
        rp = self._replay(name, doc)
        if clock is None:
            nxt = doc["cursor"] + 1
            if nxt > doc["last_index"]:
                doc["state"] = "finished"
                _atomic(folder / "demo.json", json.dumps(doc, indent=1))
                return doc
            clock = rp.bars[nxt].close_time + timedelta(minutes=1)   # seen on time, like a live loop
            doc["cursor"] = nxt
        st = paper_cycle(folder, fetch=rp.fetch, fetch_open=rp.fetch_open, now=clock)
        doc["clock"] = clock.isoformat()
        _atomic(folder / "demo.json", json.dumps(doc, indent=1))
        if st.get("equity_quote") is not None:
            with (folder / "equity.jsonl").open("a", encoding="utf-8") as fh:
                fh.write(json.dumps({"t": clock.isoformat(), "equity": str(st["equity_quote"]),
                                     "price": str(rp.bars[doc["cursor"]].close)}) + "\n")
        return doc

    def step(self, name: str, n: int = 1) -> dict[str, Any]:
        with self._lock:
            if name in self._threads and self._threads[name].is_alive():
                raise StrategyError("الجلسة التجريبية تعمل؛ أوقفها قبل التقديم اليدوي.")
        for _ in range(max(1, min(int(n), 50))):
            if self._doc(name)["state"] == "finished":
                break
            self._step(name)
        return self.status(name)

    def start(self, name: str, speed: str | None = None) -> dict[str, Any]:
        doc = self._doc(name)
        if doc["state"] == "finished":
            raise StrategyError("انتهت بيانات هذه التجربة؛ ابدأ تجربة جديدة.")
        if speed in SPEEDS:
            doc["speed"] = speed
        with self._lock:
            if name in self._threads and self._threads[name].is_alive():
                doc["state"] = "running"
                _atomic(self.folder(name) / "demo.json", json.dumps(doc, indent=1))
                return self.status(name)
            doc["state"] = "running"
            _atomic(self.folder(name) / "demo.json", json.dumps(doc, indent=1))
            ev = threading.Event()
            self._stop[name] = ev
            t = threading.Thread(target=self._run, args=(name, ev), daemon=True)
            self._threads[name] = t
            t.start()
        return self.status(name)

    def _run(self, name: str, ev: threading.Event) -> None:
        while not ev.is_set():
            try:
                doc = self._step(name)
            except Exception as exc:                       # stop and say why; never invent a price
                d = self._doc(name)
                d.update(state="error", error=f"{type(exc).__name__}: {exc}")
                _atomic(self.folder(name) / "demo.json", json.dumps(d, indent=1))
                return
            if doc.get("state") == "finished":
                return
            ev.wait(SPEEDS.get(self._doc(name).get("speed", "2"), 1.5))

    def pause(self, name: str) -> dict[str, Any]:
        doc = self._doc(name)
        with self._lock:
            ev = self._stop.get(name)
            if ev:
                ev.set()
            t = self._threads.get(name)
        if t:
            t.join(timeout=120)
        doc = self._doc(name)
        if doc["state"] == "running":
            doc["state"] = "paused"
            _atomic(self.folder(name) / "demo.json", json.dumps(doc, indent=1))
        return self.status(name)

    def set_speed(self, name: str, speed: str) -> dict[str, Any]:
        if speed not in SPEEDS:
            raise StrategyError("سرعة غير معروفة.")
        doc = self._doc(name)
        doc["speed"] = speed
        _atomic(self.folder(name) / "demo.json", json.dumps(doc, indent=1))
        return self.status(name)

    def delete(self, name: str, confirm: str) -> dict[str, Any]:
        if confirm != name:
            raise StrategyError("للحذف لازم تكتب اسم الجلسة التجريبية نفسه للتأكيد.")
        self.pause(name)
        shutil.rmtree(self.folder(name))
        self._replays.pop(name, None)
        return {"ok": True}

    # --- views ------------------------------------------------------------------------------------------
    def status(self, name: str) -> dict[str, Any]:
        folder = self.folder(name)
        doc = self._doc(name)
        st = json.loads((folder / "status.json").read_text(encoding="utf-8")) if (folder / "status.json").exists() else {}
        equity = [json.loads(l) for l in (folder / "equity.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
        rows = [json.loads(l) for l in (folder / "bars.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
        bars = [{"t": r["date"], "o": float(r["open"]), "h": float(r["high"]), "l": float(r["low"]), "c": float(r["close"])} for r in rows]
        alive = name in self._threads and self._threads[name].is_alive()
        total = doc["last_index"] - doc["first_index"] + 1
        done = max(0, doc["cursor"] - doc["first_index"] + 1)
        return {"demo": doc, "running": alive, "progress": {"done": done, "total": total},
                "status": st, "equity": equity, "bars": bars[-400:]}

    def list(self) -> dict[str, Any]:
        out = []
        for f in sorted(self.base.iterdir(), reverse=True):
            if not (f / "demo.json").exists():
                continue
            doc = json.loads((f / "demo.json").read_text(encoding="utf-8"))
            st = json.loads((f / "status.json").read_text(encoding="utf-8")) if (f / "status.json").exists() else {}
            out.append({"name": f.name, "strategy": doc["strategy"], "timeframe": doc["timeframe"], "state": doc["state"],
                        "running": f.name in self._threads and self._threads[f.name].is_alive(),
                        "capital": doc["capital"], "equity": st.get("equity_quote"), "net_return": st.get("net_return"),
                        "fills": st.get("fills"), "clock": doc.get("clock"), "created_at": doc["created_at"]})
        return {"demos": out, "speeds": list(SPEEDS), "default_capital": str(DEFAULT_CAPITAL)}

    def stop_all(self) -> None:
        for name in list(self._threads):
            try:
                self.pause(name)
            except Exception:
                pass
