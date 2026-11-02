"""OS archive lock and append-only physical frames. Not a replay reader."""
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import struct
from uuid import uuid4

from .policy import MAX_RECORD_BYTES, RECORD_VERSION, external_destination
from .bootstrap import reserve_standard_fds

MAGIC = b"DARC0001"
END = b"DARCEND1"
HEADER = struct.Struct(">8sQ32s")


class ArchiveLocked(RuntimeError):
    pass


class JournalError(OSError):
    pass


class ArchiveLock:
    def __init__(self, destination):
        self.destination = external_destination(destination)
        self.fd = None

    def __enter__(self):
        reserve_standard_fds()
        self.destination.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.destination / ".collector.lock",
                     os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0))
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            os.close(fd)
            raise ArchiveLocked("archive_already_locked") from error
        self.fd = fd
        return self

    def __exit__(self, *args):
        if self.fd is not None:
            fd, self.fd = self.fd, None
            try:
                os.lseek(fd, 0, os.SEEK_SET)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)


def frame(record):
    payload = json.dumps(record, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=False, allow_nan=False).encode("utf-8")
    if not 0 < len(payload) <= MAX_RECORD_BYTES:
        raise JournalError("evidence_record_exceeds_cap")
    length = struct.pack(">Q", len(payload))
    digest = hashlib.sha256(MAGIC + length + payload).digest()
    return HEADER.pack(MAGIC, len(payload), digest) + payload + END


@dataclass(frozen=True)
class Integrity:
    path: str
    valid_records: int
    valid_bytes: int
    status: str


def inspect_segment(path):
    """Bounded physical integrity validation only; no market/replay interface."""
    reserve_standard_fds()
    valid, offset = 0, 0
    with Path(path).open("rb") as stream:
        while True:
            head = stream.read(HEADER.size)
            if not head:
                return Integrity(str(path), valid, offset, "complete_valid")
            if len(head) != HEADER.size:
                return Integrity(str(path), valid, offset, "partial_trailing")
            magic, size, digest = HEADER.unpack(head)
            if magic != MAGIC or not 0 < size <= MAX_RECORD_BYTES:
                return Integrity(str(path), valid, offset, "corrupt")
            payload, end = stream.read(size), stream.read(len(END))
            if len(payload) != size or len(end) != len(END):
                return Integrity(str(path), valid, offset, "partial_trailing")
            expected = hashlib.sha256(MAGIC + struct.pack(">Q", size) + payload).digest()
            if digest != expected or end != END:
                return Integrity(str(path), valid, offset, "corrupt")
            try:
                record = json.loads(payload)
                if not isinstance(record, dict) or record.get("record_version") != RECORD_VERSION:
                    raise ValueError("record_version")
            except (ValueError, UnicodeDecodeError):
                return Integrity(str(path), valid, offset, "corrupt")
            valid += 1
            offset += HEADER.size + size + len(END)


class Journal:
    """Every append -> userspace flush -> fsync. Never reopen a prior segment.

    Unbuffered FileIO avoids retrying pending bytes when closing a failed writer;
    flush is still explicitly invoked before filesystem sync. Partial writes
    poison the segment; even a complete-looking failed sync is not acknowledged.
    """
    def __init__(self, destination, session_id, *, sync=None):
        reserve_standard_fds()
        self.destination = external_destination(destination)
        self.session_id = session_id
        self.sync = os.fsync if sync is None else sync
        self.previous = [inspect_segment(p) for p in sorted(self.destination.glob("*.darc"))]
        self.file = None
        self.poisoned = False
        self.new_segment()

    def new_segment(self):
        reserve_standard_fds()
        self.close()
        self.path = self.destination / (self.session_id + "-" + uuid4().hex + ".darc")
        fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_EXCL
                     | getattr(os, "O_BINARY", 0))
        self.file = os.fdopen(fd, "ab", buffering=0)
        self.poisoned = False
        if os.name != "nt":
            parent_fd = os.open(self.destination, os.O_RDONLY)
            try:
                os.fsync(parent_fd)
            except Exception:
                self.close()
                self.poisoned = True
                raise
            finally:
                os.close(parent_fd)

    def append(self, record):
        if self.poisoned or self.file is None:
            raise JournalError("journal_writer_unavailable")
        try:
            encoded = frame(record)
            if self.file.write(encoded) != len(encoded):
                raise JournalError("partial_append")
            self.file.flush()
            self.sync(self.file.fileno())
        except Exception as error:
            self.poisoned = True
            raise JournalError("journal_append_flush_or_sync_failed") from error

    def close(self):
        if self.file is not None:
            stream, self.file = self.file, None
            stream.close()
