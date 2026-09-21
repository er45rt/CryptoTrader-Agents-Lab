"""Narrow console/error-output hardening for the LLM layer.

Background
----------
CrewAI ships an event-driven console formatter that prints a Rich "LLM Error"
panel. That panel content contains a non-ASCII character (U+274C) and CrewAI also
replaces ``sys.stdout``/``sys.stderr`` with a thin proxy that forwards text to the
original stream unchanged. On a console whose encoding is cp1252 with a *strict*
error handler (common on Windows), that write raises ``UnicodeEncodeError`` -- an
output-encoding failure raised while reporting a *provider* failure. Depending on
where the write lands relative to CrewAI's own ``try``/``except`` blocks, the
encoding error either shows up as console noise (``[EventBus Error] ...``) or
propagates and masks the real provider exception.

This module provides three narrow, process-local mitigations:

* :func:`ascii_safe` keeps our own log lines and error messages ASCII-only, so the
  factory can never fail while formatting its own errors.
* :func:`tolerant_console_writes` temporarily lets unencodable characters be
  replaced instead of raising, but only for streams whose encoding genuinely
  cannot represent the probe characters, and it restores the previous handler on
  exit. It never changes a stream's encoding and never touches environment
  variables or OS settings.
* :func:`recover_original_error` unwraps an encoding failure back to the provider
  error it was masking, so the provider failure is never hidden.

Documented limitation: the formatter that emits the non-encodable panel belongs to
CrewAI, and we do not patch vendor code. If a stream cannot be reconfigured (for
example an in-memory capture stream), the encoding error may still appear as
console noise, but it can no longer replace the provider exception: the factory
re-raises a typed :class:`~crypto_trader.llm.factory.LLMProviderError` chained from
the original provider error.
"""

from __future__ import annotations

import logging
import sys
from contextlib import contextmanager
from typing import Any, Callable, Iterable, Iterator

logger = logging.getLogger("crypto_trader.llm")

__all__ = [
    "ascii_safe",
    "is_console_encoding_error",
    "recover_original_error",
    "tolerant_console_writes",
]

# Characters CrewAI/Rich emit in error panels (heavy cross, box drawing).
_PROBE_CHARACTERS = "\u274c\u250c\u2500\u2502\u2514\u2518"


def ascii_safe(text: Any) -> str:
    """Return ``text`` as an ASCII-only string that no console encoding can reject."""
    return str(text).encode("ascii", "replace").decode("ascii")


def is_console_encoding_error(exc: BaseException) -> bool:
    """True when ``exc`` is a text-encoding failure (``UnicodeEncodeError`` etc.)."""
    return isinstance(exc, UnicodeError)


def recover_original_error(exc: BaseException) -> BaseException | None:
    """Return the error that was being handled when an encoding failure occurred.

    Walks the exception chain past nested encoding failures; returns ``None`` when
    no underlying error was attached.
    """
    candidate = exc.__context__ or exc.__cause__
    while candidate is not None and is_console_encoding_error(candidate):
        candidate = candidate.__context__ or candidate.__cause__
    return candidate


def _stream_cannot_encode_probe(stream: Any) -> bool:
    """True only if the stream's encoding genuinely cannot represent our probe text."""
    encoding = getattr(stream, "encoding", None)
    if not encoding:
        return False
    try:
        _PROBE_CHARACTERS.encode(encoding)
    except (UnicodeEncodeError, LookupError):
        return True
    return False


def _underlying_stream(stream: Any) -> Any:
    """Unwrap CrewAI's stream proxy (``FilteredStream``) to the real text stream.

    CrewAI replaces ``sys.stdout``/``sys.stderr`` with a proxy that forwards writes
    to the original stream. The proxy inherits ``io.TextIOBase``, whose ``errors``
    property returns ``None``, so the proxy must be unwrapped before we can inspect
    or adjust the real error handler.
    """
    return getattr(stream, "_original_stream", None) or stream


@contextmanager
def tolerant_console_writes(streams: Iterable[Any] | None = None) -> Iterator[None]:
    """Temporarily make non-encodable console output non-fatal.

    Only streams that (a) expose ``reconfigure``, (b) currently use the ``strict``
    error handler, and (c) cannot encode the probe characters are touched. The
    previous error handler is restored on exit, including on exceptions.

    Args:
        streams: Streams to consider; defaults to the current ``sys.stdout`` and
            ``sys.stderr`` (resolved at call time).
    """
    candidates = list(streams) if streams is not None else [sys.stdout, sys.stderr]
    restore: list[tuple[Callable[..., Any], str]] = []

    for stream in candidates:
        try:
            target = _underlying_stream(stream)
            reconfigure = getattr(target, "reconfigure", None)
            if reconfigure is None:
                continue
            previous_errors = getattr(target, "errors", None)
            if isinstance(previous_errors, str) and previous_errors != "strict":
                # Already tolerant (e.g. "replace"); nothing to do.
                continue
            if not _stream_cannot_encode_probe(target):
                continue
            reconfigure(errors="replace")
            restore.append(
                (reconfigure, previous_errors if isinstance(previous_errors, str) else "strict")
            )
        except Exception:  # noqa: BLE001 - hardening must never break a provider call
            continue

    try:
        yield
    finally:
        for reconfigure, previous_errors in reversed(restore):
            try:
                reconfigure(errors=previous_errors)
            except Exception:  # noqa: BLE001 - best-effort restoration
                logger.debug("Could not restore console error handler during cleanup.")
