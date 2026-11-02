"""Read-only, bounded DARC0001 frames. Frame hashes do NOT authenticate custody.

One opened handle supplies both the content hash and parsed records. Reparse
checks and fstat comparisons detect known races but are not immutable snapshots.
"""
from __future__ import annotations

from collections import namedtuple
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from pathlib import Path
from stat import S_ISREG
import json
import os
import re
import struct
import unicodedata

from .dataset_contracts import (
    MAX_ARCHIVE_BYTES, MAX_DEPTH, MAX_FRAME_BYTES, MAX_KEYS, MAX_NUMBER_DIGITS,
    MAX_RECORDS, MAX_STRING_CHARS, RECORD_TYPES,
)
from .canonical import sha256_text

MAGIC = b"DARC0001"
END = b"DARCEND1"
HEADER = struct.Struct(">8sQ32s")
_REPARSE = 0x400
Identity = namedtuple("Identity", "dev ino size mtime_ns")


class DarcError(ValueError):
    """Bad or untrustworthy physical archive input."""


class SegmentEvidence:
    __slots__ = ("digest", "record_count", "size_bytes", "records")
    def __init__(self, digest, record_count, size_bytes, records):
        self.digest = digest
        self.record_count = record_count
        self.size_bytes = size_bytes
        self.records = tuple(records)


def _identity(file):
    stat = os.fstat(file.fileno())
    if not stat.st_dev or not stat.st_ino or stat.st_nlink != 1 or not S_ISREG(stat.st_mode):
        raise DarcError("unverifiable opened file identity or hardlink")
    return Identity(stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)


def _path_identity(path: Path):
    try:
        stat = path.lstat()
        if path.is_symlink() or stat.st_file_attributes & _REPARSE if hasattr(stat, "st_file_attributes") else path.is_symlink():
            raise DarcError("reparse point or symlink forbidden")
        if not stat.st_dev or not stat.st_ino:
            raise DarcError("unverifiable path file identity")
        return stat
    except (OSError, PermissionError) as exc:
        raise DarcError("path identity unavailable") from exc


def _safe_path(raw_path):
    supplied = str(raw_path)
    if (
        not isinstance(raw_path, (Path, str)) or not supplied
        or not Path(supplied).is_absolute()
        or supplied.startswith(("\\\\", "//", "\\\\?\\", "\\\\.\\"))
        or ".." in Path(supplied).parts
        or "\x00" in supplied
    ):
        raise DarcError("only non-UNC explicit absolute paths accepted")
    path = Path(supplied)
    for component in (*reversed(path.parents), path):
        _path_identity(component)
    return path


def _check_depth(text: str):
    depth, in_str, escape = 0, False, False
    for ch in text:
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
        elif ch in "[{":
            depth += 1
            if depth > MAX_DEPTH:
                raise DarcError("JSON nesting limit exceeded")
        elif ch in "]}":
            depth -= 1
            if depth < 0:
                raise DarcError("JSON malformed")
    if in_str or depth != 0:
        raise DarcError("JSON malformed")


