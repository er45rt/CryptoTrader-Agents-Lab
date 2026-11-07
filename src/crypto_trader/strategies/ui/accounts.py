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
* disconnect: the stored pair and every synced file are deleted.
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

__all__ = ["BinanceLink", "real_client"]


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

    def balances(self) -> dict[str, Any]:
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
        doc = self._state()
        doc.update(last_sync=_now(), last_error=None, last_error_at=None)
        self._save_state(doc)
        return {"balances": rows, "value": str(value), "value_complete": all(r["value"] is not None for r in rows),
                "at": doc["last_sync"]}
