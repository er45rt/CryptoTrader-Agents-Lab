"""HTTP transport for the Spot Testnet adapter (Phase 11).

The client depends on the :class:`Transport` protocol only, so every behaviour (rate limits,
timeouts, reconnects, ambiguous writes) is testable offline with a scripted transport.

The request object carries redaction metadata: ``HttpRequest.safe_repr()`` never exposes the API
key header or the signature, and the real transport performs **no logging of its own** so a
credential can never leak through this layer.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from .errors import BinanceTestnetError
from .signing import redact_query_string

__all__ = [
    "API_KEY_HEADER",
    "HttpRequest",
    "HttpResponse",
    "RequestsTransport",
    "ScriptedTransport",
    "Transport",
    "TransportError",
    "TransportTimeout",
]

API_KEY_HEADER = "X-MBX-APIKEY"


class TransportError(BinanceTestnetError):
    """A transport-level failure (DNS, connection reset, TLS, 5xx)."""


class TransportTimeout(TransportError):
    """The request timed out: for a write this is an AMBIGUOUS outcome."""


@dataclass(frozen=True)
class HttpRequest:
    """One request. ``query`` is the final, signed query string."""

    method: str
    url: str
    query: str = ""
    headers: Mapping[str, str] = field(default_factory=dict)
    weight: int = 1
    is_write: bool = False

    def safe_repr(self) -> str:
        """A log-safe description: no API key, no signature, no credential material."""
        base = f"{self.method.upper()} {self.url}"
        redacted = redact_query_string(self.query)
        return f"{base}?{redacted}" if redacted else base

    def redacted_headers(self) -> dict[str, str]:
        return {
            key: ("<redacted>" if key.upper() == API_KEY_HEADER else value)
            for key, value in self.headers.items()
        }


@dataclass(frozen=True)
class HttpResponse:
    """A parsed response."""

    status_code: int
    body: Any = None
    headers: Mapping[str, str] = field(default_factory=dict)

    def header(self, name: str, default: str | None = None) -> str | None:
        wanted = name.lower()
        for key, value in self.headers.items():
            if key.lower() == wanted:
                return value
        return default

    @property
    def retry_after_seconds(self) -> float | None:
        raw = self.header("retry-after")
        if raw is None:
            return None
        try:
            return max(0.0, float(raw))
        except (TypeError, ValueError):
            return None

    @property
    def used_weight(self) -> int | None:
        raw = self.header("x-mbx-used-weight-1m")
        try:
            return int(raw) if raw is not None else None
        except (TypeError, ValueError):
            return None


@runtime_checkable
class Transport(Protocol):
    """The only network surface the client knows about."""

    def send(self, request: HttpRequest) -> HttpResponse: ...


@dataclass
class RequestsTransport:
    """Real HTTPS transport (used only for the live testnet integration run)."""

    timeout_seconds: float = 10.0

    def send(self, request: HttpRequest) -> HttpResponse:
        import requests  # local import keeps the module importable without the dependency

        url = f"{request.url}?{request.query}" if request.query else request.url
        try:
            response = requests.request(
                request.method.upper(),
                url,
                headers=dict(request.headers),
                timeout=self.timeout_seconds,
            )
        except requests.exceptions.Timeout as exc:
            raise TransportTimeout("the request timed out.") from exc
        except requests.exceptions.RequestException as exc:
            raise TransportError(f"transport failure: {type(exc).__name__}") from exc

        try:
            body = response.json() if response.content else None
        except ValueError:
            body = None
        return HttpResponse(
            status_code=response.status_code,
            body=body,
            headers={str(key).lower(): str(value) for key, value in response.headers.items()},
        )


@dataclass
class ScriptedTransport:
    """Deterministic transport double: a queue of responses, optionally then a failure."""

    responses: list[HttpResponse | Exception] = field(default_factory=list)
    requests: list[HttpRequest] = field(default_factory=list)

    def send(self, request: HttpRequest) -> HttpResponse:
        self.requests.append(request)
        if not self.responses:
            raise TransportError("scripted transport has no further responses.")
        nxt = self.responses.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt

    # --- assertions helpers (tests) ---

    def sent_queries(self) -> Sequence[str]:
        return tuple(request.query for request in self.requests)

    def sent_paths(self) -> Sequence[str]:
        return tuple(request.url for request in self.requests)

    def contains(self, needle: str) -> bool:
        """True when any recorded request exposes ``needle`` in a log-safe form."""
        return any(needle in request.safe_repr() or needle in request.query
                   for request in self.requests)
