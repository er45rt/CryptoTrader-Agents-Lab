"""Local GUI server for the strategy engine (D30). Standard library only; binds to 127.0.0.1.

    python -m crypto_trader.strategies.ui [--port 8765] [--workspace PATH]

Safety:
  * loopback only; requests whose Host header is not localhost/127.0.0.1 are refused (DNS rebinding);
  * every POST must carry the header X-CT-UI: 1 (a cross-site page cannot set it without CORS);
  * no credential, key or order endpoint exists; real trading is disabled in the engine source
    (production.environment.MAINNET_ENABLED = False) and the UI only reports that;
  * the engine core is used, never modified: backtests and paper sessions go through the strategies layer.
Workspace (default %USERPROFILE%\\ct_engine_workspace): strategies/, data/, runs/, paper/, settings.json.
"""
from __future__ import annotations

import gzip
import json
import os
import subprocess
import sys
import threading
import time
import traceback
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from ..api import StrategyError
from ..loader import list_strategies, load_strategy
from ..runtime import MarketSpec, RunSettings
from . import accounts, alerts, auth, compare, dca, decisions, demo, portfolio, portfolio_csv, prices, store

STATIC = Path(__file__).with_name("static")
ALLOWED_HOSTS = {"127.0.0.1", "localhost"}
STATIC_FILES = {"app.js": "text/javascript; charset=utf-8", "i18n.js": "text/javascript; charset=utf-8",
                "decisions.js": "text/javascript; charset=utf-8", "demo.js": "text/javascript; charset=utf-8",
                "portfolio.js": "text/javascript; charset=utf-8", "dashboard.js": "text/javascript; charset=utf-8",
                "lang.js": "text/javascript; charset=utf-8", "i18n_en.json": "application/json; charset=utf-8",
                "app.css": "text/css; charset=utf-8"}
SEALED_FROM = "2025-09-07T00:00:00+00:00"


def build_id() -> str:
    """Fingerprint of the UI code on disk (this package's .py/.pyw and every static file). A server reports the one it
    started with, so the launcher and the page can tell a server running older code (owner report 2026-11-07)."""
    import hashlib

    h = hashlib.sha256()
    here = Path(__file__).parent
    for p in sorted([*here.glob("*.py"), *here.glob("*.pyw"), *STATIC.iterdir()], key=lambda p: (p.parent.name, p.name)):
        if p.is_file():
            h.update(f"{p.parent.name}/{p.name}\0".encode())
            h.update(p.read_bytes())
    return h.hexdigest()[:16]


BUILD = build_id()                     # the code this server process is running
_JOBS: dict[str, dict[str, Any]] = {}
_JOBS_LOCK = threading.Lock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_default(o: Any) -> Any:
    if isinstance(o, (Decimal, datetime, Path)):
        return str(o)
    raise TypeError(type(o))


