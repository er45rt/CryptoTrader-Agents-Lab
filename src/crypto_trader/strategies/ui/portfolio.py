"""My Portfolio (owner decision D40, phase 1): the owner's real holdings, entered by hand or imported from CSV.

Local only: the ledger lives in <workspace>/portfolio/ledger.json, never in Git. No wallet or exchange connection,
no keys, no seed phrases, no orders: this module only records and computes.

Accounting - Weighted Average Cost (WAC), per asset, replayed in time order (ties keep entry order):
  buy        qty += q; basis += q*price + quote fee                       (avg = basis / qty)
             a fee paid in the asset itself reduces the quantity received; the basis stays (the fee is capitalised)
  sell       realized += q*price - quote fee - q*avg; basis -= q*avg; qty -= q            (avg unchanged)
             a fee paid in the asset is disposed of at zero: realized -= f*avg; basis -= f*avg; qty -= f
  transfer   between the owner's own wallets: quantity, basis and average are unchanged - never a gain;
             a network fee paid in the asset is a cost: realized -= f*avg; basis -= f*avg; qty -= f
  fee        a stand-alone fee in the asset: same as the transfer fee above
Quote currency is USDT. A sale or fee larger than the quantity held is refused (the ledger can never go negative).
"""
from __future__ import annotations

import json
import os
import threading
import uuid
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, localcontext
from pathlib import Path
from typing import Any, Callable, Iterable

from ..api import StrategyError

__all__ = ["Ledger", "TX_TYPES", "QUOTE", "compute", "parse_tx"]

QUOTE = "USDT"
TX_TYPES = ("buy", "sell", "transfer", "fee")
_ASSET_OK = set("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789")
_TEXT_MAX = 120
_ZERO = Decimal(0)
_MAX_DECIMALS, _MAX_INT_DIGITS = 18, 15   # 33 significant digits at most per value
_PREC = 80                                # compute(): sums and qty*price of bounded values stay exact


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _num(value: Any, name: str, *, allow_zero: bool = False) -> Decimal:
    try:
        d = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        raise StrategyError(f"{name}: لازم يكون رقمًا.") from None
    if not d.is_finite() or d < 0 or (d == 0 and not allow_zero):
        raise StrategyError(f"{name}: لازم يكون رقمًا أكبر من صفر." if not allow_zero else f"{name}: لازم يكون صفرًا أو أكثر.")
    if d == 0:
        return Decimal(0)                       # one spelling of zero ("-0" -> "0")
    # bounded precision (adversarial check of Sol D40 blocker 3): every stored value fits exactly in compute()'s
    # context, so a check never compares a value other than the one that is held
    if -d.as_tuple().exponent > _MAX_DECIMALS or d.adjusted() + 1 > _MAX_INT_DIGITS:
        raise StrategyError(f"{name}: دقة الرقم أكبر من المسموح (حتى {_MAX_DECIMALS} خانة عشرية و{_MAX_INT_DIGITS} خانة قبل الفاصلة).")
    return d


def _when(value: Any) -> str:
    text = str(value or "").strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        raise StrategyError(f"التاريخ {value!r} غير صحيح؛ استخدم مثل 2026-01-05 أو 2026-01-05T10:00:00+00:00.") from None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt = dt.astimezone(timezone.utc)
    if dt > datetime.now(timezone.utc):
        raise StrategyError("التاريخ في المستقبل؛ سجّل العمليات المنفّذة فعلًا فقط.")
    return dt.isoformat()


def _text(value: Any) -> str:
    text = str(value or "").strip()
    if len(text) > _TEXT_MAX or any(ord(c) < 32 for c in text):
        raise StrategyError(f"النص طويل أو فيه رموز غير مسموحة (الحد {_TEXT_MAX} حرفًا).")
    return text


