"""Paper session 2026-11-08 (traceback from T1): every cycle failed in ssl.create_default_context() because the process
inherited SSLKEYLOGFILE naming a path that refused writes ('\\\\.\\aswMonFltProxy\\<id>'). The public klines fetches now
build their TLS context without that key log, with the same certificate and hostname verification."""
from __future__ import annotations

import io
import json
import ssl
import urllib.request
from datetime import datetime, timedelta, timezone

import pytest

from crypto_trader.strategies import ohlcv


@pytest.fixture()
def unwritable_keylog(tmp_path, monkeypatch):
    bad = tmp_path / "no-such-dir" / "keys.log"               # opening it for append fails, like the refused device
    monkeypatch.setenv("SSLKEYLOGFILE", str(bad))
    return bad


def test_the_context_ignores_the_key_log_and_still_verifies(unwritable_keylog):
    ctx = ohlcv._tls_context()
    assert ctx.keylog_filename is None and not unwritable_keylog.exists()
    assert ctx.check_hostname is True and ctx.verify_mode == ssl.CERT_REQUIRED
    assert ctx.protocol == ssl.PROTOCOL_TLS_CLIENT


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_both_public_fetches_work_with_an_unwritable_key_log(unwritable_keylog, monkeypatch):
    seen = []

    class Opener:
        def open(self, req, timeout=None):
            return _Resp(json.dumps([]).encode())

    def build_opener(*handlers):
        seen.extend(h for h in handlers if isinstance(h, urllib.request.HTTPSHandler))
        return Opener()

    monkeypatch.setattr(urllib.request, "build_opener", build_opener)
    now = datetime(2026, 11, 8, 6, 0, tzinfo=timezone.utc)
    assert ohlcv.fetch_public_klines("BTCUSDT", "4h", start=now - timedelta(hours=8), now=now) == []
    assert ohlcv.fetch_forming_open("BTCUSDT", "4h", now=now) is None
    assert len(seen) == 2
    for h in seen:
        assert h._context.keylog_filename is None and h._context.verify_mode == ssl.CERT_REQUIRED
        assert h._context.check_hostname is True
