"""Bounded, DNS-pinned HTTPS-only web text fetcher for research agents.

No credentials, proxies, redirects, local addresses, or unbounded responses.
Remote content is untrusted input and must never be treated as instructions.
"""
from __future__ import annotations

import ipaddress
import queue
import socket
import threading
import time
from html.parser import HTMLParser
from urllib.parse import urlsplit

import requests
from crewai.tools import BaseTool
from pydantic import BaseModel, Field
from requests.adapters import HTTPAdapter
from urllib3 import connection as urllib3_connection
from urllib3 import connectionpool as urllib3_connectionpool
from urllib3.util import connection as urllib3_socket

MAX_RESPONSE_BYTES = 262_144
MAX_OUTPUT_CHARS = 20_000
TIMEOUT_SECONDS = 8.0
DNS_TIMEOUT_SECONDS = 2.0
_DNS_SLOTS = threading.BoundedSemaphore(value=4)
_BLOCKED_SUFFIXES = (".localhost", ".local", ".internal", ".home.arpa", ".test", ".invalid")


class UnsafeScrapeURL(ValueError):
    """Blocked URL or unsafe web response."""


class _TextOnly(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip = 0
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in ("script", "style", "template", "noscript"):
            self._skip += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style", "template", "noscript") and self._skip:
            self._skip -= 1

    def handle_data(self, data: str) -> None:
        if not self._skip and len(" ".join(self.parts)) < MAX_OUTPUT_CHARS:
            text = data.strip()
            if text:
                self.parts.append(text)


def _public_ip(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return False
    return ip.is_global


def _bounded_dns(host: str):
    if not _DNS_SLOTS.acquire(blocking=False):
        raise UnsafeScrapeURL("DNS resolver capacity exhausted")
    output: queue.Queue = queue.Queue(maxsize=1)

    def resolve():
        try:
            output.put((socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM), None))
        except Exception as exc:
            output.put((None, exc))
        finally:
            _DNS_SLOTS.release()

    threading.Thread(target=resolve, daemon=True, name="safe-scrape-dns").start()
    try:
        results, error = output.get(timeout=DNS_TIMEOUT_SECONDS)
    except queue.Empty as exc:
        raise UnsafeScrapeURL("DNS deadline exceeded") from exc
    if error is not None:
        raise UnsafeScrapeURL("DNS resolution failed") from error
    return results


def _validate_and_pin(url: str) -> tuple[str, str]:
    if not isinstance(url, str) or len(url) > 2048 or any(ord(c) < 32 for c in url):
        raise UnsafeScrapeURL("invalid URL")
    try:
        parsed = urlsplit(url)
        host = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise UnsafeScrapeURL("malformed URL") from exc
    if (parsed.scheme != "https" or not host or parsed.username is not None
            or parsed.password is not None or parsed.fragment or port not in (None, 443)):
        raise UnsafeScrapeURL("HTTPS public host on port 443 only; no credentials or fragments")
    if host == "localhost" or host.endswith(_BLOCKED_SUFFIXES):
        raise UnsafeScrapeURL("local hostname blocked")
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        if not literal.is_global:
            raise UnsafeScrapeURL("non-public IP blocked")
        return host, host
    # Alternative shortened numeric IPv4 forms (such as 127.1) must not reach DNS.
    if host.replace(".", "").isdigit():
        raise UnsafeScrapeURL("numeric IP-like hostname blocked")
    if not _public_ip(host):
        try:
            host = host.encode("idna").decode("ascii").lower()
        except UnicodeError as exc:
            raise UnsafeScrapeURL("invalid hostname") from exc
        if (not host or not all(c.isascii() and (c.isalnum() or c in ".-") for c in host)
                or host.endswith(".") or ".." in host or host.endswith(_BLOCKED_SUFFIXES)):
            raise UnsafeScrapeURL("invalid or local hostname")
        results = _bounded_dns(host)
        addresses = {item[4][0] for item in results}
        if not addresses or not all(_public_ip(ip) for ip in addresses):
            raise UnsafeScrapeURL("DNS contains non-public IP")
        pinned = sorted(addresses)[0]
    else:
        pinned = host
    if not _public_ip(pinned):
        raise UnsafeScrapeURL("non-public IP blocked")
    return host, pinned


class _PinnedConnection(urllib3_connection.HTTPSConnection):
    def __init__(self, *args, pinned_ip: str, **kwargs):
        self._pinned_ip = pinned_ip
        super().__init__(*args, **kwargs)

    def _new_conn(self):
        # Actual TCP socket uses the PREVALIDATED IP, never a second DNS lookup.
        # urllib3's TLS layer still verifies the original hostname via SNI.
        return urllib3_socket.create_connection(
            (self._pinned_ip, self.port), self.timeout,
            source_address=self.source_address, socket_options=self.socket_options,
        )


class _PinnedPool(urllib3_connectionpool.HTTPSConnectionPool):
    ConnectionCls = _PinnedConnection


class _PinnedHTTPSAdapter(HTTPAdapter):
    def __init__(self, host: str, ip: str) -> None:
        super().__init__(max_retries=0)
        self.pool = _PinnedPool(host, port=443, pinned_ip=ip, cert_reqs="CERT_REQUIRED")

    def get_connection_with_tls_context(self, request, verify, proxies=None, cert=None):
        return self.pool

    def get_connection(self, url, proxies=None):
        return self.pool

    def close(self) -> None:
        self.pool.close()
        super().close()


class ScrapeInput(BaseModel):
    website_url: str = Field(..., description="One publicly reachable HTTPS page.")


class SafeScrapeWebsiteTool(BaseTool):
    name: str = "Read website content"
    description: str = "Read bounded text from a public HTTPS web page (no redirects)."
    args_schema: type[BaseModel] = ScrapeInput

    def _run(self, website_url: str) -> str:
        start = time.monotonic()
        host, pinned = _validate_and_pin(website_url)
        session = requests.Session()
        session.trust_env = False  # No HTTP_PROXY, HTTPS_PROXY, .netrc or ambient auth.
        session.proxies = {}
        session.mount("https://", _PinnedHTTPSAdapter(host, pinned))
        try:
            with session.get(
                website_url, allow_redirects=False, stream=True, verify=True,
                timeout=(TIMEOUT_SECONDS, TIMEOUT_SECONDS),
                headers={"User-Agent": "CryptoTrader-ResearchSafeScrape/1.0"},
            ) as response:
                if 300 <= response.status_code < 400:
                    raise UnsafeScrapeURL("redirect blocked")
                response.raise_for_status()
                media_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
                if media_type not in ("text/html", "text/plain"):
                    raise UnsafeScrapeURL("non-text response blocked")
                declared = response.headers.get("Content-Length")
                if declared is not None and int(declared) > MAX_RESPONSE_BYTES:
                    raise UnsafeScrapeURL("response exceeds byte cap")
                body = bytearray()
                for chunk in response.iter_content(chunk_size=16_384):
                    if time.monotonic() - start > TIMEOUT_SECONDS:
                        raise UnsafeScrapeURL("deadline exceeded")
                    body.extend(chunk)
                    if len(body) > MAX_RESPONSE_BYTES:
                        raise UnsafeScrapeURL("response exceeds byte cap")
                text = body.decode("utf-8", errors="replace")
                if media_type == "text/plain":
                    return text[:MAX_OUTPUT_CHARS]
                reader = _TextOnly()
                reader.feed(text)
                return " ".join(reader.parts)[:MAX_OUTPUT_CHARS]
        finally:
            session.close()
