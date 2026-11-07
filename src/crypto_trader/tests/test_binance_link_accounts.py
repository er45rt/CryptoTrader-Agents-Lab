"""Owner decision D44, K3: link Binance READ-ONLY - the permission contract is checked before the key is stored and
before every read; a key with any trading / withdrawal / transfer capability is refused and never saved."""
from __future__ import annotations

import pytest

from crypto_trader.mainnet.permissions import parse_api_restrictions
from crypto_trader.strategies.api import StrategyError
from crypto_trader.strategies.ui import accounts, vault

KEY, SECRET = "A1b2C3d4" * 8, "Z9y8X7w6" * 8
READ_ONLY = {"enableReading": True, "enableWithdrawals": False, "enableSpotAndMarginTrading": False,
             "enableMargin": False, "enableFutures": False, "enableVanillaOptions": False, "enableFixApiTrade": False,
             "ipRestrict": True, "enableInternalTransfer": False, "permitsUniversalTransfer": False}


class FakeClient:
    """Stands in for MainnetReadOnlyClient: the real permission parser, scripted bodies, a record of calls."""

    def __init__(self, restrictions, account=None, log=None):
        self.restrictions, self.account, self.log = restrictions, account, log if log is not None else []

    def api_restrictions(self):
        self.log.append("restrictions")
        return parse_api_restrictions(self.restrictions)

    def verify_read_only_permissions(self):
        return self.api_restrictions()

    def account_snapshot(self):
        self.log.append("account")
        return self.account


def _vault(tmp_path):
    return vault.Vault(tmp_path, protect=lambda b: bytes(x ^ 0x5A for x in b), unprotect=lambda b: bytes(x ^ 0x5A for x in b))


def _link(tmp_path, restrictions=READ_ONLY, account=None, log=None, prices=None):
    return accounts.BinanceLink(tmp_path, vault=_vault(tmp_path), prices=prices,
                                client_factory=lambda creds: FakeClient(restrictions, account, log))


def test_a_read_only_key_is_checked_then_stored_encrypted_and_reported_masked(tmp_path):
    link = _link(tmp_path)
    with pytest.raises(StrategyError, match="رمز الدخول"):
        link.connect(KEY, SECRET, pin_set=False)                 # a PIN first
    assert not link.vault.exists()
    st = link.connect(KEY, SECRET, pin_set=True)
    assert st["connected"] and st["key"] == "…" + KEY[-4:] and st["ip_restricted"] is True and st["permission"] == "read_only"
    on_disk = b"".join(p.read_bytes() for p in (tmp_path / "accounts").iterdir())
    assert KEY.encode() not in on_disk and SECRET.encode() not in on_disk


@pytest.mark.parametrize("flag", ["enableWithdrawals", "enableSpotAndMarginTrading", "enableMargin", "enableFutures",
                                  "enableInternalTransfer", "permitsUniversalTransfer"])
def test_a_key_with_any_trading_withdrawal_or_transfer_power_is_refused_and_never_saved(tmp_path, flag):
    link = _link(tmp_path, {**READ_ONLY, flag: True})
    with pytest.raises(StrategyError) as e:
        link.connect(KEY, SECRET, pin_set=True)
    assert not link.vault.exists() and KEY not in str(e.value) and SECRET not in str(e.value)
    assert link.status()["connected"] is False and link.status()["last_error"]


def test_a_key_that_cannot_read_or_an_ambiguous_profile_is_refused(tmp_path):
    for body in ({**READ_ONLY, "enableReading": False}, {k: v for k, v in READ_ONLY.items() if k != "enableWithdrawals"}):
        link = _link(tmp_path, body)
        with pytest.raises(StrategyError):
            link.connect(KEY, SECRET, pin_set=True)
        assert not link.vault.exists()


def test_balances_recheck_permissions_first_and_price_only_what_has_a_price(tmp_path):
    log = []
    account = {"balances": [{"asset": "BTC", "free": "0.5", "locked": "0.1"}, {"asset": "USDT", "free": "100", "locked": "0"},
                            {"asset": "XYZ", "free": "3", "locked": "0"}, {"asset": "ETH", "free": "0", "locked": "0"}]}
    prices = lambda assets: {"BTC": {"price": "70000", "at": "t"}, "XYZ": {"price": None, "reason": "no Binance USDT pair"}}  # noqa: E731
    link = _link(tmp_path, account=account, log=log, prices=prices)
    link.connect(KEY, SECRET, pin_set=True)
    log.clear()
    b = link.balances()
    assert log == ["restrictions", "account"]                       # the contract before the read, every time
    rows = {r["asset"]: r for r in b["balances"]}
    assert set(rows) == {"BTC", "USDT", "XYZ"} and rows["BTC"]["total"] == "0.6" and rows["BTC"]["value"] == "42000.0"
    assert rows["USDT"]["value"] == "100" and rows["XYZ"]["value"] is None and rows["XYZ"]["price_missing"] == "no Binance USDT pair"
    assert b["value_complete"] is False and b["value"] == "42100.0" and link.status()["last_sync"]


