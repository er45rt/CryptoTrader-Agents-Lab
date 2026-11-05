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
from . import decisions, store

STATIC = Path(__file__).with_name("static")
ALLOWED_HOSTS = {"127.0.0.1", "localhost"}
STATIC_FILES = {"app.js": "text/javascript; charset=utf-8", "i18n.js": "text/javascript; charset=utf-8",
                "decisions.js": "text/javascript; charset=utf-8",
                "app.css": "text/css; charset=utf-8"}
SEALED_FROM = "2025-09-07T00:00:00+00:00"
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
        return True

    # --- routing --------------------------------------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802
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
                "/api/system": lambda: self.ws.system(),
                # decision center (D31): read-only views over the engine's recorded decisions
                "/api/decisions": lambda: decisions.collect(
                    self.ws.root, source=q.get("source") or None, strategy=q.get("strategy") or None,
                    symbol=q.get("symbol") or None, kinds=[k for k in (q.get("kinds") or "").split(",") if k] or None,
                    since=q.get("since") or None, until=q.get("until") or None, limit=int(q.get("limit") or 300)),
                "/api/decision": lambda: decisions.detail(self.ws.root, q["id"]),
            }
            if url.path not in routes:
                return self._send(404, {"error": "not found"})
            self._send(200, routes[url.path]())
        except (StrategyError, KeyError, ValueError, FileNotFoundError) as exc:
            self._send(400, {"error": str(exc)})
        except Exception as exc:  # report, never crash the server
            self.ws.log_error(f"GET {url.path}: {type(exc).__name__}: {exc}\n{traceback.format_exc()}")
            self._send(500, {"error": f"{type(exc).__name__}: {exc}"})

    def do_POST(self) -> None:  # noqa: N802
        if not self._guard(True):
            return
        url = urlparse(self.path)
        try:
            body = self._body()
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
            self._send(404, {"error": "not found"})
        except (StrategyError, KeyError, ValueError, FileNotFoundError, FileExistsError) as exc:
            self._send(400, {"error": str(exc)})
        except Exception as exc:
            self.ws.log_error(f"POST {url.path}: {type(exc).__name__}: {exc}\n{traceback.format_exc()}")
            self._send(500, {"error": f"{type(exc).__name__}: {exc}"})


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