class Handler(BaseHTTPRequestHandler):
    server_version = "CTEngineUI/1"
    ws: store.Workspace  # set on the class by serve()

    def log_message(self, fmt: str, *args: Any) -> None:  # quiet console; errors go to the workspace log
        pass

    # --- plumbing -------------------------------------------------------------------------------------
    def _host_ok(self) -> bool:
        host = (self.headers.get("Host") or "").split(":")[0].strip("[]").lower()
        return host in ALLOWED_HOSTS

    def _send(self, code: int, body: Any, ctype: str = "application/json; charset=utf-8") -> None:
        data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False, default=_json_default).encode("utf-8")
        self.send_response(code)
        # compress: on this machine local responses above ~200 KB are cut by the network filter (reset after ~19 s);
        # a backtest result is ~350 KB raw and ~70 KB gzipped
        if len(data) > 1024 and "gzip" in (self.headers.get("Accept-Encoding") or ""):
            data = gzip.compress(data, 6)
            self.send_header("Content-Encoding", "gzip")
            self.send_header("Vary", "Accept-Encoding")
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self' 'unsafe-inline'")
        cookie = getattr(self, "_cookie", None)
        if cookie:
            self.send_header("Set-Cookie", cookie)
            self._cookie = None
        self.end_headers()
        self.wfile.write(data)

    def _body(self) -> dict[str, Any]:
        n = int(self.headers.get("Content-Length") or 0)
        if n > 2_000_000:
            raise StrategyError("request too large.")
        return json.loads(self.rfile.read(n) or b"{}")

    def _refuse(self, reason: str) -> None:
        # read (and discard) a bounded body first: closing a socket with unread data makes Windows send a
        # reset, and the client would see a broken connection instead of the 403
        n = int(self.headers.get("Content-Length") or 0)
        if 0 < n <= 2_000_000:
            self.rfile.read(n)
        self.close_connection = True
        self._send(HTTPStatus.FORBIDDEN, {"error": reason})

    def _guard(self, write: bool) -> bool:
        if not self._host_ok():
            self._refuse("host not allowed")
            return False
        if write and self.headers.get("X-CT-UI") != "1":
            self._refuse("missing X-CT-UI header")
            return False
        # local PIN login (D44): once a PIN exists, every API call except the public ones needs a live session
        path = urlparse(self.path).path
        public = (path in ("/", "/index.html") or path.lstrip("/") in STATIC_FILES
                  or path in ("/api/system", "/api/auth/status", "/api/auth/login", "/api/auth/setup", "/api/auth/logout"))
        if not public and not self.auth.check(self._token()):
            n = int(self.headers.get("Content-Length") or 0)
            if 0 < n <= 2_000_000:
                self.rfile.read(n)
            self._send(HTTPStatus.UNAUTHORIZED, {"error": "login required", "auth": True})
            return False
        return True

    def _token(self) -> str | None:
        for part in (self.headers.get("Cookie") or "").split(";"):
            name, _, value = part.strip().partition("=")
            if name == "ct_session" and value:
                return value
        return None

    def _send_session(self, token: str | None, body: Any) -> None:
        """Reply with the session cookie set (token) or cleared (None): HttpOnly, SameSite=Strict, this site only."""
        self._cookie = (f"ct_session={token}; HttpOnly; SameSite=Strict; Path=/" if token
                        else "ct_session=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0")
        self._send(200, body)

    # --- routing --------------------------------------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802
        if urlparse(self.path).path == "/api/status":
            return self._get_status()
        if not self._guard(False):
            return
        url = urlparse(self.path)
        try:
            if url.path in ("/", "/index.html"):
                return self._send(200, (STATIC / "index.html").read_bytes(), "text/html; charset=utf-8")
            if url.path.lstrip("/") in STATIC_FILES:      # a fixed list: no path is ever built from the request
                name = url.path.lstrip("/")
                return self._send(200, (STATIC / name).read_bytes(), STATIC_FILES[name])
            q = {k: v[0] for k, v in parse_qs(url.query).items()}
            routes = {
                "/api/home": lambda: self.ws.home(),
                "/api/strategies": lambda: self.ws.strategies(),
                "/api/data": lambda: self.ws.data_files(),
                "/api/runs": lambda: self.ws.runs(),
                "/api/run": lambda: self.ws.run(q["id"]),
                "/api/job": lambda: _job(q["id"]),
                "/api/paper": lambda: self.ws.paper_sessions(),
                "/api/paper/session": lambda: self.ws.paper_detail(q["name"]),
                "/api/risk": lambda: self.ws.risk(),
                "/api/logs": lambda: self.ws.logs(),
                "/api/system": lambda: {**self.ws.system(), "build": BUILD, "disk_build": build_id()},
                # local PIN login (D44)
                "/api/auth/status": lambda: {"enabled": self.auth.enabled(), "logged_in": self.auth.logged_in(self._token())},
                # linked accounts (D44): Binance READ-ONLY
                "/api/accounts": lambda: {**self.link.status(), "pin_set": self.auth.enabled()},
                "/api/accounts/binance/balances": lambda: self.link.balances(),
                # decision center (D31): read-only views over the engine's recorded decisions
                "/api/decisions": lambda: decisions.collect(
                    self.ws.root, source=q.get("source") or None, strategy=q.get("strategy") or None,
                    symbol=q.get("symbol") or None, kinds=[k for k in (q.get("kinds") or "").split(",") if k] or None,
                    since=q.get("since") or None, until=q.get("until") or None, reason=q.get("reason") or None,
                    limit=int(q.get("limit") or 300)),
                "/api/decision": lambda: decisions.detail(self.ws.root, q["id"], window=int(q.get("window") or 40)),
                # demo mode (D32): offline replay of local bars through the unchanged paper engine
                "/api/demo": lambda: self.demo.list(),
                "/api/demo/session": lambda: self.demo.status(q["name"]),
                # my portfolio (D40): the owner's own ledger, local only
                "/api/portfolio": lambda: {**self.ledger.summary(), "transactions": self.ledger.transactions()},
                "/api/portfolio/history": lambda: self.ledger.history(self.prices.daily),
                "/api/portfolio/asset": lambda: self.ledger.asset_view(q["asset"], self.prices.daily, prices.SOURCE),
                # monthly DCA plans (D40): tracking only, executed buys are the ledger's own transactions
                "/api/dca": lambda: self.dca.status(),
                # dashboard (D42 B2): backtests side by side only when comparable
                "/api/compare": lambda: compare.comparison(self.ws.root),
            }
            if url.path not in routes:
                return self._send(404, {"error": "not found"})
            self._send(200, routes[url.path]())
        except (StrategyError, KeyError, ValueError, FileNotFoundError) as exc:
            self._send(400, {"error": str(exc)})
        except Exception as exc:  # report, never crash the server
            self.ws.log_error(f"GET {url.path}: {type(exc).__name__}: {exc}\n{traceback.format_exc()}")
            self._send(500, {"error": f"{type(exc).__name__}: {exc}"})

    def _get_status(self) -> None:
        """Dashboard status/alerts bar (D42 B3): read-only and without network. Its own path so that no error of any
        kind - in the guard, the sources or the response write - reaches a log, a file or stderr (Sol D42 recheck 3)."""
        try:
            if not self._guard(False):
                return
            self._send(200, alerts.status(self.ws, demos=self.demo.list, price_cache=self.prices.cached,
                                          dca_status=self._status_dca))
        except Exception as exc:  # noqa: BLE001 - nothing is logged for this route
            try:
                self._send(500, {"error": f"{type(exc).__name__}: {exc}"})
            except Exception:  # noqa: BLE001 - the client may be gone
                pass

    def _status_dca(self) -> dict[str, Any]:
        """DCA reminders for the status bar: no plans file -> no plans; otherwise read under the ledger lock without ever
        creating its file (a missing lock file is reported on the bar as a failed check, D42 recheck)."""
        if not self.dca.path.exists():
            return {"reminders": []}
        with self.ledger._lock.no_create():
            return self.dca.status(priced=False)

    def do_POST(self) -> None:  # noqa: N802
        if not self._guard(True):
            return
        url = urlparse(self.path)
        try:
            body = self._body()
            if url.path in ("/api/auth/setup", "/api/auth/login"):     # local PIN login (D44)
                if url.path == "/api/auth/setup":
                    self.auth.set_pin(body.get("pin"), token=self._token())
                return self._send_session(self.auth.login(body.get("pin")), {"ok": True})
            if url.path == "/api/accounts/binance/connect":         # D44: read-only key, checked before it is stored
                res = self.link.connect(body.get("api_key"), body.get("api_secret"), pin_set=self.auth.enabled())
                body = None                                            # the pair leaves this handler now
                self.ws._event("accounts.binance.connected", permission="read_only")
                return self._send(200, res)
            if url.path == "/api/accounts/binance/disconnect":
                res = self.link.disconnect()
                self.ws._event("accounts.binance.disconnected")
                return self._send(200, res)
            if url.path == "/api/auth/logout":
                self.auth.logout(self._token())
                return self._send_session(None, {"ok": True})
            if url.path == "/api/strategies/check":
                return self._send(200, self.ws.check_strategy(body["spec"]))
            if url.path == "/api/strategies/upload":
                return self._send(200, self.ws.upload_strategy(body["filename"], body["content"]))
            if url.path == "/api/strategies/remove":
                return self._send(200, self.ws.remove_strategy(body["filename"]))
            if url.path == "/api/backtest":
                return self._send(200, {"job": _start_job(self.ws.backtest, body)})
            if url.path == "/api/paper/new":
                return self._send(200, self.ws.paper_new(body))
            if url.path == "/api/paper/start":
                return self._send(200, self.ws.paper_start(body["name"]))
            if url.path == "/api/paper/stop":
                return self._send(200, self.ws.paper_stop(body["name"]))
            if url.path == "/api/risk":
                return self._send(200, self.ws.save_risk(body))
            if url.path.startswith("/api/demo/"):
                return self._send(200, self._demo_post(url.path.rsplit("/", 1)[1], body))
            if url.path == "/api/portfolio/add":
                try:
                    tx = self.ledger.add_checked(body, allow_duplicate=body.get("allow_duplicate") is True)
                except portfolio.DuplicateTransaction as exc:
                    return self._send(409, {"error": str(exc), "duplicate": True})
                self.ws._event("portfolio.added", id=tx["id"], type=tx["type"], asset=tx["asset"])
                return self._send(200, {"transaction": tx})
            if url.path == "/api/portfolio/delete":
                res = self.ledger.delete(str(body["id"]))
                self.ws._event("portfolio.deleted", id=res["deleted"])
                return self._send(200, res)
            if url.path == "/api/portfolio/import/preview":
                return self._send(200, portfolio_csv.preview(str(body["text"]), self.ledger,
                                                             skip_duplicates=bool(body.get("skip_duplicates"))))
            if url.path == "/api/portfolio/import/confirm":
                res = portfolio_csv.confirm(str(body["text"]), str(body["sha256"]), self.ledger,
                                            skip_duplicates=bool(body.get("skip_duplicates")),
                                            filename=str(body.get("filename") or ""))
                self.ws._event("portfolio.imported", batch=res["batch"], rows=res["imported"])
                return self._send(200, res)
            if url.path == "/api/portfolio/import/undo":
                res = self.ledger.delete_batch(str(body["batch"]))
                self.ws._event("portfolio.import_undone", batch=res["deleted_batch"], rows=res["removed"])
                return self._send(200, res)
            if url.path == "/api/dca/create":
                plan = self.dca.create(body)
                self.ws._event("dca.created", id=plan["id"], asset=plan["asset"])
                return self._send(200, {"plan": plan})
            if url.path == "/api/dca/active":
                return self._send(200, {"plan": self.dca.set_active(str(body["id"]), bool(body.get("active")))})
            if url.path == "/api/dca/link":
                return self._send(200, {"transaction": self.dca.link(str(body["tx_id"]), body.get("plan") or None)})
            self._send(404, {"error": "not found"})
        except (StrategyError, KeyError, ValueError, FileNotFoundError, FileExistsError) as exc:
            self._send(400, {"error": str(exc)})
        except Exception as exc:
            self.ws.log_error(f"POST {url.path}: {type(exc).__name__}: {exc}\n{traceback.format_exc()}")
            self._send(500, {"error": f"{type(exc).__name__}: {exc}"})


