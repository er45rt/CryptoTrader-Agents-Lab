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


def _demos(root: Path) -> list[dict[str, Any]]:
    """Demo sessions (D32) are paper sessions in <workspace>/demo; same records, same reading."""
    out = []
    base = root / "demo"
    if not base.is_dir():
        return out
    for folder in sorted(base.iterdir()):
        if not (folder / "session.json").exists() or not (folder / "report.json").exists():
            continue
        doc = _json(folder / "session.json")
        rep = _json(folder / "report.json")
        source = {"kind": "demo", "name": folder.name, "label": f"تجريبي · {folder.name}", "strategy": doc["strategy_name"],
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


#: where an order intent stopped, from the journaled submission (each decision is counted once, at one stage)
_STOPPED_AT = {"stopped_by_validity": "validity", "stopped_by_risk": "risk", "blocked_by_safety": "safety"}


def funnel(items: list[dict[str, Any]]) -> dict[str, Any]:
    """Decision funnel (phase 2, A1): evaluated bars -> trade signals -> validity -> risk -> safety -> sent -> filled.
    Built only from recorded fields: a signal is a decision whose action is buy/sell; the stage where it stopped is its
    journaled submission. A submission recorded with any other value is counted as unclassified, never guessed."""
    out = {"evaluated": len(items), "missed": 0, "no_signal": 0, "signals": 0, "unclassified": 0,
           "stopped": {"validity": 0, "risk": 0, "safety": 0}, "sent": 0, "filled": 0}
    for i in items:
        if i["kind"] == "missed":
            out["missed"] += 1
        elif i["action"] not in ("buy", "sell"):
            out["no_signal"] += 1
        else:
            out["signals"] += 1
            sub = i.get("submission")
            if sub in _STOPPED_AT:
                out["stopped"][_STOPPED_AT[sub]] += 1
            elif sub == "submitted_to_simulator":
                out["sent"] += 1
                out["filled"] += bool(i["fills"])
            else:
                out["unclassified"] += 1
    passed = out["signals"] - out["unclassified"] - out["stopped"]["validity"]
    out["passed"] = {"validity": passed, "risk": passed - out["stopped"]["risk"],
                     "safety": passed - out["stopped"]["risk"] - out["stopped"]["safety"]}
    return out


def block_reasons(i: dict[str, Any]) -> list[str]:
    """Why this decision did not become a trade, as "<stage>:<recorded code>" keys (phase 2, A3). Only recorded fields:
    the validity reason, the risk/safety reason codes, a late bar, or the strategy's own no-cash rationale. A decision
    the strategy did not want to trade has no block reason - it was not prevented, it was not asked for."""
    k = i["kind"]
    if k == "rejected_validity":
        return [f"validity:{(i.get('validity') or {}).get('reason') or 'unrecorded'}"]
    if k in ("rejected_risk", "blocked_safety"):
        stage = "risk" if k == "rejected_risk" else "safety"
        return [f"{stage}:{c}" for c in ((i.get("risk") or {}).get("codes") or ["unrecorded"])]
    if k == "missed":
        return ["missed:bar-arrived-late"]
    if i.get("rationale") == "strategy:no_cash":
        return ["cash:no_cash"]
    return []


def reasons(items: list[dict[str, Any]]) -> dict[str, Any]:
    """Block reasons ranked by the number of decisions carrying them; a decision with several codes counts once per
    code, so shares are of blocked decisions and may sum above 100%."""
    rows: dict[str, dict[str, Any]] = {}
    blocked = 0
    for i in items:
        keys = block_reasons(i)
        blocked += bool(keys)
        for key in dict.fromkeys(keys):
            r = rows.setdefault(key, {"key": key, "stage": key.split(":", 1)[0], "code": key.split(":", 1)[1],
                                      "count": 0, "last_at": None, "strategies": set()})
            r["count"] += 1
            r["last_at"] = max(r["last_at"] or i["at"], i["at"])
            r["strategies"].add(i["strategy"])
    ranked = sorted(rows.values(), key=lambda r: (-r["count"], r["key"]))
    return {"blocked": blocked, "rows": [{**r, "strategies": sorted(r["strategies"]),
                                          "share": r["count"] / blocked if blocked else 0} for r in ranked]}


def _parse_time(text: str | None) -> datetime | None:
    return datetime.fromisoformat(text) if text else None


def collect(root: Path, *, source: str | None = None, strategy: str | None = None, symbol: str | None = None,
            kinds: list[str] | None = None, since: str | None = None, until: str | None = None,
            reason: str | None = None, limit: int = 300) -> dict[str, Any]:
    """All decisions, newest first, filtered; counts, funnel and reasons are over the filtered set before the
    `kinds` / `reason` drill-down."""
    items = _paper(root) + _demos(root) + _backtests(root)
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
    flow, why = funnel(sel), reasons(sel)
    if kinds:
        sel = [i for i in sel if i["kind"] in kinds]
    if reason:
        sel = [i for i in sel if reason in block_reasons(i)]
    sel.sort(key=lambda i: i["at"], reverse=True)
    total = len(sel)
    return {"items": [{k: v for k, v in i.items() if k != "evidence"} for i in sel[: max(1, min(limit, 2000))]],
            "total": total, "counts": counts, "funnel": flow, "reasons": why, "facets": {**facets, "sources": [
                {"id": f"{k}:{n}", "label": label} for k, n, label in facets["sources"]]}}


def detail(root: Path, item_id: str, *, window: int = 40) -> dict[str, Any]:
    """One decision with its raw evidence and the bars around it (paper: bars.jsonl; backtest: chart bars)."""
    kind, _, rest = item_id.partition(":")
    name, _, ref = rest.partition(":")
    if kind not in ("paper", "demo", "backtest") or not name or not ref:
        raise ValueError("bad decision id.")
    items = _paper(root) if kind == "paper" else _demos(root) if kind == "demo" else _backtests(root)
    match = next((i for i in items if i["id"] == item_id), None)
    if match is None:
        raise KeyError("decision not found.")
    bars: list[dict[str, Any]] = []
    at = datetime.fromisoformat(match["at"])
    if kind in ("paper", "demo"):
        path = root / kind / name / "bars.jsonl"
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
        window = max(10, min(int(window), 300))
        bars = bars[max(0, k - window): k + 12 + window // 4]
    # trade inspector (phase 2, A2): the other side of the same trade, read from the same source's records only -
    # an entry's next exit, an exit's last entry; nothing is computed from them
    same = sorted((i for i in items if i["source"]["name"] == name), key=lambda i: i["at"])
    linked = None
    if match["kind"] == "entry":
        linked = next((i for i in same if i["kind"] == "exit" and i["at"] > match["at"]), None)
    elif match["kind"] == "exit":
        linked = next((i for i in reversed(same) if i["kind"] == "entry" and i["at"] < match["at"]), None)
    base = f"runs/{name}/engine" if kind == "backtest" else f"{kind}/{name}"
    records = {"decision": f"{base}/{'decisions.json' if kind == 'backtest' else 'report.json'}",
               "trace": f"{base}/decision_trace.jsonl" if match["evidence"]["trace"] else None,
               "bars": f"runs/{name}/result.json" if kind == "backtest" else f"{kind}/{name}/bars.jsonl"}
    return {"item": match, "bars": bars, "records": records,
            "linked": None if linked is None else {k: linked[k] for k in ("id", "kind", "at", "fills", "exit_kind")}}
