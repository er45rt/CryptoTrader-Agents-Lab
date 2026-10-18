"""Offline physical journal/OS-lock tests. No replay reader is implemented."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

from crypto_trader.collector.journal import (
    ArchiveLock, ArchiveLocked, Journal, JournalError, HEADER, END, frame, inspect_segment,
)
from crypto_trader.collector.policy import RECORD_VERSION, external_destination


def record(kind="fixture"):
    return {"record_version": RECORD_VERSION, "record_type": kind}


@pytest.mark.parametrize("kind", ["bootstrap", "collector_start", "before_transport",
                                  "after_outcome", "raw_observation", "failed_poll",
                                  "collection_gap", "heartbeat", "clean_stop"])
def test_every_record_append_flush_fsync_before_return(tmp_path, kind):
    calls = []
    journal = Journal(tmp_path, "session", sync=lambda fd: (os.fsync(fd), calls.append("fsync")))
    actual = journal.file
    class Trace:
        def write(self, data):
            calls.append("append")
            return actual.write(data)
        def flush(self):
            calls.append("flush")
            actual.flush()
        def fileno(self):
            return actual.fileno()
        def close(self):
            actual.close()
    journal.file = Trace()
    journal.append(record(kind))
    assert calls == ["append", "flush", "fsync"]
    assert inspect_segment(journal.path).valid_records == 1
    journal.close()


def test_restart_never_overwrites_prior_and_new_session(tmp_path):
    first = Journal(tmp_path, "first")
    first.append(record())
    path = first.path
    before = path.read_bytes()
    first.close()
    second = Journal(tmp_path, "second")
    second.append(record("new"))
    assert path.read_bytes() == before
    assert second.path != path
    assert second.previous[0].status == "complete_valid"
    second.close()


@pytest.mark.parametrize("cut", [1, HEADER.size - 1, HEADER.size, -1])
def test_partial_tail_classified_and_preserved_on_restart(tmp_path, cut):
    complete = frame(record())
    partial = complete[:cut]
    old = tmp_path / "crashed.darc"
    old.write_bytes(complete + partial)
    result = inspect_segment(old)
    assert result.status == "partial_trailing"
    assert result.valid_records == 1
    second = Journal(tmp_path, "restart")
    second.append(record("collector_start"))
    assert old.read_bytes() == complete + partial
    assert second.previous[0].status == "partial_trailing"
    second.close()


@pytest.mark.parametrize("offset", [0, HEADER.size - 1, HEADER.size + 1, -1])
def test_corruption_never_valid(tmp_path, offset):
    encoded = bytearray(frame(record()))
    encoded[offset] ^= 1
    path = tmp_path / "bad.darc"
    path.write_bytes(encoded)
    assert inspect_segment(path).status == "corrupt"


def test_wrong_record_version_rejected(tmp_path):
    path = tmp_path / "version.darc"
    path.write_bytes(frame({"record_version": RECORD_VERSION + 1}))
    assert inspect_segment(path).status == "corrupt"


def test_fsync_failure_poisoned_no_more_append(tmp_path):
    def fail(fd):
        raise OSError("fixture")
    journal = Journal(tmp_path, "session", sync=fail)
    with pytest.raises(JournalError):
        journal.append(record())
    prior = journal.path.read_bytes()
    with pytest.raises(JournalError, match="unavailable"):
        journal.append(record())
    assert journal.path.read_bytes() == prior
    journal.close()


def test_partial_write_poisoned_no_close_retry(tmp_path):
    journal = Journal(tmp_path, "session")
    actual = journal.file
    class ShortWriter:
        def write(self, value):
            return actual.write(value[:3])
        def close(self):
            actual.close()
    journal.file = ShortWriter()
    with pytest.raises(JournalError):
        journal.append(record())
    journal.close()
    assert inspect_segment(journal.path).status == "partial_trailing"
    assert journal.path.stat().st_size == 3


def test_same_archive_rejected_different_archive_allowed(tmp_path):
    with ArchiveLock(tmp_path / "one"):
        with pytest.raises(ArchiveLocked):
            with ArchiveLock(tmp_path / "one"):
                pytest.fail("second owner accepted")
        with ArchiveLock(tmp_path / "two"):
            pass
    with ArchiveLock(tmp_path / "one"):
        pass


@pytest.mark.parametrize("crash", [False, True])
def test_process_lock_contention_and_os_release(tmp_path, crash):
    script = """
import os,sys,time
from crypto_trader.collector.journal import ArchiveLock
with ArchiveLock(sys.argv[1]):
    print('locked',flush=True)
    if sys.argv[2] == 'crash':
        sys.stdin.readline()
        os._exit(91)
    sys.stdin.readline()
"""
    child = subprocess.Popen([sys.executable, "-B", "-c", script, str(tmp_path),
                              "crash" if crash else "clean"],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline().strip() == "locked"
        with pytest.raises(ArchiveLocked):
            with ArchiveLock(tmp_path):
                pass
        child.communicate("\n", timeout=20)
        assert child.returncode == (91 if crash else 0)
        # Windows kernel release may lag process exit briefly; bounded retry,
        # never steal a lock based on PID or delete a stale lock file.
        deadline = time.monotonic() + 2
        while True:
            try:
                with ArchiveLock(tmp_path):
                    break
            except ArchiveLocked:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.05)
        assert not list(tmp_path.glob("*.darc"))
    finally:
        if child.poll() is None:
            child.kill()  # Only this test-created child.
            child.wait(timeout=10)


def test_archive_must_be_absolute_and_external(tmp_path):
    with pytest.raises(ValueError):
        external_destination(Path("relative"))
    repo = Path(__file__).resolve().parents[3]
    with pytest.raises(ValueError, match="outside_git"):
        external_destination(repo / "archive")
    assert external_destination(tmp_path) == tmp_path.resolve()


def test_no_deletion_or_truncation_primitives_in_journal():
    import ast
    import crypto_trader.collector.journal as module
    tree = ast.parse(Path(module.__file__).read_text())
    forbidden = {"unlink", "remove", "rmtree", "truncate", "replace", "rename"}
    assert not any(isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                   and n.func.attr in forbidden for n in ast.walk(tree))

