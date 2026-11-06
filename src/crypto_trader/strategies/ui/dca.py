"""Monthly DCA plans (owner decision D40): tracking and analysis ONLY - nothing here buys anything, ever.

A plan says "buy <amount> USDT of <asset> on day <d> of every month from <start>". Executed purchases are NOT stored
here: they are ordinary `buy` transactions in the portfolio ledger carrying `dca_plan = <plan id>` (one ledger, no
double counting). Each month is: executed (a linked buy exists that month) | due (planned for today) | missed (planned
date passed, nothing linked) | upcoming. Totals are contributions and their current market value - never income or
engine profit. Plans live in <workspace>/portfolio/dca.json.
"""
from __future__ import annotations

import calendar
import json
import os
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Callable

from ..api import StrategyError
from . import portfolio

__all__ = ["Plans"]

_ZERO = Decimal(0)


def _month(text: Any) -> tuple[int, int]:
    try:
        y, m = (int(x) for x in str(text).strip()[:7].split("-"))
        date(y, m, 1)
    except (ValueError, TypeError):
        raise StrategyError("شهر البداية لازم يكون مثل 2026-01.") from None
    return y, m


def _months(start: tuple[int, int], end: tuple[int, int]) -> list[tuple[int, int]]:
    out, (y, m) = [], start
    while (y, m) <= end:
        out.append((y, m))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def _in_plan(plan: dict[str, Any], at: str) -> bool:
    """A buy belongs to a plan only inside its months: from the start month to the stop date (if stopped).
    Quantity and paid are both computed from exactly these buys (Sol D40 blocker 5)."""
    if at[:7] < plan["start"]:
        return False
    return not plan.get("stopped_at") or at[:10] <= plan["stopped_at"]


