"""Workspace + actions behind the GUI (D30). Pure strategies-layer calls; no engine internals touched."""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import uuid
from dataclasses import asdict
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from crypto_trader.production.environment import MAINNET_ENABLED
from crypto_trader.risk import load_limits

from ..analysis import downsample_bars, drawdown_series, monthly_returns, robustness_verdict
from ..api import TIMEFRAMES, StrategyError
from ..loader import list_strategies, load_strategy
from ..ohlcv import load_bars
from ..runtime import MarketSpec, RunSettings, _limits

REPO = Path(__file__).resolve().parents[4]
SEALED_FROM = datetime(2025, 9, 7, tzinfo=timezone.utc)
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_\-]{0,63}$")
DEFAULT_SETTINGS = {
    "capital": "1000", "fee": "0.001", "slippage_bps": "5", "max_position": None, "risk_overrides": {},
    "data_dirs": [r"C:\Users\Jamal\ct_trading_pilot\user_data\data\binance"],
    "evidence_root": r"C:\Users\Jamal\ct_fix_runs\sandbox\holdout_evidence",
    "test_summary_glob": r"C:\Users\Jamal\ct_fix_runs\d29\full_*.log",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _dec(text: Any, name: str, *, positive: bool = True) -> Decimal:
    try:
        value = Decimal(str(text))
    except (InvalidOperation, ValueError):
        raise StrategyError(f"{name}: not a number.") from None
    if not value.is_finite() or (positive and value <= 0) or value < 0:
        raise StrategyError(f"{name}: must be a positive number.")
    return value


def _atomic(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _tail(path: Path, n: int = 60) -> list[str]:
    if not path.exists():
        return []
    return path.read_text(encoding="utf-8", errors="replace").splitlines()[-n:]


def _spec_file(spec: str) -> str:
    """'C:\\x\\f.py:Class' or 'C:\\x\\f.py' -> 'f.py'."""
    path = spec if spec.endswith(".py") else spec.rpartition(":")[0]
    return Path(path).name


def _pid_alive(pid: int) -> bool:
    if os.name == "nt":
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True)
        return str(pid) in out.stdout
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


class Workspace:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        for sub in ("strategies", "data", "runs", "paper"):
            (self.root / sub).mkdir(parents=True, exist_ok=True)
        self.settings_path = self.root / "settings.json"
        if not self.settings_path.exists():
            _atomic(self.settings_path, json.dumps(DEFAULT_SETTINGS, indent=1))
        self._lock = threading.Lock()

    # --- settings / logging -----------------------------------------------------------------------------
    def settings(self) -> dict[str, Any]:
        return {**DEFAULT_SETTINGS, **json.loads(self.settings_path.read_text(encoding="utf-8"))}

    def log_error(self, text: str) -> None:
        with (self.root / "ui_errors.log").open("a", encoding="utf-8") as fh:
            fh.write(f"[{_now()}] {text}\n")

    def _event(self, kind: str, **data: Any) -> None:
        with (self.root / "operations.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"at": _now(), "kind": kind, **data}, ensure_ascii=False, default=str) + "\n")

    # --- strategies ------------------------------------------------------------------------------------
    def strategy_dirs(self) -> list[Path]:
        dirs = [self.root / "strategies"]
        if (REPO / "user_strategies").is_dir():
            dirs.append(REPO / "user_strategies")
        return dirs

    def strategies(self) -> dict[str, Any]:
        items = []
        for d in self.strategy_dirs():
            for item in list_strategies(d):
                item["folder"] = "workspace" if d == self.root / "strategies" else "examples"
                item["removable"] = item["folder"] == "workspace"
                item["file"] = _spec_file(item["spec"])
                items.append(item)
        return {"strategies": items, "freqtrade_hint": "freqtrade:<path>\\File.py:ClassName"}

    def check_strategy(self, spec: str) -> dict[str, Any]:
        loaded = load_strategy(spec)
        return {"ok": True, "name": loaded.cls.name, "version": loaded.cls.version, "timeframe": loaded.cls.timeframe,
                "warmup_bars": loaded.cls.warmup_bars, "sha256": loaded.source_sha256}

    def upload_strategy(self, filename: str, content: str) -> dict[str, Any]:
        stem = filename[:-3] if filename.endswith(".py") else filename
        if not _NAME.match(stem):
            raise StrategyError("file name: letters, digits, '_' or '-' only, ending in .py.")
        target = self.root / "strategies" / f"{stem}.py"
        if target.exists():
            raise StrategyError(f"{target.name} already exists; remove it first.")
        target.write_text(content, encoding="utf-8")
        try:
            listed = [x for x in list_strategies(target.parent) if _spec_file(x["spec"]) == target.name]
            ok = [x for x in listed if x.get("ok")]
            if not ok:
                raise StrategyError("; ".join(x.get("error", "no Strategy class") for x in listed) or "no Strategy class found.")
        except StrategyError:
            target.unlink(missing_ok=True)
            raise
        self._event("strategy.added", file=target.name)
        return {"ok": True, "file": target.name, "strategies": ok}

    def remove_strategy(self, filename: str) -> dict[str, Any]:
        target = (self.root / "strategies" / filename).resolve()
        if target.parent != (self.root / "strategies").resolve() or target.suffix != ".py":
            raise StrategyError("only files in the workspace strategies folder can be removed.")
        target.unlink()
        self._event("strategy.removed", file=filename)
        return {"ok": True}

    # --- data --------------------------------------------------------------------------------------------
    def data_files(self) -> dict[str, Any]:
        out = []
        dirs = [self.root / "data"] + [Path(d) for d in self.settings()["data_dirs"]]
        for d in dirs:
            if not d.is_dir():
                continue
            for p in sorted(d.iterdir()):
                if p.suffix not in (".csv", ".feather"):
                    continue
                m = re.search(r"-(\d+[mhd])\.", p.name)
                out.append({"path": str(p), "name": p.name, "timeframe": m.group(1) if m else None})
        return {"files": out}

    def _allowed_data(self, path: str) -> Path:
        allowed = {f["path"] for f in self.data_files()["files"]}
        if path not in allowed:
            raise StrategyError("data file must be one of the listed data files.")
        return Path(path)

    # --- backtest ----------------------------------------------------------------------------------------
    def _run_settings(self, body: dict[str, Any]) -> RunSettings:
        s = self.settings()
        fee = _dec(body.get("fee", s["fee"]), "fee")
        overrides = dict(s.get("risk_overrides") or {}) | dict(body.get("risk_overrides") or {})
        mp = body.get("max_position", s.get("max_position"))
        settings = RunSettings(capital_quote=_dec(body.get("capital", s["capital"]), "capital"), taker_fee=fee, maker_fee=fee,
                               slippage_bps=_dec(body.get("slippage_bps", s["slippage_bps"]), "slippage", positive=False),
                               max_position_quote=_dec(mp, "max position") if mp not in (None, "") else None,
                               risk_overrides=overrides)
        try:
            load_limits(_limits(settings))     # validates override keys/values the engine way
        except StrategyError:
            raise
        except Exception as exc:               # the engine's own rejection, reported as bad input
            raise StrategyError(f"risk limits rejected by the engine: {exc}") from exc
        return settings

    def backtest(self, body: dict[str, Any]) -> dict[str, Any]:
        from ..fast import run_fast_backtest
        from ..runtime import run_backtest

        loaded = load_strategy(body["spec"])
        params = dict(body.get("params") or {})
        start = datetime.fromisoformat(body["start"]).replace(tzinfo=timezone.utc) if body.get("start") else None
        end = datetime.fromisoformat(body["end"]).replace(tzinfo=timezone.utc) if body.get("end") else None
        bars = load_bars(self._allowed_data(body["data"]), loaded.cls.timeframe, start=start, end=end,
                         not_after=SEALED_FROM, allow_gaps=True)
        if len(bars) < loaded.cls.warmup_bars + 3:
            raise StrategyError(f"only {len(bars)} bars in range; the strategy needs {loaded.cls.warmup_bars}+ to warm up.")
        settings = self._run_settings(body)
        rid = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:6]
        run_dir = self.root / "runs" / rid
        engine = bool(body.get("engine"))
        if engine:
            result = run_backtest(loaded.instantiate(**params), bars, settings=settings, out_dir=run_dir / "engine")
        else:
            result = run_fast_backtest(loaded.instantiate(**params), bars, settings=settings)

        def rerun(fee: Decimal) -> Any:
            s2 = RunSettings(**{**asdict(settings), "taker_fee": fee, "maker_fee": fee})
            return run_fast_backtest(loaded.instantiate(**params), bars, settings=s2)

        with self._lock:   # count, judge and append as one step: concurrent runs never share a trial number
            n_trials = len(self.runs()["runs"]) + 1
            verdict = robustness_verdict(result, rerun_with_fees=rerun, n_trials=n_trials)
            return self._record(rid, run_dir, engine, body, loaded, params, bars, result, verdict)

    def _record(self, rid: str, run_dir: Path, engine: bool, body: dict[str, Any], loaded: Any, params: dict[str, Any],
                bars: Any, result: Any, verdict: dict[str, Any]) -> dict[str, Any]:
        step = max(1, len(result.equity_curve) // 1500)
        payload = {
            "id": rid, "at": _now(), "mode": "engine" if engine else "fast", "strategy": result.strategy,
            "strategy_version": result.strategy_version, "spec": body["spec"], "sha256": loaded.source_sha256,
            "data": body["data"], "timeframe": result.timeframe, "start": result.start, "end": result.end,
            "bars_count": result.bars, "params": params, "settings": result.settings, "metrics": result.metrics,
            "benchmark": result.benchmark, "verdict": verdict, "trades": result.trades,
            "equity": result.equity_curve[::step], "drawdown": drawdown_series(result.equity_curve)[::step],
            "monthly": monthly_returns(result.equity_curve), "bars": downsample_bars(bars),
            "fills": (result.engine.get("fills") if not engine else None),
        }
        run_dir.mkdir(parents=True, exist_ok=True)
        _atomic(run_dir / "result.json", json.dumps(payload, ensure_ascii=False, default=str))
        summary = {k: payload[k] for k in ("id", "at", "mode", "strategy", "spec", "sha256", "data", "start", "end")}
        summary |= {"net_return": result.metrics["net_return"], "sharpe": result.metrics.get("sharpe"),
                    "max_drawdown": result.metrics["max_drawdown"], "verdict": verdict["verdict"],
                    "hold_return": result.benchmark["net_return"]}
        with (self.root / "runs" / "runs.jsonl").open("a", encoding="utf-8") as fh:   # caller holds self._lock
            fh.write(json.dumps(summary, ensure_ascii=False, default=str) + "\n")   # append-only experiment log
        self._event("backtest.done", id=rid, strategy=result.strategy, mode=payload["mode"])
        return payload

    def runs(self) -> dict[str, Any]:
        path = self.root / "runs" / "runs.jsonl"
        rows = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()] if path.exists() else []
        return {"runs": rows[::-1], "count": len(rows)}

    def run(self, rid: str) -> dict[str, Any]:
        if not re.match(r"^[0-9T]+-[0-9a-f]{6}$", rid):
            raise StrategyError("bad run id.")
        return json.loads((self.root / "runs" / rid / "result.json").read_text(encoding="utf-8"))

    # --- paper ---------------------------------------------------------------------------------------------
    def _paper_dir(self, name: str) -> Path:
        if not _NAME.match(name):
            raise StrategyError("session name: letters, digits, '_' or '-'.")
        return self.root / "paper" / name

    def _running(self, folder: Path) -> bool:
        """True while the session's loop process is alive, whether or not a STOP has been requested."""
        pid_file = folder / "pid.json"
        if not pid_file.exists():
            return False
        return _pid_alive(int(json.loads(pid_file.read_text())["pid"]))

    def paper_sessions(self) -> dict[str, Any]:
        out = []
        for folder in sorted((self.root / "paper").iterdir()):
            if not (folder / "session.json").exists():
                continue
            doc = json.loads((folder / "session.json").read_text(encoding="utf-8"))
            st = json.loads((folder / "status.json").read_text(encoding="utf-8")) if (folder / "status.json").exists() else {}
            out.append({"name": folder.name, "strategy": doc["strategy_name"], "symbol": doc["market"]["symbol"],
                        "timeframe": doc["timeframe"], "capital": doc["settings"]["capital_quote"],
                        "running": self._running(folder), "stopping": (folder / "STOP").exists(), "equity": st.get("equity_quote"), "net_return": st.get("net_return"),
                        "position": st.get("position"), "fills": st.get("fills"), "last_cycle": st.get("cycle_at"),
                        "missed_bars": st.get("missed_bars"), "error": (json.loads((folder / "last_error.json").read_text())
                                                                     if (folder / "last_error.json").exists() else None)})
        return {"sessions": out}

    def paper_new(self, body: dict[str, Any]) -> dict[str, Any]:
        from ..paper_live import create_paper_session

        folder = self._paper_dir(body["name"])
        market = MarketSpec(symbol=body.get("symbol", "BTCUSDT"), base_currency=body.get("base", "BTC"))
        create_paper_session(folder, strategy_spec=body["spec"], market=market, settings=self._run_settings(body),
                             params=dict(body.get("params") or {}))
        self._event("paper.created", name=body["name"], spec=body["spec"])
        return {"ok": True, "name": body["name"]}

    def paper_start(self, name: str) -> dict[str, Any]:
        folder = self._paper_dir(name)
        if not (folder / "session.json").exists():
            raise StrategyError("no such session.")
        with self._lock:
            return self._start_locked(name, folder)

    def _start_locked(self, name: str, folder: Path) -> dict[str, Any]:
        if self._running(folder):
            if (folder / "STOP").exists():
                # the old loop has not reached its STOP check yet; removing STOP now would leave two loops
                raise StrategyError("the previous loop is still stopping; try again after one poll interval.")
            return {"ok": True, "already_running": True}
        (folder / "STOP").unlink(missing_ok=True)
        env = dict(os.environ, PYTHONPATH=str(REPO / "src"), PYTHONUNBUFFERED="1", PYTHONDONTWRITEBYTECODE="1")
        flags = (subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS) if os.name == "nt" else 0
        with open(folder / "loop.log", "a", encoding="utf-8") as log:   # the child keeps its own handle
            proc = subprocess.Popen([sys.executable, "-B", "-m", "crypto_trader.strategies", "paper-run", str(folder),
                                     "--poll", "30"], stdout=log, stderr=subprocess.STDOUT, env=env, cwd=str(REPO),
                                    creationflags=flags)
        _atomic(folder / "pid.json", json.dumps({"pid": proc.pid, "started": _now()}))
        self._event("paper.started", name=name, pid=proc.pid)
        return {"ok": True, "pid": proc.pid}

    def paper_stop(self, name: str) -> dict[str, Any]:
        folder = self._paper_dir(name)
        (folder / "STOP").write_text(_now(), encoding="utf-8")
        self._event("paper.stopped", name=name)
        return {"ok": True, "note": "the loop stops at its next check (within one poll interval)."}

    def paper_detail(self, name: str) -> dict[str, Any]:
        folder = self._paper_dir(name)
        doc = json.loads((folder / "session.json").read_text(encoding="utf-8"))
        st = json.loads((folder / "status.json").read_text(encoding="utf-8")) if (folder / "status.json").exists() else {}
        rows = [json.loads(l) for l in _tail(folder / "bars.jsonl", 300) if l.strip()]
        bars = [{"t": r["date"], "o": float(r["open"]), "h": float(r["high"]), "l": float(r["low"]), "c": float(r["close"])}
                for r in rows if not r.get("_bad")]
        decisions = []
        if (folder / "report.json").exists():
            rep = json.loads((folder / "report.json").read_text(encoding="utf-8"))
            decisions = [{"at": d["observed_at"], "action": d["action"], "rationale": d["rationale"],
                          "risk": d.get("risk_decision"), "validity": d.get("validity_disposition")}
                         for d in rep.get("decisions", []) if d["rationale"] not in ("warmup", "pre-session-history")][-50:]
        err = json.loads((folder / "last_error.json").read_text(encoding="utf-8")) if (folder / "last_error.json").exists() else None
        return {"session": doc, "status": st, "running": self._running(folder), "bars": bars, "decisions": decisions[::-1],
                "log": _tail(folder / "loop.log", 40), "error": err}

    # --- risk / logs / system / home --------------------------------------------------------------------------
    def risk(self) -> dict[str, Any]:
        s = self.settings()
        settings = self._run_settings({})
        return {"editable": {k: s[k] for k in ("capital", "fee", "slippage_bps", "max_position", "risk_overrides")},
                "effective_engine_limits": _limits(settings),
                "rules": ["Spot، شراء فقط، بدون رافعة", "الصفقة عند افتتاح الشمعة التالية ± الانزلاق",
                          "الشمعة المتأخرة (> 5 دقائق أو شمعة) ما تتداول", "قرار آخر شمعة في الاختبار ما يتنفذ"],
                "production": {"mainnet_enabled": MAINNET_ENABLED,
                               "note": "حدود المسار الإنتاجي تُعرض فقط ولا تُعدَّل من الواجهة؛ التداول الحقيقي معطّل في الكود."}}

    def save_risk(self, body: dict[str, Any]) -> dict[str, Any]:
        current = json.loads(self.settings_path.read_text(encoding="utf-8"))
        candidate = {**current, **{k: body[k] for k in ("capital", "fee", "slippage_bps", "max_position", "risk_overrides") if k in body}}
        self._run_settings({**candidate})       # validate before saving
        _atomic(self.settings_path, json.dumps(candidate, indent=1))
        self._event("risk.saved", values={k: candidate.get(k, DEFAULT_SETTINGS[k]) for k in ("capital", "fee", "slippage_bps", "max_position", "risk_overrides")})
        return {"ok": True}

    def logs(self) -> dict[str, Any]:
        ops = [json.loads(l) for l in _tail(self.root / "operations.jsonl", 200) if l.strip()]
        paper = {}
        for folder in (self.root / "paper").iterdir():
            if folder.is_dir():
                paper[folder.name] = _tail(folder / "loop.log", 30)
        return {"operations": ops[::-1], "errors": _tail(self.root / "ui_errors.log", 80)[::-1], "paper_logs": paper,
                "experiments": self.runs()["runs"][:100]}

    def system(self) -> dict[str, Any]:
        s = self.settings()
        ev: dict[str, Any] = {"available": False}
        root = Path(s["evidence_root"])
        if (root / "ledger.jsonl").exists():
            rows = [json.loads(l) for l in (root / "ledger.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
            kinds: dict[str, int] = {}
            for r in rows:
                kinds[r["kind"]] = kinds.get(r["kind"], 0) + 1
            last = rows[-1] if rows else {}
            ev = {"available": True, "entries": len(rows), "kinds": kinds,
                  "last": last.get("request_utc") or last.get("detected_utc"), "alerts": _tail(root / "alerts.log", 5)}
        tests = None
        import glob
        logs = sorted(glob.glob(s["test_summary_glob"]), key=os.path.getmtime)
        if logs:
            tests = {"file": Path(logs[-1]).name, "summary": (_tail(Path(logs[-1]), 1) or [""])[0]}
        return {"mainnet_enabled": MAINNET_ENABLED, "evidence_collector": ev, "tests": tests,
                "workspace": str(self.root), "time": _now()}

    def home(self) -> dict[str, Any]:
        sessions = self.paper_sessions()["sessions"]
        runs = self.runs()
        return {"sessions": sessions, "running": sum(1 for x in sessions if x["running"]),
                "equity_total": sum(float(x["equity"] or 0) for x in sessions),
                "capital_total": sum(float(x["capital"]) for x in sessions),
                "open_positions": [{"session": x["name"], "position": x["position"]} for x in sessions
                                   if x["position"] and any(Decimal(v) != 0 for v in x["position"].values())],
                "experiments": runs["count"], "last_run": runs["runs"][0] if runs["runs"] else None,
                "mainnet_enabled": MAINNET_ENABLED}
