"""Strategy comparison (phase 2, owner decision D42, B2): backtest results side by side, never misleadingly.

Two runs are compared only when they share the same data file, timeframe, period, mode (fast preview vs full engine)
and cost settings (fees, slippage); every other run is listed apart as having no comparable peer. Every number is read
from the run's own result.json (metrics, benchmark, settings) - nothing is recomputed or estimated here.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

__all__ = ["comparison", "comparable_key"]

_cache: dict[str, tuple[float, int, dict[str, Any]]] = {}


def _n(v: Any) -> float | None:
    """A recorded metric is shown only as a number; anything else in the file is "not available", never markup
    (Sol D42 blocker 2)."""
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and v == v else None


def _result(path: Path) -> dict[str, Any] | None:
    try:
        st = path.stat()
    except OSError:
        return None
    hit = _cache.get(str(path))
    if hit and hit[0] == st.st_mtime and hit[1] == st.st_size:
        return hit[2]
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    m, s = doc.get("metrics") or {}, doc.get("settings") or {}
    row = {"id": doc.get("id"), "at": doc.get("at"), "strategy": doc.get("strategy"), "version": doc.get("strategy_version"),
           "params": doc.get("params") or {}, "mode": doc.get("mode"), "data": Path(str(doc.get("data") or "")).name,
           "timeframe": doc.get("timeframe"), "start": doc.get("start"), "end": doc.get("end"), "bars": doc.get("bars_count"),
           "capital": s.get("capital_quote"), "taker_fee": s.get("taker_fee"), "maker_fee": s.get("maker_fee"),
           "slippage_bps": s.get("slippage_bps"), "net_return": _n(m.get("net_return")),
           "max_drawdown": _n(m.get("max_drawdown")), "sharpe": _n(m.get("sharpe")), "round_trips": _n(m.get("round_trips")),
           "fills": _n(m.get("fills")), "fees_quote": _n(m.get("fees_quote")),
           "hold_return": _n((doc.get("benchmark") or {}).get("net_return")),
           "verdict": (doc.get("verdict") or {}).get("verdict")}
    _cache[str(path)] = (st.st_mtime, st.st_size, row)
    return row


def comparable_key(r: dict[str, Any]) -> tuple:
    return (r["data"], r["timeframe"], r["start"], r["end"], r["mode"], r["taker_fee"], r["maker_fee"], r["slippage_bps"])


def comparison(root: Path) -> dict[str, Any]:
    log = root / "runs" / "runs.jsonl"
    ids = []
    for line in log.read_text(encoding="utf-8").splitlines() if log.exists() else []:
        try:
            ids.append(json.loads(line)["id"])
        except (ValueError, KeyError, TypeError):
            continue
    rows, missing = [], []
    for rid in dict.fromkeys(ids):
        r = _result(root / "runs" / str(rid) / "result.json")
        (rows.append(r) if r else missing.append(rid))
    groups: dict[tuple, list[dict[str, Any]]] = {}
    for r in rows:
        groups.setdefault(comparable_key(r), []).append(r)
    out = []
    for key, rs in groups.items():
        rs = sorted(rs, key=lambda r: (r["net_return"] is None, -(r["net_return"] or 0)))
        out.append({"key": dict(zip(("data", "timeframe", "start", "end", "mode", "taker_fee", "maker_fee", "slippage_bps"), key)),
                    "runs": rs, "comparable": len(rs) > 1})
    out.sort(key=lambda g: max(r["at"] or "" for r in g["runs"]), reverse=True)     # newest first ...
    out.sort(key=lambda g: (not g["comparable"], -len(g["runs"])))                    # ... within: comparable, larger
    return {"groups": out, "runs": len(rows), "missing": missing}