class Plans:
    def __init__(self, ledger: portfolio.Ledger, prices: Callable[[list[str]], dict[str, dict[str, Any]]] | None = None,
                 today: Callable[[], date] = lambda: datetime.now(timezone.utc).date()) -> None:
        self.ledger, self._prices, self._today = ledger, prices, today
        self.path = ledger.dir / "dca.json"
        ledger.plan_check = self.check      # every ledger write tagged with a plan is validated here

    def check(self, tx: dict[str, Any]) -> None:
        """A buy may carry dca_plan only for an existing plan of the same asset."""
        plan = next((p for p in self._load() if p["id"] == tx.get("dca_plan")), None)
        if plan is None:
            raise StrategyError("الخطة غير موجودة.")
        if tx.get("type") != "buy" or tx.get("asset") != plan["asset"]:
            raise StrategyError("يُربط بالخطة شراءٌ لنفس الأصل فقط.")
        if not _in_plan(plan, tx["at"]):
            raise StrategyError("تاريخ الشراء خارج فترة الخطة.")

    def _load(self) -> list[dict[str, Any]]:
        return json.loads(self.path.read_text(encoding="utf-8"))["plans"] if self.path.exists() else []

    def _save(self, plans: list[dict[str, Any]]) -> None:
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({"version": 1, "plans": plans}, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, self.path)

    # --- plans -----------------------------------------------------------------------------------------
    def create(self, body: dict[str, Any]) -> dict[str, Any]:
        probe = portfolio.parse_tx({"type": "buy", "asset": body.get("asset"), "at": "2020-01-01", "qty": "1",
                                    "price": "1"})           # same asset rules as the ledger
        amount = portfolio._num(body.get("amount"), "المبلغ الشهري")
        try:
            day = int(str(body.get("day", "")).strip())
        except ValueError:
            day = 0
        if not 1 <= day <= 28:
            raise StrategyError("يوم الشراء لازم يكون بين 1 و28 (حتى يوجد في كل شهر).")
        start = _month(body.get("start") or self._today().isoformat())
        plan = {"id": uuid.uuid4().hex[:10], "asset": probe["asset"], "amount": str(amount), "day": day,
                "start": f"{start[0]:04d}-{start[1]:02d}", "active": True, "note": portfolio._text(body.get("note")),
                "created_at": datetime.now(timezone.utc).isoformat()}
        with self.ledger._lock:
            self._save(self._load() + [plan])
        return plan

    def set_active(self, plan_id: str, active: bool) -> dict[str, Any]:
        with self.ledger._lock:
            plans = self._load()
            plan = next((p for p in plans if p["id"] == plan_id), None)
            if plan is None:
                raise KeyError("الخطة غير موجودة.")
            plan["active"] = bool(active)
            if not active:
                plan["stopped_at"] = self._today().isoformat()
            else:
                plan.pop("stopped_at", None)
            self._save(plans)
        return plan

    def link(self, tx_id: str, plan_id: str | None) -> dict[str, Any]:
        """Mark an existing ledger buy as (not) executed for a plan. Same asset only; the ledger stays one source."""
        with self.ledger._lock:
            plans = {p["id"]: p for p in self._load()}
            txs = self.ledger._load()
            tx = next((t for t in txs if t["id"] == tx_id), None)
            if tx is None:
                raise KeyError("العملية غير موجودة.")
            if plan_id:
                if plan_id not in plans:
                    raise KeyError("الخطة غير موجودة.")
                self.check({**tx, "dca_plan": plan_id})
                tx["dca_plan"] = plan_id
            else:
                tx.pop("dca_plan", None)
            self.ledger._save(txs)
        return tx

    # --- status ----------------------------------------------------------------------------------------
    def status(self) -> dict[str, Any]:
        today = self._today()
        plans = self._load()
        txs = self.ledger._load()
        quotes = self._prices(sorted({p["asset"] for p in plans})) if (self._prices and plans) else {}
        out, reminders = [], []
        for p in plans:
            buys = sorted((t for t in txs if t.get("dca_plan") == p["id"] and t["type"] == "buy"
                           and t["asset"] == p["asset"] and _in_plan(p, t["at"])), key=lambda t: t["at"])
            stop = date.fromisoformat(p["stopped_at"]) if p.get("stopped_at") else None
            last = (stop or today)
            months = []
            for (y, m) in _months(_month(p["start"]), (last.year, last.month)):
                planned = date(y, m, min(p["day"], calendar.monthrange(y, m)[1]))
                done = [t for t in buys if t["at"][:7] == f"{y:04d}-{m:02d}"]
                paid = sum((Decimal(t["qty"]) * Decimal(t["price"]) +
                            (Decimal(t["fee"]) if t["fee_asset"] != t["asset"] else _ZERO) for t in done), _ZERO)
                state = ("executed" if done else "due" if planned == today else
                         "missed" if planned < today else "upcoming")
                if stop and planned > stop and not done:
                    continue
                months.append({"month": f"{y:04d}-{m:02d}", "planned_date": planned.isoformat(),
                               "planned_amount": p["amount"], "state": state, "paid": portfolio._s(paid, 2),
                               "tx_ids": [t["id"] for t in done]})
                if state in ("due", "missed") and p["active"]:
                    reminders.append({"plan": p["id"], "asset": p["asset"], "month": months[-1]["month"],
                                      "planned_date": planned.isoformat(), "state": state})
            qty = sum((Decimal(t["qty"]) - (Decimal(t["fee"]) if t["fee_asset"] == t["asset"] else _ZERO)
                       for t in buys), _ZERO)
            paid = sum((Decimal(t["qty"]) * Decimal(t["price"]) +
                        (Decimal(t["fee"]) if t["fee_asset"] != t["asset"] else _ZERO) for t in buys), _ZERO)
            outside = [t["id"] for t in txs if t.get("dca_plan") == p["id"] and t not in buys]
            q = quotes.get(p["asset"]) or {}
            price = Decimal(q["price"]) if q.get("price") is not None else None
            value = qty * price if price is not None else None
            nxt = None
            if p["active"]:
                y, m = today.year, today.month
                for _ in range(2):
                    d = date(y, m, min(p["day"], calendar.monthrange(y, m)[1]))
                    cur = next((x for x in months if x["month"] == f"{y:04d}-{m:02d}"), None)
                    if d >= today and not (cur and cur["state"] == "executed"):
                        nxt = d.isoformat()
                        break
                    y, m = (y + 1, 1) if m == 12 else (y, m + 1)
            out.append({**p, "months": months, "n_executed": sum(m["state"] == "executed" for m in months),
                        "n_missed": sum(m["state"] == "missed" for m in months),
                        "qty": portfolio._s(qty), "paid": portfolio._s(paid, 2),
                        "avg_cost": portfolio._s(paid / qty) if qty else None,
                        "price": portfolio._s(price) if price is not None else None,
                        "price_source": q.get("source"), "price_at": q.get("at"),
                        "price_missing": (q.get("reason") or "no price source") if price is None else None,
                        "value": portfolio._s(value, 2) if value is not None else None,
                        "value_minus_paid": portfolio._s(value - paid, 2) if value is not None else None,
                        "next_purchase": nxt, "linked_outside_plan": outside})
        return {"plans": out, "reminders": reminders, "today": today.isoformat(),
                "note": "tracking only - no purchase is ever placed by this app"}