def test_a_key_widened_at_binance_after_linking_stops_working_at_once(tmp_path):
    state = {"r": dict(READ_ONLY)}
    link = accounts.BinanceLink(tmp_path, vault=_vault(tmp_path),
                                client_factory=lambda creds: FakeClient(state["r"], {"balances": []}))
    link.connect(KEY, SECRET, pin_set=True)
    state["r"] = {**READ_ONLY, "enableSpotAndMarginTrading": True}
    with pytest.raises(StrategyError):
        link.balances()
    assert link.status()["last_error"]


def test_disconnect_removes_the_key_and_every_synced_file(tmp_path):
    link = _link(tmp_path, account={"balances": []})
    link.connect(KEY, SECRET, pin_set=True)
    (tmp_path / "accounts" / "binance_trades.json").write_text("[]")
    assert link.disconnect() == {"disconnected": True}
    assert not link.vault.exists() and not list((tmp_path / "accounts").glob("binance_*.json"))
    assert link.status()["connected"] is False


def test_the_link_module_has_no_order_or_withdrawal_path():
    from pathlib import Path
    src = Path(accounts.__file__).read_text(encoding="utf-8").lower()
    for word in ("/api/v3/order", "withdraw(", "transfer(", "new_order", "cancel_order(", "mainnet_enabled = true", "post("):
        assert word not in src, word


def test_through_the_server_no_key_or_secret_ever_reaches_a_workspace_file(tmp_path, monkeypatch):
    import http.client
    import json
    import threading

    from crypto_trader.strategies.ui import server

    srv = server.make_server(tmp_path, 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]
    state = {"r": dict(READ_ONLY)}
    monkeypatch.setattr(server.Handler, "link", accounts.BinanceLink(
        tmp_path, vault=_vault(tmp_path), client_factory=lambda creds: FakeClient(state["r"], {"balances": []})))

    def call(method, path, body=None, cookie=None):
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
        h = {"Host": "127.0.0.1", "X-CT-UI": "1", "Content-Type": "application/json", **({"Cookie": cookie} if cookie else {})}
        c.request(method, path, json.dumps(body) if body is not None else None, h)
        r = c.getresponse()
        out = (r.status, json.loads(r.read()), r.getheader("Set-Cookie"))
        c.close()
        return out

    try:
        assert call("POST", "/api/accounts/binance/connect", {"api_key": KEY, "api_secret": SECRET})[0] == 400  # no PIN
        _, _, cookie = call("POST", "/api/auth/setup", {"pin": "482915"})
        session = cookie.split(";")[0]
        assert call("POST", "/api/accounts/binance/connect", {"api_key": KEY, "api_secret": SECRET})[0] == 401
        state["r"] = {**READ_ONLY, "enableWithdrawals": True}
        st, doc, _ = call("POST", "/api/accounts/binance/connect", {"api_key": KEY, "api_secret": SECRET}, session)
        assert st == 400 and KEY not in json.dumps(doc) and SECRET not in json.dumps(doc)
        state["r"] = dict(READ_ONLY)
        st, doc, _ = call("POST", "/api/accounts/binance/connect", {"api_key": KEY, "api_secret": SECRET}, session)
        assert st == 200 and doc["connected"] and doc["key"] == "…" + KEY[-4:]
        st, doc, _ = call("GET", "/api/accounts", cookie=session)
        assert st == 200 and doc["pin_set"] and doc["connected"] and KEY not in json.dumps(doc)
        assert call("GET", "/api/accounts/binance/balances", cookie=session)[0] == 200
        assert call("POST", "/api/accounts/binance/disconnect", {}, session)[1] == {"disconnected": True}
    finally:
        srv.shutdown()
        srv.server_close()
    for f in tmp_path.rglob("*"):
        if f.is_file():
            data = f.read_bytes()
            assert KEY.encode() not in data and SECRET.encode() not in data, f