def parse_tx(body: dict[str, Any]) -> dict[str, Any]:
    """Validate one transaction from the form or a CSV row. Raises StrategyError with an Arabic message."""
    kind = str(body.get("type", "")).strip().lower()
    if kind not in TX_TYPES:
        raise StrategyError(f"نوع العملية لازم يكون واحدًا من: {', '.join(TX_TYPES)}.")
    asset = str(body.get("asset", "")).strip().upper()
    if not asset or len(asset) > 15 or not set(asset) <= _ASSET_OK or asset == QUOTE:
        raise StrategyError("رمز الأصل غير صحيح (حروف إنجليزية وأرقام، مثل BTC)؛ USDT هي عملة التسعير ولا تُسجَّل كأصل.")
    tx: dict[str, Any] = {"type": kind, "asset": asset, "at": _when(body.get("at") or body.get("date")),
                          "network": _text(body.get("network")), "wallet": _text(body.get("wallet")),
                          "note": _text(body.get("note"))}
    if kind in ("buy", "sell", "transfer"):
        tx["qty"] = str(_num(body.get("qty", body.get("quantity")), "الكمية"))
    if kind in ("buy", "sell"):
        tx["price"] = str(_num(body.get("price"), "السعر"))
    fee = body.get("fee")
    if kind == "fee":
        tx["fee"] = str(_num(fee, "الرسوم"))
        fee_asset = asset
    else:
        tx["fee"] = str(_num(fee if str(fee or "").strip() else "0", "الرسوم", allow_zero=True))
        fee_asset = str(body.get("fee_asset") or (QUOTE if kind in ("buy", "sell") else asset)).strip().upper()
    if fee_asset not in (QUOTE, asset):
        raise StrategyError(f"عملة الرسوم لازم تكون {QUOTE} أو {asset}.")
    if kind == "transfer" and fee_asset != asset:
        raise StrategyError("رسوم التحويل تُسجَّل بنفس الأصل المحوَّل.")
    tx["fee_asset"] = fee_asset
    plan = str(body.get("dca_plan") or "").strip()
    if plan:
        if kind != "buy":
            raise StrategyError("الربط بخطة الشراء الشهري للمشتريات فقط.")
        tx["dca_plan"] = _text(plan)
    return tx


