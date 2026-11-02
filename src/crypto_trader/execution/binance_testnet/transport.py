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
from urllib.parse import unquote, urlsplit

from .errors import BinanceTestnetError
from .signing import redact_query_string

__all__ = [
    "API_KEY_HEADER",
    "HttpRequest",
    "HttpResponse",
    "PreSendTransportError",
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


class PreSendTransportError(TransportError):
    """The transport mechanically established that the request never reached the venue.

    Strictly limited to failures raised while *establishing* the connection:

    * DNS resolution failure;
    * connection refused before any connection was established;
    * connect timeout before any connection was established.

    Everything else - TLS/SSL handshake errors, read or write timeouts, resets, aborted or
    half-closed connections, remote disconnects, truncated responses and unknown socket failures -
    is NOT a pre-send proof and must be treated as a post-send unknown outcome.
    """

    reason_code = "pre_send_connection_failure_proven"


def redact_venue_message(message: str, secrets: tuple[str, ...]) -> str:
    """Fail closed on credential echoes after repeated percent-encoding."""
    decoded = message
    for _ in range(8):
        if "signature=" in decoded.lower() or any(secret and secret in decoded for secret in secrets):
            return "<redacted venue message>"
        next_value = unquote(decoded)
        if next_value == decoded:
            return message
        decoded = next_value
    # Do not expose a suspicious message after hitting the bounded decoding limit.
    return "<redacted venue message>"


def safe_transport_error(exc: TransportError) -> TransportError:
    """Keep only the failure classification, never a signed URL or unsafe cause."""
    if isinstance(exc, PreSendTransportError):
        return PreSendTransportError("connection failure proven before transmission.")
    if isinstance(exc, TransportTimeout):
        return TransportTimeout("the request timed out.")
    return TransportError("the transport failed.")


def is_pre_send_connection_failure(exc: BaseException, *, request_url: str) -> bool:
    """Require a requests/urllib3 connection-establishment proof for this HTTPS endpoint.

    Exception class *names*, arbitrary causes and ambient contexts cannot prove that
    the current signed POST was not transmitted. Only the actual MaxRetryError carried
    by requests.ConnectionError, with a recognized urllib3 reason and matching target,
    can establish a safe pre-send classification.
    """
    import requests
    from urllib3.exceptions import MaxRetryError, NameResolutionError, NewConnectionError

    if type(exc) is not requests.exceptions.ConnectionError or len(exc.args) != 1:
        return False
    retry = exc.args[0]
    if type(retry) is not MaxRetryError or type(retry.reason) not in (
        NewConnectionError, NameResolutionError,
    ):
        return False
    pool = retry.pool
    if (
        type(retry.url) is not str or pool is None
        or type(getattr(pool, "host", None)) is not str
        or getattr(pool, "scheme", None) != "https"
    ):
        return False
    target = urlsplit(request_url)
    retry_target = urlsplit(retry.url)
    return (
        target.scheme == "https"
        and type(target.hostname) is str
        and pool.host.lower() == target.hostname.lower()
        and (pool.port or 443) == (target.port or 443)
        and retry_target.path == target.path
    )


@dataclass(frozen=True)
class HttpRequest:
    """One request. ``query`` is the final, signed query string."""

    method: str
    url: str = field(repr=False)
    query: str = field(default="", repr=False)
    headers: Mapping[str, str] = field(default_factory=dict, repr=False)
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
        failure = None
        try:
            # Do not inherit system proxy settings or credential files for signed requests.
            with requests.Session() as session:
                session.trust_env = False
                response = session.request(
                    request.method.upper(),
                    url,
                    headers=dict(request.headers),
                    timeout=self.timeout_seconds,
                    # Redirects are never followed: following one could replay the API key
                    # and signature to another host or silently re-POST a write.
                    allow_redirects=False,
                )
        except requests.exceptions.ConnectTimeout:
            # Connection establishment timed out: no request was transmitted.
            failure = PreSendTransportError(
                "the connection attempt timed out before any request was transmitted."
            )
        except requests.exceptions.Timeout:
            failure = TransportTimeout("the request timed out.")
        except requests.exceptions.ConnectionError as exc:
            if is_pre_send_connection_failure(exc, request_url=request.url):
                failure = PreSendTransportError(
                    "the connection could not be established (resolution/refused) before any "
                    "request was transmitted."
                )
            # SSL/TLS handshake failures, aborted connections and remote disconnects prove nothing
            # about whether request bytes were written.
            else:
                failure = TransportError("the transport failed; transmission is uncertain.")
        except requests.exceptions.RequestException:
            failure = TransportError("the transport failed; transmission is uncertain.")

        # Raise outside the handler so even __context__ cannot retain the signed URL.
        if failure is not None:
            raise failure from None

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