def _demo_post(self: "Handler", action: str, body: dict[str, Any]) -> Any:
    d = self.demo
    if action == "new":
        start = datetime.fromisoformat(body["start"]).replace(tzinfo=timezone.utc) if body.get("start") else None
        capital = store._dec(body.get("capital", demo.DEFAULT_CAPITAL), "رأس المال")
        settings = self.ws._run_settings({**body, "capital": str(capital)})
        res = d.create(spec=body["spec"], data=self.ws._allowed_data(body["data"]), start=start, settings=settings,
                       params=dict(body.get("params") or {}), speed=str(body.get("speed", "2")))
        self.ws._event("demo.created", name=res["demo"]["name"], spec=body["spec"])
        if body.get("autostart"):
            res = d.start(res["demo"]["name"])
        return res
    if action == "start":
        return d.start(body["name"], body.get("speed"))
    if action == "pause":
        return d.pause(body["name"])
    if action == "step":
        return d.step(body["name"], int(body.get("n", 1)))
    if action == "speed":
        return d.set_speed(body["name"], str(body["speed"]))
    if action == "delete":
        res = d.delete(body["name"], str(body.get("confirm", "")))
        self.ws._event("demo.deleted", name=body["name"])
        return res
    raise KeyError("not found")


Handler._demo_post = _demo_post   # type: ignore[attr-defined]


