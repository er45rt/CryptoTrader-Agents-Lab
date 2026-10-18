"""S-01 SSRF guard regressions. Purely offline; all network interfaces are mocked."""
import socket

import pytest
import requests

from crypto_trader.tools import safe_scrape_tool as s


def _public_dns(monkeypatch, addresses=("93.184.216.34",)):
    def resolve(host, port, type):
        assert port == 443 and type == socket.SOCK_STREAM
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 443)) for ip in addresses]
    monkeypatch.setattr(s.socket, "getaddrinfo", resolve)


@pytest.mark.parametrize("url", [
    "http://example.com/", "file:///etc/passwd", "https://localhost/",
    "https://test.localhost/", "https://intranet.local/", "https://a.internal/",
    "https://127.0.0.1/", "https://127.1/", "https://10.0.0.1/",
    "https://172.16.0.1/", "https://192.168.1.3/", "https://169.254.169.254/",
    "https://[::1]/", "https://[fe80::1]/", "https://[fc00::1]/",
    "https://0.0.0.0/", "https://example.com:8080/",
    "https://user:pass@example.com/", "https://example.com/#fragment",
    "https://example.com/\n", "ftp://example.com/", "https://example.test/",
])
def test_rejects_unsafe_urls_before_transport(monkeypatch, url):
    def no_dns(*a, **k):
        raise AssertionError("blocked URL must not resolve")
    monkeypatch.setattr(s.socket, "getaddrinfo", no_dns)
    with pytest.raises(s.UnsafeScrapeURL):
        s.SafeScrapeWebsiteTool()._run(url)


@pytest.mark.parametrize("addresses", [
    ("127.0.0.1",), ("169.254.169.254",), ("10.5.1.4",),
    ("192.168.0.20",), ("172.31.3.4",), ("::1",), ("fe80::1",),
    ("93.184.216.34", "10.0.0.1"),
])
def test_dns_refuses_private_and_mixed_sets(monkeypatch, addresses):
    _public_dns(monkeypatch, addresses)
    with pytest.raises(s.UnsafeScrapeURL, match="non-public"):
        s.SafeScrapeWebsiteTool()._run("https://example.com/a")


def test_dns_rebinding_prevented_by_pinned_transport(monkeypatch):
    _public_dns(monkeypatch)
    host, ip = s._validate_and_pin("https://example.com/a")
    adapter = s._PinnedHTTPSAdapter(host, ip)
    assert adapter.pool.conn_kw["pinned_ip"] == "93.184.216.34"
    addresses = []
    def fake_connect(destination, *args, **kwargs):
        addresses.append(destination)
        return object()
    monkeypatch.setattr(s.urllib3_socket, "create_connection", fake_connect)
    conn = adapter.pool._new_conn()
    conn._new_conn()
    assert addresses == [("93.184.216.34", 443)]
    adapter.close()


class FakeResponse:
    def __init__(self, *, code=200, body=b"<html><body>public news</body></html>", headers=None):
        self.status_code = code
        self._body = body
        self.headers = headers or {"Content-Type": "text/html"}
    def __enter__(self):
        return self
    def __exit__(self, *args):
        return False
    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError("response error")
    def iter_content(self, chunk_size):
        for at in range(0, len(self._body), chunk_size):
            yield self._body[at:at + chunk_size]


def _fake_session_get(monkeypatch, response):
    _public_dns(monkeypatch)
    calls = []
    def fake_get(self, url, **kwargs):
        calls.append((self, url, kwargs))
        return response
    monkeypatch.setattr(requests.Session, "get", fake_get)
    return calls


def test_https_transport_disables_proxies_redirects_auth_and_is_bounded(monkeypatch):
    calls = _fake_session_get(monkeypatch, FakeResponse())
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9090")
    text = s.SafeScrapeWebsiteTool()._run("https://example.com/news")
    session, url, options = calls[0]
    assert text == "public news"
    assert url == "https://example.com/news"
    assert session.trust_env is False and session.proxies == {}
    assert options["allow_redirects"] is False
    assert options["stream"] is True and options["verify"] is True
    assert options["timeout"] == (s.TIMEOUT_SECONDS, s.TIMEOUT_SECONDS)
    assert isinstance(session.adapters["https://"], s._PinnedHTTPSAdapter)


@pytest.mark.parametrize("code", [301, 302, 303, 307, 308])
def test_redirects_are_never_followed(monkeypatch, code):
    _fake_session_get(monkeypatch, FakeResponse(code=code,
        headers={"Content-Type": "text/html", "Location": "http://127.0.0.1/"}))
    with pytest.raises(s.UnsafeScrapeURL, match="redirect"):
        s.SafeScrapeWebsiteTool()._run("https://example.com")


@pytest.mark.parametrize("response", [
    FakeResponse(body=b"hello", headers={"Content-Type": "application/octet-stream"}),
    FakeResponse(headers={"Content-Type": "text/html", "Content-Length": "300000"}),
    FakeResponse(body=b"x" * (s.MAX_RESPONSE_BYTES + 1)),
])
def test_rejects_unsupported_or_excessive_responses(monkeypatch, response):
    _fake_session_get(monkeypatch, response)
    with pytest.raises(s.UnsafeScrapeURL):
        s.SafeScrapeWebsiteTool()._run("https://example.com")


def test_total_deadline_enforced_during_stream(monkeypatch):
    _fake_session_get(monkeypatch, FakeResponse(body=b"hello"))
    sequence = iter([0, 100])
    monkeypatch.setattr(s.time, "monotonic", lambda: next(sequence))
    with pytest.raises(s.UnsafeScrapeURL, match="deadline"):
        s.SafeScrapeWebsiteTool()._run("https://example.com")


def test_toolmanager_uses_secure_scraper_not_legacy_tool():
    from crypto_trader.crew import ToolManager
    tool = ToolManager().get_scrape_tool()
    assert isinstance(tool, s.SafeScrapeWebsiteTool)

def test_dns_resolution_is_bounded_even_if_resolver_stalls(monkeypatch):
    import threading
    completed = threading.Event()
    def stalled(*args, **kwargs):
        completed.wait(0.2)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]
    monkeypatch.setattr(s.socket, "getaddrinfo", stalled)
    monkeypatch.setattr(s, "DNS_TIMEOUT_SECONDS", 0.005)
    try:
        with pytest.raises(s.UnsafeScrapeURL, match="DNS deadline exceeded"):
            s.SafeScrapeWebsiteTool()._run("https://example.com/")
    finally:
        completed.set()
