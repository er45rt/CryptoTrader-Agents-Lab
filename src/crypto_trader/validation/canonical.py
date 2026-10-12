"""Strict canonical evidence encoding: offline only, no floats or implicit normalization."""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal, DecimalException, Inexact, InvalidOperation, Rounded, localcontext
from hashlib import sha256
from typing import Mapping
from uuid import UUID
import json
import re
import unicodedata

SCHEMA_ID = "cryptotrader.validation.v1"
SERIALIZER_ID = "strict-json-decimal-utc9-v1"
_HEX = re.compile(r"[0-9a-f]{64}\Z")
_DEC = re.compile(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?\Z")
_TS = re.compile(r"(\d{4}-\d\d-\d\d)T(\d\d:\d\d:\d\d)\.(\d{9})Z\Z")
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z")


class CanonicalError(ValueError):
    pass


def _nfc(value: str) -> str:
    if not isinstance(value, str) or unicodedata.normalize("NFC", value) != value or "\x00" in value:
        raise CanonicalError("only unchanged NFC Unicode strings without NUL accepted")
    return value


def _walk(value, depth=0):
    if depth > 32:
        raise CanonicalError("excessive nesting")
    if value is None or isinstance(value, bool):
        return value
    if type(value) is int:
        if abs(value) > 2**53 - 1:
            raise CanonicalError("integer exceeds exact JSON range")
        return value
    if isinstance(value, str):
        return _nfc(value)
    if type(value) in (tuple, list):
        return [_walk(x, depth + 1) for x in value]
    if isinstance(value, Mapping):
        if len(value) > 4096:
            raise CanonicalError("mapping too large")
        out = {}
        for k, v in value.items():
            k = _nfc(k)
            if k in out:
                raise CanonicalError("duplicate normalized key")
            out[k] = _walk(v, depth + 1)
        return out
    raise CanonicalError("floats/Decimal/opaque objects require explicit schema strings")


def canonical_bytes(value) -> bytes:
    try:
        return json.dumps(
            _walk(value), sort_keys=True, separators=(",", ":"),
            allow_nan=False, ensure_ascii=False,
        ).encode("utf-8")
    except (TypeError, UnicodeError, ValueError) as exc:
        raise CanonicalError("invalid canonical payload") from exc


def parse_json_strict(data: str):
    if not isinstance(data, str):
        raise CanonicalError("JSON input must be text")
    def pairs_unique(items):
        d = {}
        for key, value in items:
            if key in d:
                raise CanonicalError("duplicate key")
            d[key] = value
        return d
    def reject_constant(x):
        raise CanonicalError("nonfinite JSON token")
    try:
        return _walk(json.loads(data, object_pairs_hook=pairs_unique, parse_constant=reject_constant))
    except (TypeError, ValueError) as exc:
        raise CanonicalError("invalid strict JSON") from exc


def decimal_string(value: str, *, quantum: str) -> str:
    if not isinstance(value, str) or not _DEC.fullmatch(value) or (value.startswith("-") and Decimal(value).is_zero()):
        raise CanonicalError("invalid Decimal syntax")
    if not isinstance(quantum, str) or not _DEC.fullmatch(quantum) or Decimal(quantum) <= 0:
        raise CanonicalError("invalid schema quantum")
    try:
        x, q = Decimal(value), Decimal(quantum)
        if not x.is_finite() or not q.is_finite():
            raise CanonicalError("nonfinite Decimal")
        with localcontext() as ctx:
            ctx.prec = max(64, len(value) + len(quantum) + 8)
            ctx.traps[Inexact] = True
            ctx.traps[Rounded] = False
            result = x.quantize(q)
            # quantize() alone only checks exponent, not increments such as 0.05.
            if x % q != 0:
                raise CanonicalError("not an exact multiple of schema quantum")
        if result != x:
            raise CanonicalError("inexact quantum")
        return format(result, "f")
    except (DecimalException, ValueError) as exc:
        raise CanonicalError("invalid decimal precision") from exc


def utc_nanoseconds(value: str) -> str:
    m = _TS.fullmatch(value) if isinstance(value, str) else None
    if m is None:
        raise CanonicalError("require UTC nanosecond timestamp")
    try:
        datetime.strptime(m.group(1) + "T" + m.group(2), "%Y-%m-%dT%H:%M:%S")
    except ValueError as exc:
        raise CanonicalError("invalid calendar timestamp") from exc
    return value


def canonical_uuid(value: str) -> str:
    if not isinstance(value, str) or not _UUID.fullmatch(value):
        raise CanonicalError("noncanonical UUID")
    try:
        if str(UUID(value)) != value:
            raise CanonicalError("noncanonical UUID")
    except ValueError as exc:
        raise CanonicalError("invalid UUID") from exc
    return value


def sha256_text(value: str) -> str:
    if not isinstance(value, str) or not _HEX.fullmatch(value):
        raise CanonicalError("lowercase SHA256 required")
    return value


def input_manifest(values: Mapping[str, str], *, allowed_paths: tuple[str, ...]) -> dict[str, str]:
    if not isinstance(values, Mapping) or not values or len(values) > 2048:
        raise CanonicalError("nonempty bounded manifest required")
    out = {}
    for path, digest in values.items():
        _nfc(path)
        if (
            not path or "\\" in path or path.startswith("/") or ":" in path
            or any(x in {"", ".", ".."} for x in path.split("/"))
            or any(x.lower() in {".git", ".env", ".ssh", "secrets", "credentials"} for x in path.split("/"))
            or path.lower().endswith((".env", ".key", ".pem"))
            or path not in allowed_paths
        ):
            raise CanonicalError("unapproved or sensitive manifest path")
        out[path] = sha256_text(digest)
    return dict(sorted(out.items()))


def content_hash(domain: str, value) -> str:
    """Version-bound canonical digest; encode ns beyond 53-bit JSON as strings.

    The domain is part of the preimage. Semantic or serializer revisions
    require a new hash domain/version; historical digests stay unchanged.
    """
    _nfc(domain)
    if not domain or len(domain) > 128:
        raise CanonicalError("invalid domain")
    return sha256(SCHEMA_ID.encode("ascii") + b"\0" + domain.encode("utf-8") + b"\0" + canonical_bytes(value)).hexdigest()
