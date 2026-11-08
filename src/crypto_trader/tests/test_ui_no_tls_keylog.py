"""2026-11-08: the paper session failed for hours because its process inherited SSLKEYLOGFILE naming a path that later
refused writes. urllib3 (requests, used by the Binance read-only link) opens that path on every new TLS context too,
and the UI server is long-running. The UI server removes the variable from its own environment before anything starts."""
from __future__ import annotations

import os

import pytest

from crypto_trader.strategies.ui import server


@pytest.fixture()
def unwritable_keylog(tmp_path, monkeypatch):
    bad = tmp_path / "no-such-dir" / "keys.log"
    monkeypatch.setenv("SSLKEYLOGFILE", str(bad))
    return bad


def test_the_link_library_fails_with_the_variable_and_works_without_it(unwritable_keylog):
    from urllib3.util.ssl_ import create_urllib3_context

    with pytest.raises(OSError):
        create_urllib3_context()                      # what every Binance link request did once the path refused writes
    server.no_tls_key_log()
    assert "SSLKEYLOGFILE" not in os.environ
    ctx = create_urllib3_context()
    assert ctx.keylog_filename is None and not unwritable_keylog.exists()


def test_main_removes_the_variable_before_the_server_is_created(unwritable_keylog, tmp_path, monkeypatch):
    seen = {}

    class Stop(Exception):
        pass

    def fake_make_server(workspace, port):
        seen["env"] = os.environ.get("SSLKEYLOGFILE")
        raise Stop()

    monkeypatch.setattr(server, "make_server", fake_make_server)
    with pytest.raises(Stop):
        server.main(["--port", "0", "--no-browser", "--workspace", str(tmp_path)])
    assert seen == {"env": None}
