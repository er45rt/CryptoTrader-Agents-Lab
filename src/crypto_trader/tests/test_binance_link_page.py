"""Owner decision D44, K5: the Linked accounts page, and cost / profit of a Binance balance only when the recorded Binance
history explains the balance exactly (otherwise "not available" with the reason)."""
from __future__ import annotations

import re
from pathlib import Path

from crypto_trader.strategies.ui import accounts, portfolio, server
from crypto_trader.tests.test_binance_link_accounts import KEY, READ_ONLY, SECRET, FakeClient, _vault

STATIC = Path(server.__file__).with_name("static")
JS = (STATIC / "accounts.js").read_text(encoding="utf-8")


def test_cost_and_profit_only_when_the_recorded_history_matches_the_balance(tmp_path):
    account = {"balances": [{"asset": "BTC", "free": "0.02", "locked": "0"}, {"asset": "ETH", "free": "1", "locked": "0"},
                            {"asset": "SOL", "free": "3", "locked": "0"}, {"asset": "ADA", "free": "10", "locked": "0"},
                            {"asset": "USDT", "free": "50", "locked": "0"}]}
    prices = lambda a: {k: {"price": v, "at": "t"} for k, v in {"BTC": "100000", "ETH": "3000", "SOL": "150", "ADA": "1"}.items()}  # noqa: E731
    link = accounts.BinanceLink(tmp_path, vault=_vault(tmp_path), prices=prices,
                                client_factory=lambda c: FakeClient(READ_ONLY, account))
    link.connect(KEY, SECRET, pin_set=True)
    lg = portfolio.Ledger(tmp_path)
    for body in ({"asset": "BTC", "qty": "0.02", "price": "90000"},            # complete
                 {"asset": "ETH", "qty": "0.4", "price": "2500"},              # incomplete (holds 1)
                 {"asset": "SOL", "qty": "5", "price": "100"},                 # exceeds (holds 3)
                 {"asset": "ADA", "qty": "10", "price": "1", "wallet": "Ledger"}):   # other wallet: no Binance record
        lg.add({"type": "buy", "at": "2026-01-01", "wallet": "Binance", **body})
    rows = {r["asset"]: r for r in link.balances(lg)["balances"]}
    assert rows["BTC"]["history"] == "complete" and rows["BTC"]["cost"] == "1800.00" and rows["BTC"]["unrealized"] == "200.00"
    for asset, why in (("ETH", "incomplete"), ("SOL", "exceeds"), ("ADA", "missing")):
        assert rows[asset]["history"] == why and rows[asset]["cost"] is None and rows[asset]["unrealized"] is None, asset
    assert rows["USDT"]["history"] == "quote"
    assert link.balances()["balances"][0].get("history") is None        # no ledger given: no reconciliation claims


def test_the_page_is_in_the_menu_served_and_calls_only_existing_read_only_endpoints():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    assert 'data-page="accounts"' in html and '<script src="/accounts.js"></script>' in html
    assert '"accounts.js"' in Path(server.__file__).read_text(encoding="utf-8")
    paths = set(re.findall(r'api\("(/api/[a-z/]+)', JS))
    assert paths == {"/api/accounts", "/api/auth/setup", "/api/accounts/binance/connect", "/api/accounts/binance/disconnect",
                     "/api/accounts/binance/balances", "/api/accounts/binance/sync", "/api/accounts/binance/preview",
                     "/api/accounts/binance/import"}, paths
    src = Path(server.__file__).read_text(encoding="utf-8")
    assert all(f'"{p}"' in src for p in paths)


def test_the_key_fields_are_cleared_at_once_and_nothing_can_trade_or_withdraw():
    on = JS[JS.index("if (on) on.onclick"):JS.index("const off")]
    assert on.index('$("#bn-key").value = ""; $("#bn-secret").value = "";') < on.index('api("/api/accounts/binance/connect"')
    assert 'type="password"' in JS and 'autocomplete="off"' in JS
    low = JS.lower()
    for word in ("/api/v3/order", "withdraw\"", "/order", "testnet", "mainnet", "seed phrase\""):
        assert word not in low, word
    pin = JS[JS.index("async () => {\n    const a = $(\"#pin1\")"):JS.index("function accLink")]
    assert '$("#pin1").value = ""; $("#pin2").value = "";' in pin