def compute(txs: Iterable[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Replay the ledger with WAC. Returns per-asset state; raises StrategyError if any step would go negative.
    Runs with an 80-digit decimal context: quantities are exact, so a balance check sees the value actually held."""
    with localcontext() as ctx:
        ctx.prec = _PREC
        return _compute(txs)


def _compute(txs: Iterable[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    state: dict[str, dict[str, Any]] = {}
    for tx in sorted(txs, key=lambda t: (t["at"], t.get("seq", 0))):
        a = state.setdefault(tx["asset"], {"asset": tx["asset"], "qty": _ZERO, "basis": _ZERO, "realized": _ZERO,
                                           "invested": _ZERO, "fees_quote": _ZERO, "fees_asset_cost": _ZERO,
                                           "bought_qty": _ZERO, "sold_qty": _ZERO, "networks": [], "last_at": None,
                                           "n_tx": 0})
        qty, fee = Decimal(tx.get("qty", "0")), Decimal(tx.get("fee", "0"))
        price = Decimal(tx.get("price", "0"))
        avg = a["basis"] / a["qty"] if a["qty"] else _ZERO
        in_asset = tx["fee_asset"] == tx["asset"]

        def dispose(units: Decimal, *, cost: bool) -> None:
            if units > a["qty"]:
                raise StrategyError(f"{tx['asset']} في {tx['at'][:10]}: الكمية المطلوبة {units} أكبر من الرصيد {a['qty']}.")
            out = units * avg
            a["qty"] -= units
            a["basis"] -= out
            if cost:
                a["realized"] -= out
                a["fees_asset_cost"] += out

        if tx["type"] == "buy":
            a["qty"] += qty
            a["basis"] += qty * price + (_ZERO if in_asset else fee)
            a["invested"] += qty * price + (_ZERO if in_asset else fee)
            a["bought_qty"] += qty
            if in_asset:
                # the fee must leave part of THIS purchase, otherwise its cost would vanish from basis and P&L
                if fee >= qty:
                    raise StrategyError(f"{tx['asset']} في {tx['at'][:10]}: رسوم الشراء بنفس الأصل لازم تكون أقل من الكمية المشتراة.")
                a["qty"] -= fee          # capitalised: basis kept
            else:
                a["fees_quote"] += fee
        elif tx["type"] == "sell":
            if qty > a["qty"]:
                raise StrategyError(f"{tx['asset']} في {tx['at'][:10]}: بيع {qty} أكبر من الرصيد {a['qty']}.")
            a["realized"] += qty * price - (_ZERO if in_asset else fee) - qty * avg
            a["basis"] -= qty * avg
            a["qty"] -= qty
            a["sold_qty"] += qty
            if in_asset:
                dispose(fee, cost=True)
            else:
                a["fees_quote"] += fee
        else:  # transfer between own wallets (quantity is not moved out of the portfolio) or a stand-alone fee
            if tx["type"] == "transfer" and qty > a["qty"]:
                raise StrategyError(f"{tx['asset']} في {tx['at'][:10]}: تحويل {qty} أكبر من الرصيد {a['qty']}.")
            dispose(fee, cost=True)
        if a["qty"] == 0:
            a["basis"] = _ZERO          # no rounding residue on a closed position
        if tx.get("network") and tx["network"] not in a["networks"]:
            a["networks"].append(tx["network"])
        a["last_at"] = tx["at"]
        a["n_tx"] += 1
    for a in state.values():
        a["avg_cost"] = a["basis"] / a["qty"] if a["qty"] else None
    return state


def _s(d: Decimal | None, places: int = 8) -> str | None:
    if d is None:
        return None
    q = d.quantize(Decimal(1).scaleb(-places)).normalize()
    return format(q, "f")


class _LedgerLock:
    """One lock per workspace, across threads AND processes (Sol D40 blocker 6): a thread lock plus an OS byte-range
    lock on <workspace>/portfolio/.lock, so two servers on the same workspace can never interleave load -> save."""

    def __init__(self, path: Path, timeout: float = 15.0) -> None:
        self.path, self.timeout = path, timeout
        self._thread = threading.Lock()
        self._fh = None

    def __enter__(self) -> "_LedgerLock":
        import time

        if not self._thread.acquire(timeout=self.timeout):
            raise StrategyError("سجل المحفظة مشغول؛ حاول بعد لحظات.")
        fh = open(self.path, "a+b")
        deadline = time.monotonic() + self.timeout
        try:
            while True:
                try:
                    if os.name == "nt":
                        import msvcrt
                        fh.seek(0)
                        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError:
                    if time.monotonic() > deadline:
                        raise StrategyError("سجل المحفظة مشغول؛ حاول بعد لحظات.") from None
                    time.sleep(0.05)
        except BaseException:
            fh.close()
            self._thread.release()
            raise
        self._fh = fh
        return self

    def __exit__(self, *exc: Any) -> None:
        fh, self._fh = self._fh, None
        try:
            if os.name == "nt":
                import msvcrt
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        finally:
            fh.close()
            self._thread.release()


class Ledger:
    """The owner's transaction ledger in <workspace>/portfolio/ledger.json (atomic writes, one workspace lock)."""

    def __init__(self, root: Path, prices: Callable[[list[str]], dict[str, dict[str, Any]]] | None = None) -> None:
        self.dir = Path(root) / "portfolio"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path = self.dir / "ledger.json"
        self._lock = _LedgerLock(self.dir / ".lock")
        self._prices = prices
        self.plan_check: Callable[[dict[str, Any]], None] | None = None   # set by dca.Plans

    def _load(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        return json.loads(self.path.read_text(encoding="utf-8"))["transactions"]

    def _save(self, txs: list[dict[str, Any]]) -> None:
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({"version": 1, "transactions": txs}, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, self.path)

    def transactions(self) -> list[dict[str, Any]]:
        with self._lock:
            return sorted(self._load(), key=lambda t: (t["at"], t.get("seq", 0)), reverse=True)

    def add(self, body: dict[str, Any], *, source: str = "manual") -> dict[str, Any]:
        return self.add_many([body], source=source)[0]

    def add_many(self, bodies: list[dict[str, Any]], *, source: str, batch: str | None = None,
                 guard: Callable[[list[dict[str, Any]]], None] | None = None) -> list[dict[str, Any]]:
        """All or nothing: every row is validated and the whole ledger replayed before anything is written.
        `guard` sees the ledger as loaded under the lock and may refuse (e.g. a CSV batch already imported)."""
        new = [parse_tx(b) for b in bodies]
        with self._lock:
            for tx in new:
                if tx.get("dca_plan"):    # only an existing plan for the same asset (Sol D40 blocker 4); read
                    if self.plan_check is None:          # under the lock like every other workspace file
                        raise StrategyError("الخطة غير موجودة.")
                    self.plan_check(tx)
            txs = self._load()
            if guard:
                guard(txs)
            seq = max((t.get("seq", 0) for t in txs), default=0)
            for tx in new:
                seq += 1
                tx.update(id=uuid.uuid4().hex[:12], seq=seq, source=source, recorded_at=_now(),
                          **({"batch": batch} if batch else {}))
            compute(txs + new)            # refuses before writing if any balance would go negative
            self._save(txs + new)
        return new

    def delete(self, tx_id: str) -> dict[str, Any]:
        with self._lock:
            txs = self._load()
            keep = [t for t in txs if t["id"] != tx_id]
            if len(keep) == len(txs):
                raise KeyError("العملية غير موجودة.")
            compute(keep)                 # e.g. deleting a buy that a later sale relies on is refused
            self._save(keep)
        return {"deleted": tx_id}

    def delete_batch(self, batch: str) -> dict[str, Any]:
        with self._lock:
            txs = self._load()
            keep = [t for t in txs if t.get("batch") != batch]
            if len(keep) == len(txs):
                raise KeyError("دفعة الاستيراد غير موجودة.")
            compute(keep)
            self._save(keep)
        return {"deleted_batch": batch, "removed": len(txs) - len(keep)}

    def history(self, daily: Callable[[str, Any], dict[str, Any]]) -> dict[str, Any]:
        """Portfolio value and cost basis at each daily close, from the first transaction to the last closed day.
        A day is `complete` only if every asset held that day has a close; otherwise the missing assets are left out
        of `value` (never guessed) and listed in `missing`."""
        from datetime import date, timedelta

        with self._lock:
            txs = sorted(self._load(), key=lambda t: (t["at"], t.get("seq", 0)))
        if not txs:
            return {"points": [], "missing": {}}
        first = date.fromisoformat(txs[0]["at"][:10])
        assets = sorted({t["asset"] for t in txs})
        closes, missing = {}, {}
        for a in assets:
            res = daily(a, first)
            closes[a] = res["closes"]
            if res.get("reason"):
                missing[a] = res["reason"]
        points, i, day = [], 0, first
        last_day = datetime.now(timezone.utc).date() - timedelta(days=1)
        while day <= last_day:
            while i < len(txs) and txs[i]["at"][:10] <= day.isoformat():
                i += 1
            state = compute(txs[:i])
            value, complete, basis = _ZERO, True, _ZERO
            for a, s in state.items():
                basis += s["basis"]
                if s["qty"] > 0:
                    c = closes.get(a, {}).get(day.isoformat())
                    if c is None:
                        complete = False
                    else:
                        value += s["qty"] * Decimal(c)
            points.append({"d": day.isoformat(), "value": _s(value, 2), "basis": _s(basis, 2), "complete": complete})
            day += timedelta(days=1)
        return {"points": points, "missing": missing}

    def asset_view(self, asset: str, daily: Callable[[str, Any], dict[str, Any]], source: str) -> dict[str, Any]:
        """One asset: its daily closes from its first transaction (or 90 days if none) and its transactions."""
        from datetime import date, timedelta

        asset = str(asset).strip().upper()
        if not asset or len(asset) > 15 or not set(asset) <= _ASSET_OK:
            raise StrategyError("رمز الأصل غير صحيح.")
        txs = [t for t in self.transactions() if t["asset"] == asset]
        since = (date.fromisoformat(min(t["at"][:10] for t in txs)) - timedelta(days=30) if txs
                 else datetime.now(timezone.utc).date() - timedelta(days=90))
        res = daily(asset, since)
        return {"asset": asset, "source": source, "closes": res["closes"], "reason": res.get("reason"),
                "transactions": txs}

    def summary(self) -> dict[str, Any]:
        with self._lock:
            txs = self._load()
        state = compute(txs)
        held = sorted(a for a, s in state.items() if s["qty"] > 0)
        quotes = self._prices(held) if (self._prices and held) else {}
        assets, value, priced_basis, unpriced = [], _ZERO, _ZERO, []
        for name in sorted(state, key=lambda n: (state[n]["qty"] == 0, n)):
            s = state[name]
            q = quotes.get(name) or {}
            price = Decimal(q["price"]) if q.get("price") is not None else None
            row = {"asset": name, "qty": _s(s["qty"]), "avg_cost": _s(s["avg_cost"]), "basis": _s(s["basis"], 2),
                   "invested": _s(s["invested"], 2), "realized": _s(s["realized"], 2),
                   "fees_quote": _s(s["fees_quote"], 2), "fees_asset_cost": _s(s["fees_asset_cost"], 2),
                   "networks": s["networks"], "last_at": s["last_at"], "n_tx": s["n_tx"], "closed": s["qty"] == 0,
                   "price": _s(price) if price is not None else None, "price_source": q.get("source"),
                   "price_at": q.get("at"), "price_missing": None, "value": None, "unrealized": None,
                   "unrealized_pct": None}
            if s["qty"] > 0:
                if price is None:
                    row["price_missing"] = q.get("reason") or "no price source"
                    unpriced.append(name)
                else:
                    v = s["qty"] * price
                    row.update(value=_s(v, 2), unrealized=_s(v - s["basis"], 2),
                               unrealized_pct=_s((v - s["basis"]) / s["basis"] * 100, 2) if s["basis"] else None)
                    value += v
                    priced_basis += s["basis"]
            assets.append(row)
        for row in assets:
            row["allocation_pct"] = _s(Decimal(row["value"]) / value * 100, 2) if row["value"] and value else None
        unreal = value - priced_basis
        return {"quote": QUOTE, "assets": assets, "unpriced": unpriced,
                "totals": {"value": _s(value, 2) if value else None,   # priced holdings only; see value_complete
                           "value_complete": not unpriced,
                           "basis": _s(sum((s["basis"] for s in state.values()), _ZERO), 2),
                           "priced_basis": _s(priced_basis, 2),
                           "unrealized": _s(unreal, 2) if value else None,
                           "unrealized_pct": _s(unreal / priced_basis * 100, 2) if priced_basis and value else None,
                           "realized": _s(sum((s["realized"] for s in state.values()), _ZERO), 2),
                           "invested": _s(sum((s["invested"] for s in state.values()), _ZERO), 2),
                           "fees_quote": _s(sum((s["fees_quote"] for s in state.values()), _ZERO), 2)},
                "n_transactions": len(txs), "storage": str(self.path)}
