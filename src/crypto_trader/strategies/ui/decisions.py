"""Decision center (owner decision D31): why each decision was taken or not, and by whom.

Every field shown comes from records written while the engine ran; nothing is inferred afterwards:
  * the decision chain is the engine's journal-derived RunReport.decisions (paper: report.json of the
    session; engine backtest: engine/decisions.json): strategy action + rationale -> validity disposition
    + reason -> risk decision + reason codes -> safety -> submission;
  * fills come from the same report (matched by intent id);
  * the decision trace (decision_trace.jsonl, written by StrategyDecisionSource while deciding) adds what
    the strategy itself read on that bar and the signal it returned.
No AI takes part in this path (decision_source == "strategy-plugin"); the AI step is always reported as
"not involved". Fast previews have no journal and are not listed.
"""
from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

__all__ = ["collect", "detail", "KINDS"]

#: decision kinds, in display order
KINDS = ("entry", "exit", "rejected_risk", "rejected_validity", "blocked_safety", "missed", "no_entry", "keep", "other")
_SKIP = {"warmup", "pre-session-history"}
_EXIT_REASONS = {"ft:exit:stoploss": "stop_loss", "ft:exit:trailing": "trailing_stop", "ft:exit:roi": "take_profit",
                 "ft:exit:signal": "exit_signal"}


def _json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _traces(path: Path) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
            out.setdefault(row["ref"], row)          # first record wins (append-only, never rewritten)
        except (ValueError, KeyError, TypeError):
            continue
    return out


def _dec(text: Any) -> Decimal | None:
    try:
        return Decimal(str(text))
    except (InvalidOperation, ValueError):
        return None


def _kind(d: dict[str, Any], trace: dict[str, Any] | None) -> str:
    rationale, sub, action = d.get("rationale") or "", d.get("submission") or "", d.get("action")
    if rationale == "missed:bar-arrived-late":
        return "missed"
    if action in ("buy", "sell"):
        return {"submitted_to_simulator": "entry" if action == "buy" else "exit",
                "stopped_by_validity": "rejected_validity", "stopped_by_risk": "rejected_risk",
                "blocked_by_safety": "blocked_safety"}.get(sub, "other")
    if action == "hold":
        held = _dec(((trace or {}).get("sizing") or {}).get("held"))
        if held is not None:
            return "keep" if held > 0 else "no_entry"
        return "no_entry" if rationale in ("strategy:keep", "strategy:no_cash") else "other"
    return "other"


def _exit_kind(signal: dict[str, Any] | None, rationale: str) -> str | None:
    reason = (signal or {}).get("reason") or (rationale.split(":", 1)[1] if rationale.startswith("strategy:") else "")
    return _EXIT_REASONS.get(reason, "strategy_exit")