def _parse_object(payload: bytes, *, require_record: bool = True):
    try:
        text = payload.decode("utf-8", "strict")
        _check_depth(text)
        # Bound total structural token count BEFORE json.loads() allocates objects.
        if sum(text.count(ch) for ch in ("{", "}", "[", "]", ",", ":")) > 8_192:
            raise DarcError("decoded structural budget exceeded")
        def strict_int(value):
            if len(value) > MAX_NUMBER_DIGITS:
                raise DarcError("integer digit budget exceeded")
            return int(value)
        def strict_float(value):
            if len(value) > MAX_NUMBER_DIGITS:
                raise DarcError("numeric digit budget exceeded")
            try:
                result = Decimal(value)
                if not result.is_finite() or abs(result.adjusted()) > 1000:
                    raise DarcError("nonfinite or extreme numeric exponent")
                return result
            except InvalidOperation as exc:
                raise DarcError("invalid Decimal token") from exc
        def no_constants(value):
            raise DarcError("nonfinite JSON constants not allowed")
        def unique(items):
            if len(items) > MAX_KEYS:
                raise DarcError("JSON key budget exceeded")
            result = {}
            for key, value in items:
                if key in result:
                    raise DarcError("duplicate JSON key")
                result[key] = value
            return result
        record = json.loads(
            text, parse_int=strict_int, parse_float=strict_float,
            parse_constant=no_constants, object_pairs_hook=unique,
        )
        if require_record:
            if not isinstance(record, dict) or type(record.get("record_version")) is not int or record["record_version"] != 1:
                raise DarcError("unsupported record version")
            record_type = record.get("record_type")
            if not isinstance(record_type, str) or record_type not in RECORD_TYPES:
                raise DarcError("unsupported record type")
        elif type(record) not in (dict, list):
            raise DarcError("embedded response JSON must be an object or array")
        def walk(value, level=0):
            if level > MAX_DEPTH:
                raise DarcError("JSON nesting limit")
            if isinstance(value, str):
                if len(value) > MAX_STRING_CHARS or any(0xD800 <= ord(c) <= 0xDFFF for c in value):
                    raise DarcError("unbounded or invalid Unicode text")
            elif isinstance(value, list):
                if len(value) > MAX_KEYS * 10:
                    raise DarcError("oversized JSON array")
                for x in value:walk(x, level+1)
            elif isinstance(value, dict):
                for k, x in value.items():
                    if unicodedata.normalize("NFC", k) != k:
                        raise DarcError("non-NFC controlled JSON key")
                    walk(k, level+1)
                    walk(x, level+1)
        walk(record)
        return record
    except (UnicodeError, ValueError, TypeError, RecursionError, OverflowError) as exc:
        if isinstance(exc, DarcError): raise
        raise DarcError("invalid strict DARC JSON") from exc


def read_segment(
    path, *, expected_sha256: str, expected_size: int,
    max_records: int = MAX_RECORDS,
) -> SegmentEvidence:
    try:
        sha256_text(expected_sha256)
        if (type(expected_size) is not int or not 0 < expected_size <= MAX_ARCHIVE_BYTES
                or type(max_records) is not int or not 0 < max_records <= MAX_RECORDS):
            raise DarcError("invalid declared size/record budget")
        path = _safe_path(path)
        before_path = _path_identity(path)
        if before_path.st_size != expected_size:
            raise DarcError("manifest file byte size mismatch")
        records = []
        file_hash = sha256()
        count_bytes = 0
        with path.open("rb", buffering=0) as stream:
            initial = _identity(stream)
            if (initial.dev, initial.ino) != (before_path.st_dev, before_path.st_ino):
                raise DarcError("path replaced between lstat and open")
            if initial.size != expected_size:
                raise DarcError("opened file size differs from manifest")
            while True:
                header = stream.read(HEADER.size)
                if not header:
                    break
                if len(header) != HEADER.size:
                    raise DarcError("truncated frame header")
                magic, payload_size, digest = HEADER.unpack(header)
                if magic != MAGIC or not 0 < payload_size <= MAX_FRAME_BYTES:
                    raise DarcError("invalid magic or oversized attacker length")
                if count_bytes + HEADER.size + payload_size + len(END) > expected_size:
                    raise DarcError("frame extends beyond manifest size")
                if len(records) >= max_records:
                    raise DarcError("record budget exceeded")
                raw = stream.read(payload_size)
                trailer = stream.read(len(END))
                if len(raw) != payload_size or trailer != END:
                    raise DarcError("truncated or invalid frame tail")
                expected = sha256(MAGIC + struct.pack(">Q", payload_size) + raw).digest()
                if expected != digest:
                    raise DarcError("frame SHA256 mismatch")
                records.append(_parse_object(raw))
                file_hash.update(header)
                file_hash.update(raw)
                file_hash.update(trailer)
                count_bytes += HEADER.size + payload_size + len(END)
            final = _identity(stream)
            if final != initial:
                raise DarcError("file changed while reading")
        after_path = _path_identity(path)
        if (after_path.st_dev, after_path.st_ino, after_path.st_size, after_path.st_mtime_ns) != tuple(initial):
            raise DarcError("file path changed after reading")
        if not records or count_bytes != expected_size or file_hash.hexdigest() != expected_sha256:
            raise DarcError("empty file, trailing bytes or manifest SHA256 mismatch")
        return SegmentEvidence(file_hash.hexdigest(),len(records),count_bytes,records)
    except (OSError, PermissionError) as exc:
        raise DarcError("read-only DARC file unavailable") from exc
