"""Linked accounts: Binance READ-ONLY (owner decision D44).

Built on the reviewed Phase-15 mainnet read-only layer (crypto_trader.mainnet): MainnetReadOnlyClient can only send GET
requests to an allow-listed set of read endpoints, and the signed permission contract
(MainnetReadOnlyClient.api_restrictions) fails closed unless the key can read and has NO trading, margin, futures, options,
withdrawal or transfer capability. This module adds only:

* connect: the contract is checked BEFORE anything is stored; a key that is not read-only is refused and never saved;
  the pair is then kept only in the DPAPI vault (vault.py). A PIN must be set first (auth.py);
* re-check on every use: each balance read re-validates the permission contract first (a key whose permissions were
  widened at Binance afterwards stops working here at once);
* status: connected / masked key / IP restriction / last sync / last error (errors are the client's redacted text);
* disconnect: the stored pair and every synced file are deleted;
* trade history (K4): synced into a REVIEW file (accounts/binance_trades.json), never straight into the ledger. A
  preview classifies every trade - already imported (same Binance trade id), possible duplicate of a ledger row
  (same date/type/asset/quantity/price/fee), blocked (fee in a third asset such as BNB), or ready - and only ready
  trades the owner selects are imported, each carrying its Binance trade id so it can never be imported twice.
No order, cancel, withdrawal or transfer path exists here or in the client; MAINNET_ENABLED is untouched.
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable

from ...mainnet import MainnetReadOnlyClient
from ...mainnet.credentials import MainnetReadOnlyCredentials
from ...mainnet.errors import MainnetError
from ..api import StrategyError
from .vault import Vault

__all__ = ["BinanceLink", "real_client", "QUOTE"]

QUOTE = "USDT"
PAGE = 1000


def real_client(creds: MainnetReadOnlyCredentials) -> MainnetReadOnlyClient:
    from ...execution.binance_testnet import RequestsTransport, TestnetClock

    client = MainnetReadOnlyClient(credentials=creds, transport=RequestsTransport(timeout_seconds=15.0),
                                   clock=TestnetClock(time_source=time.time, recv_window_ms=5_000))
    client.synchronize_clock()
    return client


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _dec(x: Any) -> Decimal:
    try:
        return Decimal(str(x))
    except (InvalidOperation, ValueError):
        return Decimal(0)


class BinanceLink:
    def __init__(self, root: Path, *, vault: Vault | None = None,
                 client_factory: Callable[[MainnetReadOnlyCredentials], Any] = real_client,
                 prices: Callable[[list[str]], dict[str, dict[str, Any]]] | None = None) -> None:
        self.dir = Path(root) / "accounts"
        self.vault = vault or Vault(root)
        self._client = client_factory
        self._prices = prices
        self.state_path = self.dir / "binance_state.json"

    # --- state (no secret is ever written here) ------------------------------------------------------------
    def _state(self) -> dict[str, Any]:
        try:
            return json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _save_state(self, doc: dict[str, Any]) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_name(self.state_path.name + ".tmp")
        tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, self.state_path)

    def _fail(self, exc: Exception) -> StrategyError:
        from ...mainnet.errors import MainnetEndpointError, ReadOnlyPermissionError

        if isinstance(exc, ReadOnlyPermissionError):       # the key is not strictly read-only (or its profile is unclear)
            msg = "المفتاح مرفوض: صلاحياته ليست قراءة فقط. أنشئ مفتاحًا بصلاحية Enable Reading وحدها — " + str(exc)
        elif isinstance(exc, MainnetEndpointError):        # Binance refused the request (key, IP, time, ...)
            msg = "Binance رفض الطلب (تحقق من المفتاح وتقييد عنوان IP) — " + str(exc)
        elif isinstance(exc, (StrategyError, MainnetError)):
            msg = str(exc)
        else:
            msg = "تعذّر الاتصال بـ Binance — " + type(exc).__name__
        doc = self._state()
        doc.update(last_error=msg[:400], last_error_at=_now())
        self._save_state(doc)
        return StrategyError(msg)

    def status(self) -> dict[str, Any]:
        doc = self._state()
        connected = self.vault.exists()
        key = None
        if connected:
            try:
                key = self.vault.masked_key()
            except StrategyError as exc:
                doc = {**doc, "last_error": str(exc)}
        return {"provider": "binance", "connected": connected, "key": key, "permission": "read_only",
                "connected_at": doc.get("connected_at") if connected else None,
                "ip_restricted": doc.get("ip_restricted") if connected else None,
                "last_sync": doc.get("last_sync") if connected else None,
                "last_error": doc.get("last_error"), "last_error_at": doc.get("last_error_at")}

    # --- connect / disconnect ------------------------------------------------------------------------------
    def connect(self, api_key: Any, api_secret: Any, *, pin_set: bool) -> dict[str, Any]:
        if not pin_set:
            raise StrategyError("أنشئ رمز الدخول (PIN) أولًا، ثم اربط Binance.")
        creds = Vault.credentials(api_key, api_secret)            # format check; values never in a message
        try:
            restrictions = self._client(creds).api_restrictions()  # fails closed unless strictly read-only
        except Exception as exc:  # noqa: BLE001 - nothing is stored on any failure
            raise self._fail(exc) from None
        self.vault.save(creds)
        self._save_state({"connected_at": _now(), "ip_restricted": restrictions.recorded.get("ipRestrict"),
                          "permissions": restrictions.to_record(), "last_sync": None, "last_error": None})
        return self.status()

    def disconnect(self) -> dict[str, Any]:
        removed = self.vault.delete()
        for f in self.dir.glob("binance_*.json") if self.dir.is_dir() else []:
            f.unlink()
        return {"disconnected": removed}

    # --- reads ---------------------------------------------------------------------------------------------
    def client(self) -> Any:
        creds = self.vault.load()
        if creds is None:
            raise StrategyError("حساب Binance غير مربوط.")
        client = self._client(creds)
        client.verify_read_only_permissions()                     # re-checked before every read
        return client

    def balances(self, ledger: Any = None) -> dict[str, Any]:
        try:
            account = self.client().account_snapshot()
        except Exception as exc:  # noqa: BLE001
            raise self._fail(exc) from None
        rows = []
        for b in account.get("balances") or []:
            free, locked = _dec(b.get("free")), _dec(b.get("locked"))
            if free + locked > 0:
                rows.append({"asset": str(b.get("asset")), "free": str(free), "locked": str(locked),
                             "total": str(free + locked)})
        quotes = self._prices(sorted({r["asset"] for r in rows if r["asset"] != "USDT"})) if (self._prices and rows) else {}
        value = Decimal(0)
        for r in rows:
            if r["asset"] == "USDT":
                r.update(price="1", value=r["total"], price_at=None, price_missing=None)
            else:
                q = quotes.get(r["asset"]) or {}
                p = q.get("price")
                r.update(price=p, price_at=q.get("at"), value=str(_dec(p) * _dec(r["total"])) if p is not None else None,
                         price_missing=None if p is not None else (q.get("reason") or "no price source"))
            if r["value"] is not None:
                value += _dec(r["value"])
        if ledger is not None:
            _reconcile(rows, ledger)
        doc = self._state()
        doc.update(last_sync=_now(), last_error=None, last_error_at=None)
        self._save_state(doc)
        return {"balances": rows, "value": str(value), "value_complete": all(r["value"] is not None for r in rows),
                "at": doc["last_sync"]}

    # --- trade history: review first, import only what the owner selects (K4) -----------------------------
    @property
    def trades_path(self) -> Path:
        return self.dir / "binance_trades.json"

    def _staged(self) -> dict[str, Any]:
        try:
            return json.loads(self.trades_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"synced_at": None, "trades": []}

    def sync_trades(self, assets: list[str]) -> dict[str, Any]:
        """Fetch the available history of <asset>USDT for each asset into the review file (merged by trade id)."""
        import re

        assets = sorted({a for a in assets if re.fullmatch(r"[A-Z0-9]{2,15}", str(a)) and a != QUOTE})
        staged = {t["ext_id"]: t for t in self._staged()["trades"]}
        try:
            client = self.client()                                 # permission contract checked first
            for asset in assets:
                symbol, from_id = f"{asset}{QUOTE}", 0
                while True:
                    try:
                        page = client.my_trades(symbol, limit=PAGE, from_id=from_id)
                    except MainnetError as exc:
                        if "-1121" in str(exc) or "Invalid symbol" in str(exc):
                            break                                  # no <asset>USDT market: nothing to fetch
                        raise
                    for t in page:
                        row = _trade_row(symbol, asset, t)
                        staged.setdefault(row["ext_id"], row)
                    if len(page) < PAGE:
                        break
                    from_id = max(int(t["id"]) for t in page) + 1
        except Exception as exc:  # noqa: BLE001
            raise self._fail(exc) from None
        doc = {"synced_at": _now(), "trades": sorted(staged.values(), key=lambda t: (t["at"], t["ext_id"]))}
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = self.trades_path.with_name(self.trades_path.name + ".tmp")
        tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, self.trades_path)
        state = self._state()
        state.update(last_sync=doc["synced_at"], last_error=None, last_error_at=None)
        self._save_state(state)
        return {"synced_at": doc["synced_at"], "trades": len(doc["trades"]), "assets": assets}

    def preview(self, ledger: Any) -> dict[str, Any]:
        """Every staged trade with its status against the ledger; nothing is written."""
        from . import portfolio
        from .portfolio_csv import _key

        with ledger._lock:
            current = ledger._load()
        imported = {t.get("ext_id") for t in current if t.get("ext_id")}
        keys = {_key(t) for t in current}
        rows = []
        for t in self._staged()["trades"]:
            row = {"trade": t, "tx": None, "status": "ready", "reason": None}
            if t["ext_id"] in imported:
                row["status"] = "imported"
            elif t["fee_asset"] not in (QUOTE, t["asset"]) and _dec(t["fee"]) != 0:
                row.update(status="blocked", reason="fee_other_asset")
            else:
                tx = portfolio.parse_tx(_tx_body(t))
                row["tx"] = tx
                if _key(tx) in keys:
                    row.update(status="possible_duplicate", reason="same_as_ledger_row")
            rows.append(row)
        ready = [r["tx"] for r in rows if r["status"] == "ready"]
        replay_error = None
        if ready:
            try:
                portfolio.compute(current + ready)
            except StrategyError as exc:
                replay_error = str(exc)
        counts = {k: sum(r["status"] == k for r in rows) for k in ("ready", "imported", "possible_duplicate", "blocked")}
        return {"synced_at": self._staged()["synced_at"], "rows": rows, "counts": counts, "replay_error": replay_error}

    def import_selected(self, ledger: Any, ext_ids: list[str]) -> dict[str, Any]:
        """Import ONLY the selected trades that the preview marks ready, all or nothing, re-checked under the lock."""
        from .portfolio_csv import _key

        want = {str(x) for x in ext_ids or []}
        if not want:
            raise StrategyError("اختر صفقة واحدة على الأقل للاستيراد.")
        by_id = {t["ext_id"]: t for t in self._staged()["trades"]}
        unknown = want - set(by_id)
        if unknown:
            raise StrategyError("بعض الصفقات المختارة غير موجودة في آخر مزامنة؛ زامن من جديد.")
        rows = {r["trade"]["ext_id"]: r for r in self.preview(ledger)["rows"]}
        not_ready = [x for x in want if rows[x]["status"] != "ready"]
        if not_ready:
            raise StrategyError("لا يُستورد إلا ما حالته «جاهزة»: المكررة والمحجوبة والمستوردة سابقًا تُستثنى.")
        bodies = [_tx_body(by_id[x]) for x in sorted(want, key=lambda x: (by_id[x]["at"], x))]

        def guard(current: list[dict[str, Any]]) -> None:   # under the ledger lock: nothing imported twice
            have = {t.get("ext_id") for t in current}
            keys = {_key(t) for t in current}
            from . import portfolio
            if any(b["ext_id"] in have for b in bodies) or any(_key(portfolio.parse_tx(b)) in keys for b in bodies):
                raise StrategyError("تغيّر السجل بعد المعاينة (صفقة مستوردة أو مطابقة)؛ أعد المعاينة.")

        added = ledger.add_many(bodies, source="binance", guard=guard)
        return {"imported": len(added)}


def _trade_row(symbol: str, asset: str, t: Any) -> dict[str, Any]:
    from datetime import datetime as _dt

    at = _dt.fromtimestamp(int(t["time"]) / 1000, tz=timezone.utc).isoformat()
    return {"ext_id": f"binance:{symbol}:{int(t['id'])}", "symbol": symbol, "asset": asset,
            "side": "buy" if t.get("isBuyer") else "sell", "qty": str(_dec(t.get("qty"))),
            "price": str(_dec(t.get("price"))), "quote_qty": str(_dec(t.get("quoteQty"))),
            "fee": str(_dec(t.get("commission"))), "fee_asset": str(t.get("commissionAsset") or QUOTE), "at": at,
            "order_id": t.get("orderId")}


def _tx_body(t: dict[str, Any]) -> dict[str, Any]:
    fee = _dec(t["fee"])
    return {"type": t["side"], "asset": t["asset"], "at": t["at"], "qty": t["qty"], "price": t["price"],
            "fee": t["fee"] if fee != 0 else "0", "fee_asset": t["fee_asset"] if fee != 0 else QUOTE,
            "note": "Binance", "wallet": "Binance", "ext_id": t["ext_id"]}


def _reconcile(rows: list[dict[str, Any]], ledger: Any) -> None:
    """Cost and profit of a Binance balance only when the recorded Binance history explains it exactly (D44): the
    ledger rows of wallet "Binance" must hold the same quantity as the account. Otherwise both are "not available"
    with the reason - never an estimate from partial history."""
    from . import portfolio

    with ledger._lock:
        txs = [t for t in ledger._load() if str(t.get("wallet") or "").lower() == "binance"]
    state = portfolio.compute(txs) if txs else {}
    for r in rows:
        if r["asset"] == QUOTE:
            r.update(history="quote", ledger_qty=None, cost=None, avg_cost=None, unrealized=None)
            continue
        s = state.get(r["asset"])
        held = s["qty"] if s else None
        r["ledger_qty"] = str(held) if held is not None else None
        total = _dec(r["total"])
        if held is None:
            r["history"] = "missing"                       # no recorded purchase at all
        elif abs(held - total) <= Decimal("1e-8"):
            r["history"] = "complete"
        else:
            r["history"] = "incomplete" if held < total else "exceeds"
        if r["history"] == "complete":
            basis = s["basis"]
            r.update(cost=str(basis.quantize(Decimal("0.01"))), avg_cost=str((basis / held).quantize(Decimal("0.00000001"))) if held else None,
                     unrealized=str((_dec(r["value"]) - basis).quantize(Decimal("0.01"))) if r["value"] is not None else None)
        else:
            r.update(cost=None, avg_cost=None, unrealized=None)