def _items(source: dict[str, Any], decisions: list[dict[str, Any]], fills: list[dict[str, Any]],
           traces: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    by_intent: dict[str, list[dict[str, Any]]] = {}
    for f in fills:
        by_intent.setdefault(str(f.get("intent_id")), []).append(f)
    out = []
    for d in decisions:
        if d.get("rationale") in _SKIP:
            continue
        ref = d["market_reference"]
        t = traces.get(ref)
        kind = _kind(d, t)
        item = {
            "id": f"{source['kind']}:{source['name']}:{ref}", "source": source,
            "strategy": source["strategy"], "symbol": source["symbol"], "timeframe": source["timeframe"],
            "ref": ref, "at": d["observed_at"], "bar_open": (t or {}).get("bar_open"),
            "kind": kind, "exit_kind": _exit_kind((t or {}).get("signal"), d.get("rationale") or "") if kind == "exit" else None,
            "action": d.get("action"), "rationale": d.get("rationale"), "submission": d.get("submission"),
            "validity": None if d.get("validity_disposition") is None else
                        {"disposition": d["validity_disposition"], "reason": d.get("validity_reason")},
            "risk": None if d.get("risk_decision") is None else
                    {"decision": d["risk_decision"], "codes": list(d.get("risk_reason_codes") or [])},
            "fills": by_intent.get(str(d.get("intent_id")), []) if d.get("intent_id") else [],
            "signal": (t or {}).get("signal"), "read": (t or {}).get("read"), "sizing": (t or {}).get("sizing"),
            "price": (t or {}).get("price"),
            # who decided: only what the records show; the AI step is reported, never assumed
            "ai_involved": bool((t or {}).get("ai_involved", False)),
            "decision_source": (t or {}).get("decision_source", "strategy-plugin"),
            "trace_recorded_at": (t or {}).get("recorded_at"),
            "evidence": {"journal_decision": d, "trace": t},
        }
        out.append(item)
    return out


def _paper(root: Path) -> list[dict[str, Any]]:
    out = []
    base = root / "paper"
    if not base.is_dir():
        return out
    for folder in sorted(base.iterdir()):
        if not (folder / "session.json").exists() or not (folder / "report.json").exists():
            continue
        doc = _json(folder / "session.json")
        rep = _json(folder / "report.json")
        source = {"kind": "paper", "name": folder.name, "label": f"ورقي · {folder.name}", "strategy": doc["strategy_name"],
                  "symbol": doc["market"]["symbol"], "timeframe": doc["timeframe"]}
        out += _items(source, rep.get("decisions", []), rep.get("fills", []), _traces(folder / "decision_trace.jsonl"))
    return out


def _backtests(root: Path) -> list[dict[str, Any]]:
    out = []
    base = root / "runs"
    if not base.is_dir():
        return out
    for run in sorted(base.iterdir()):
        eng = run / "engine"
        if not (eng / "decisions.json").exists() or not (eng / "backtest_result.json").exists():
            continue
        res = _json(eng / "backtest_result.json")
        chain = _json(eng / "decisions.json")
        source = {"kind": "backtest", "name": run.name, "label": f"اختبار كامل · {run.name}", "strategy": res["strategy"],
                  "symbol": res["symbol"], "timeframe": res["timeframe"]}
        out += _items(source, chain.get("decisions", []), chain.get("fills", []), _traces(eng / "decision_trace.jsonl"))
    return out


def _parse_time(text: str | None) -> datetime | None:
    return datetime.fromisoformat(text) if text else None


def collect(root: Path, *, source: str | None = None, strategy: str | None = None, symbol: str | None = None,
            kinds: list[str] | None = None, since: str | None = None, until: str | None = None,
            limit: int = 300) -> dict[str, Any]:
    """All decisions, newest first, filtered; counts per kind are over the filtered set before `kinds`."""
    items = _paper(root) + _backtests(root)
    facets = {"sources": sorted({(i["source"]["kind"], i["source"]["name"], i["source"]["label"]) for i in items}),
              "strategies": sorted({i["strategy"] for i in items}), "symbols": sorted({i["symbol"] for i in items})}
    lo, hi = _parse_time(since), _parse_time(until)
    sel = [i for i in items
           if (not source or f"{i['source']['kind']}:{i['source']['name']}" == source)
           and (not strategy or i["strategy"] == strategy) and (not symbol or i["symbol"] == symbol)
           and (lo is None or datetime.fromisoformat(i["at"]) >= lo) and (hi is None or datetime.fromisoformat(i["at"]) <= hi)]
    counts = {k: 0 for k in KINDS}
    for i in sel:
        counts[i["kind"]] += 1
    if kinds:
        sel = [i for i in sel if i["kind"] in kinds]
    sel.sort(key=lambda i: i["at"], reverse=True)
    total = len(sel)
    return {"items": [{k: v for k, v in i.items() if k != "evidence"} for i in sel[: max(1, min(limit, 2000))]],
            "total": total, "counts": counts, "facets": {**facets, "sources": [
                {"id": f"{k}:{n}", "label": label} for k, n, label in facets["sources"]]}}


def detail(root: Path, item_id: str, *, window: int = 40) -> dict[str, Any]:
    """One decision with its raw evidence and the bars around it (paper: bars.jsonl; backtest: chart bars)."""
    kind, _, rest = item_id.partition(":")
    name, _, ref = rest.partition(":")
    if kind not in ("paper", "backtest") or not name or not ref:
        raise ValueError("bad decision id.")
    items = _paper(root) if kind == "paper" else _backtests(root)
    match = next((i for i in items if i["id"] == item_id), None)
    if match is None:
        raise KeyError("decision not found.")
    bars: list[dict[str, Any]] = []
    at = datetime.fromisoformat(match["at"])
    if kind == "paper":
        path = root / "paper" / name / "bars.jsonl"
        rows = []
        for line in path.read_text(encoding="utf-8").splitlines() if path.exists() else []:
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
        bars = [{"t": r["date"], "o": float(r["open"]), "h": float(r["high"]), "l": float(r["low"]), "c": float(r["close"])}
                for r in rows]
    else:
        res = root / "runs" / name / "result.json"
        if res.exists():
            bars = _json(res).get("bars", [])
    if bars:
        times = [datetime.fromisoformat(b["t"]) for b in bars]
        k = max((i for i, t in enumerate(times) if t <= at), default=0)
        bars = bars[max(0, k - window): k + 12]
    return {"item": match, "bars": bars}
