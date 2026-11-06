"""Phase 2 (owner decision D42): dashboard - four separate money spaces, strategy comparison, status bar."""
from __future__ import annotations

import http.client
import re
from pathlib import Path

from crypto_trader.strategies.ui import server
from crypto_trader.tests.test_strategies_ui import live, ws  # noqa: F401  (server fixtures)

STATIC = Path(server.__file__).with_name("static")
DASH = (STATIC / "dashboard.js").read_text(encoding="utf-8")


def test_the_dashboard_is_the_home_page_and_is_served(live):
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    assert '<script src="/dashboard.js"></script>' in html and "لوحة المتابعة</a>" in html
    assert "PAGES.home = async" in DASH and "PAGES.home = async" not in (STATIC / "app.js").read_text(encoding="utf-8")
    c = http.client.HTTPConnection("127.0.0.1", live, timeout=30)
    c.request("GET", "/dashboard.js", headers={"Host": "127.0.0.1"})
    r = c.getresponse()
    r.read()
    c.close()
    assert r.status == 200 and "javascript" in r.getheader("Content-Type")


def test_four_spaces_each_read_only_their_own_data():
    keys = re.findall(r'\["(portfolio|demo|paper|backtest)", "', DASH[DASH.index("const DB_SPACES"):DASH.index("const dbAt")])
    assert keys == ["portfolio", "demo", "paper", "backtest"]
    # every card / detail reads the space it renders (DB.spaces[k]); no code reaches into another space's data
    uses = re.findall(r"DB\.spaces(\[[^\]]*\]|\.\w+| = \{\})", DASH)
    assert uses and set(uses) <= {"[k]", " = {}"}, uses
    loads = re.findall(r'load\("(\w+)", api\("([^"]+)"\)\)', DASH)
    assert loads == [("portfolio", "/api/portfolio"), ("demo", "/api/demo"), ("paper", "/api/paper"),
                     ("backtest", "/api/runs")]
    src = Path(server.__file__).read_text(encoding="utf-8")
    for path in {p for _, p in loads} | set(re.findall(r'api\("(/api/[a-z/]+)', DASH)):
        assert f'"{path}"' in src, path


def test_each_space_names_its_source_and_nothing_can_trade():
    for src in ("سجل المحفظة · أسعار Binance العامة", "إعادة تشغيل محلية لأسعار تاريخية", "أسعار Binance العامة الحية · محاكاة",
                "سجل التجارب runs.jsonl"):
        assert src in DASH, src
    low = DASH.lower()
    for word in ("/order", "testnet", "mainnet", "api_key", "secret", "withdraw", "/api/paper/start", "/api/paper/new"):
        assert word not in low, word
