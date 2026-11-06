"""D40 phase 1, item 5: My Portfolio and My Monthly Buys pages - wired to the local API only, nothing that trades."""
from __future__ import annotations

import http.client
import re
from pathlib import Path

from crypto_trader.strategies.ui import server
from crypto_trader.tests.test_strategies_ui import live, ws  # noqa: F401  (server fixtures)

STATIC = Path(server.__file__).with_name("static")
JS = (STATIC / "portfolio.js").read_text(encoding="utf-8")


def test_pages_are_in_the_menu_and_the_script_is_served(live):
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    assert 'data-page="portfolio"' in html and 'data-page="dca"' in html and '<script src="/portfolio.js">' in html
    assert "PAGES.portfolio = " in JS and "PAGES.dca = " in JS
    conn = http.client.HTTPConnection("127.0.0.1", live, timeout=30)
    conn.request("GET", "/portfolio.js", headers={"Host": "127.0.0.1", "Accept-Encoding": "gzip"})
    resp = conn.getresponse()
    resp.read()
    conn.close()
    assert resp.status == 200 and "javascript" in resp.getheader("Content-Type")


def test_the_pages_call_only_portfolio_and_dca_endpoints_that_exist():
    paths = set(re.findall(r'api\("(/api/[a-z/]+)', JS))
    assert paths and all(p.startswith(("/api/portfolio", "/api/dca")) for p in paths), paths
    src = Path(server.__file__).read_text(encoding="utf-8")
    assert all(f'"{p}"' in src for p in paths), [p for p in paths if f'"{p}"' not in src]


def test_nothing_on_these_pages_can_trade_or_connect_an_account():
    low = JS.lower()
    for word in ("/order", "testnet", "mainnet", "api_key", "apikey", "secret", "seed", "private key", "withdraw"):
        assert word not in low, word


def test_a_page_click_handler_never_outlives_its_page():
    app = (STATIC / "app.js").read_text(encoding="utf-8")
    nav = app[app.index("function nav(page)"):app.index("function nav(page)") + 300]
    assert "main.onclick = null" in nav


def test_sar_is_labelled_approximate_and_missing_prices_say_not_available():
    assert "SAR_RATE = 3.75" in JS and "SAR تقريبي" in JS and "الريال تقريبي" in JS
    assert JS.count("غير متاح") >= 5