def _start_job(fn: Any, body: dict[str, Any]) -> str:
    jid = uuid.uuid4().hex[:12]
    with _JOBS_LOCK:
        _JOBS[jid] = {"id": jid, "state": "running", "started": _now()}

    def work() -> None:
        try:
            result = fn(body)
            state = {"state": "done", "result": result}
        except StrategyError as exc:
            state = {"state": "error", "error": str(exc)}
        except Exception as exc:
            state = {"state": "error", "error": f"{type(exc).__name__}: {exc}"}
        with _JOBS_LOCK:
            _JOBS[jid].update(state, finished=_now())

    threading.Thread(target=work, daemon=True).start()
    return jid


def _job(jid: str) -> dict[str, Any]:
    with _JOBS_LOCK:
        if jid not in _JOBS:
            raise KeyError("unknown job")
        return dict(_JOBS[jid])


def make_server(workspace: Path, port: int = 8765) -> ThreadingHTTPServer:
    Handler.ws = store.Workspace(workspace)
    Handler.auth = auth.Auth(workspace)
    Handler.demo = demo.DemoManager(workspace)
    Handler.prices = prices.BinancePrices(Path(workspace) / "portfolio" / "prices")
    Handler.ledger = portfolio.Ledger(workspace, prices=Handler.prices.latest)
    Handler.link = accounts.BinanceLink(workspace, prices=Handler.prices.latest)   # D44: Binance read-only
    Handler.dca = dca.Plans(Handler.ledger, prices=Handler.prices.latest)
    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


def main(argv: list[str] | None = None) -> int:
    import argparse
    import webbrowser

    ap = argparse.ArgumentParser(prog="python -m crypto_trader.strategies.ui")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--workspace", default=str(Path.home() / "ct_engine_workspace"))
    ap.add_argument("--no-browser", action="store_true")
    a = ap.parse_args(argv)
    srv = make_server(Path(a.workspace), a.port)
    url = f"http://127.0.0.1:{a.port}/"
    print(f"CryptoTrader engine UI on {url}  (workspace: {a.workspace}). Real trading is disabled. Ctrl+C to stop.")
    if not a.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0
