"""Unified status and alerts (phase 2, owner decision D42, B3). Read-only facts from the workspace - nothing here fetches
from the network, starts or stops anything.

It tells stale data apart from a stopped process: a paper session whose loop is alive but whose last cycle is older
than two of its bars is "stale" (running, data late); a session whose loop is not alive is "stopped". Prices are judged
from the price cache only (the age of the last closed candle served), the evidence collector from the age of its last
ledger entry. Each item is a code plus the recorded fields; the page words it.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Callable

from crypto_trader.production.environment import MAINNET_ENABLED

__all__ = ["status", "tf_seconds", "PRICE_STALE_S", "COLLECTOR_STALE_S"]

PRICE_STALE_S = 10 * 60          # a 1-minute candle older than this is stale
COLLECTOR_STALE_S = 30 * 60      # the collector writes every 15 minutes
_UNITS = {"m": 60, "h": 3600, "d": 86400, "w": 604800}


def tf_seconds(tf: str) -> int | None:
    m = re.fullmatch(r"(\d+)([mhdw])", str(tf or ""))
    return int(m.group(1)) * _UNITS[m.group(2)] if m else None


def _t(text: Any) -> datetime | None:
    try:
        dt = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _age(now: datetime, text: Any) -> float | None:
    dt = _t(text)
    return None if dt is None else max(0.0, (now - dt).total_seconds())


def status(ws: Any, *, demos: Callable[[], dict[str, Any]] | None = None,
           price_cache: Callable[[], dict[str, Any]] | None = None,
           dca_status: Callable[[], dict[str, Any]] | None = None, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    items: list[dict[str, Any]] = []

    def add(level: str, area: str, code: str, **facts: Any) -> None:
        items.append({"level": level, "area": area, "code": code, **facts})

    add("ok" if not MAINNET_ENABLED else "bad", "engine", "live_disabled" if not MAINNET_ENABLED else "live_enabled")

    def _paper() -> None:
        for s in ws.paper_sessions()["sessions"]:
            if s["error"]:
                add("bad", "paper", "session_error", name=s["name"], at=s["error"].get("at"), detail=str(s["error"].get("error"))[:300])
            if s["running"]:
                age, bar = _age(now, s["last_cycle"]), tf_seconds(s["timeframe"])
                if age is None or (bar and age > 2 * bar + 600):
                    add("warn", "paper", "paper_stale", name=s["name"], age_s=age, last=s["last_cycle"])
                else:
                    add("ok", "paper", "paper_running", name=s["name"], last=s["last_cycle"])
            else:
                add("info", "paper", "paper_stopped", name=s["name"], last=s["last_cycle"])

    def _demo() -> None:
        for d in (demos()["demos"] if demos else []):
            if d.get("state") == "error":
                add("bad", "demo", "demo_error", name=d["name"])

    def _prices() -> None:
        for asset, q in sorted((price_cache() if price_cache else {}).items()):
            if q.get("price") is None:
                add("warn", "prices", "price_unavailable", asset=asset, reason=q.get("reason"))
            else:
                age = _age(now, q.get("at"))
                if age is None or age > PRICE_STALE_S:
                    add("warn", "prices", "price_stale", asset=asset, age_s=age, at=q.get("at"))

    def _collector() -> None:
        ev = ws.system()["evidence_collector"]
        if not ev.get("available"):
            add("info", "collector", "collector_unavailable")
        else:
            age = _age(now, ev.get("last"))
            add("warn" if age is None or age > COLLECTOR_STALE_S else "ok", "collector",
                "collector_stale" if age is None or age > COLLECTOR_STALE_S else "collector_ok", age_s=age, last=ev.get("last"),
                entries=ev.get("entries"))

    def _dca() -> None:
        grouped: dict[tuple[str, str], list[str]] = {}       # one line per asset and state, not one per month
        for r in (dca_status()["reminders"] if dca_status else []):
            grouped.setdefault((r["asset"], r["state"]), []).append(r["month"])
        for (asset, state), months in sorted(grouped.items()):
            add("warn" if state == "missed" else "info", "dca", f"dca_{state}", asset=asset, months=sorted(months),
                count=len(months), month=max(months))

    def _errors() -> None:
        errors, last = 0, None
        log = ws.root / "ui_errors.log"
        if log.exists():
            for line in log.read_text(encoding="utf-8", errors="replace").splitlines():
                m = re.match(r"^\[([^\]]+)\] ", line)
                if m and (age := _age(now, m.group(1))) is not None and age <= 86400:
                    errors, last = errors + 1, m.group(1)
        if errors:
            add("warn", "errors", "ui_errors", count=errors, last=last)

    for area, check in (("paper", _paper), ("demo", _demo), ("prices", _prices), ("collector", _collector), ("dca", _dca),
                        ("errors", _errors)):
        try:
            check()
        except Exception as exc:  # noqa: BLE001 - reported on the bar itself, never raised to the server
            add("warn", area, "check_failed", detail=f"{type(exc).__name__}: {exc}"[:300])

    summary = {k: sum(i["level"] == k for i in items) for k in ("bad", "warn", "info", "ok")}
    return {"at": now.isoformat(), "items": items, "summary": summary}

