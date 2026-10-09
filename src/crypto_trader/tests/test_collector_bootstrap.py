"""Offline bootstrap tests; real fresh-venv repetitions are separately sealed."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from crypto_trader.collector import bootstrap as b


class UnitGuard(b.StartupGuard):
    def activate(self):
        pass  # Irremovable real hooks are exercised only in fresh children.


@pytest.fixture(autouse=True)
def no_preloaded_modules_for_fake_importer(monkeypatch):
    monkeypatch.setattr(b, "_preloaded_dependencies", lambda: [])
    # Tests inject a fake exit registrar; close their trusted dup deterministically.
    real_dup = b.os.dup
    captured = []
    def dup(fd):
        result = real_dup(fd)
        captured.append(result)
        return result
    monkeypatch.setattr(b.os, "dup", dup)
    yield
    for fd in captured:
        os.close(fd)



def test_exact_flags_before_import_and_inherited_provenance(monkeypatch):
    for key in b.BOOTSTRAP_VALUES:
        monkeypatch.setenv(key, "inherited-test")
    monkeypatch.setenv("UNRELATED_SECRET", "must-not-be-recorded")
    monkeypatch.setenv("PYTHONPATH", "must-not-be-recorded-path")
    guard = UnitGuard()
    seen = []
    def importer(name):
        assert name == "crypto_trader.tools.binance_futures_public_tool"
        assert all(os.environ[k] == v for k, v in b.BOOTSTRAP_VALUES.items())
        seen.append("import")
        return object()
    def sleep(seconds):
        assert guard.active and guard.phase == "post_import_grace"
        assert seconds == b.POST_IMPORT_GRACE_SECONDS
        seen.append("grace")
    module, report = b.guarded_import(importer=importer, sleep=sleep, guard=guard, register_exit=lambda fn: None)
    assert module and report["startup_validated"]
    assert seen == ["import", "grace"]
    assert report["inherited_values"] == dict.fromkeys(b.BOOTSTRAP_VALUES, "inherited-test")
    assert report["set_values"] == b.BOOTSTRAP_VALUES
    assert "must-not-be-recorded" not in json.dumps(report)
    assert "must-not-be-recorded-path" not in json.dumps(report)
    assert set(report["package_versions"]) == {"crewai", "litellm"}
    assert not guard.active


@pytest.mark.parametrize("host", ["example.invalid", "localhost", "127.0.0.2",
                                  "192.168.1.1", "10.0.0.1", "raw.githubusercontent.com",
                                  "api.scarf.sh", "f" + "api.binance.com"])
@pytest.mark.parametrize("event", ["socket.connect", "socket.getaddrinfo"])
def test_external_startup_events_rejected(host, event):
    guard = UnitGuard()
    args = (None, (host, 443)) if event == "socket.connect" else (host, 443, 0, 0, 0)
    with pytest.raises(b.StartupOutboundDenied):
        guard.audit(event, args)
    assert len(guard.attempts) == 1
    assert host in guard.attempts[0]["destination"]
    assert guard.attempts[0]["wall_ms"] > 0
    assert guard.attempts[0]["monotonic_ns"] > 0
    assert not guard.finish(deactivate=True)


@pytest.mark.parametrize("host", ["127.0.0.1", "::1"])
@pytest.mark.parametrize("event", ["socket.connect", "socket.getaddrinfo"])
def test_only_literal_loopback_exception(host, event):
    guard = UnitGuard()
    args = (None, (host, 1)) if event == "socket.connect" else (host, 1, 0, 0, 0)
    guard.audit(event, args)
    assert guard.attempts == []
    assert guard.finish(deactivate=True)


@pytest.mark.parametrize("phase", ["import", "post_import_grace"])
def test_swallowed_attempt_latches_failed_startup(monkeypatch, phase):
    for key in b.BOOTSTRAP_VALUES:
        monkeypatch.setenv(key, "before")
    guard = UnitGuard()
    def attempt():
        try:
            guard.audit("socket.connect", (None, ("example.invalid", 443)))
        except b.StartupOutboundDenied:
            pass  # Models dependency catching networking errors.
    def importer(name):
        if phase == "import":
            attempt()
        return object()
    def sleep(seconds):
        if phase == "post_import_grace":
            attempt()
    try:
        _, report = b.guarded_import(importer=importer, sleep=sleep, guard=guard, register_exit=lambda fn: None)
        assert not report["startup_validated"]
        assert report["external_attempts"][0]["phase"] == phase
        assert guard.active
    finally:
        guard.active = False  # This unit-test hook cannot leak into later tests.


def test_import_failure_never_reports_validated(monkeypatch):
    for key in b.BOOTSTRAP_VALUES:
        monkeypatch.setenv(key, "before")
    guard = UnitGuard()
    def importer(name):
        raise ImportError("fixture")
    try:
        _, report = b.guarded_import(importer=importer, sleep=lambda _: None, guard=guard, register_exit=lambda fn: None)
        assert not report["startup_validated"]
        assert report["import_error_type"] == "ImportError"
    finally:
        guard.active = False


@pytest.mark.parametrize("mechanism", ["connect", "connect_ex", "create_connection"])
def test_real_socket_attempt_intercepted_before_network(mechanism):
    script = """
import json,socket
from crypto_trader.collector.bootstrap import StartupGuard,StartupOutboundDenied
g=StartupGuard();g.activate()
try:
    if METHOD == 'create_connection':
        socket.create_connection(('name.invalid',443),timeout=0.1)
    else:
        with socket.socket() as s:
            s.settimeout(0.1)
            getattr(s,METHOD)(('192.0.2.1',443))
except StartupOutboundDenied:
    print(json.dumps(g.attempts))
else:
    raise AssertionError('guard did not intercept')
""".replace("METHOD", repr(mechanism))
    child = subprocess.run([sys.executable, "-B", "-c", script],
                           capture_output=True, text=True, timeout=20)
    assert child.returncode == 0, child.stderr
    assert len(json.loads(child.stdout)) == 1


def test_entrypoint_is_stdlib_only_before_bootstrap():
    import ast
    source = Path(b.__file__).with_name("__main__.py").read_text()
    tree = ast.parse(source)
    first_imports = [n for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]
    names = [(n.module if isinstance(n, ast.ImportFrom) else n.names[0].name)
             for n in first_imports]
    assert names == ["argparse", "json", "sys", "bootstrap"]
    assert "guarded_import(" in source

def test_importing_entrypoint_does_not_preload_dependencies():
    script = "import crypto_trader.collector.__main__,sys; assert not any(n in sys.modules for n in ('crewai','litellm','opentelemetry','crypto_trader.tools'))"
    child = subprocess.run([sys.executable, "-B", "-c", script], capture_output=True,
                           text=True, timeout=20)
    assert child.returncode == 0, child.stderr


def test_preloaded_dependency_fails_closed(monkeypatch):
    monkeypatch.setattr(b, "_preloaded_dependencies", lambda: ["crewai"])
    for key in b.BOOTSTRAP_VALUES:
        monkeypatch.setenv(key, "before")
    guard = UnitGuard()
    try:
        module, report = b.guarded_import(importer=lambda _: pytest.fail("must not import"),
                                          sleep=lambda _: None, guard=guard, register_exit=lambda fn: None)
        assert module is None and not report["startup_validated"]
        assert report["preloaded_dependencies"] == ["crewai"]
        assert guard.active
    finally:
        guard.active = False


@pytest.mark.parametrize("close_stderr", [False, True])
def test_exit_time_swallowed_attempt_exits_nonzero(close_stderr):
    script = """
import atexit,socket,os
from crypto_trader.collector.bootstrap import guarded_import
def importer(name):
    def late():
        try:
            socket.create_connection(('name.invalid',443),timeout=0.1)
        except OSError:
            pass
    atexit.register(late)
    return object()
guarded_import(importer=importer,sleep=lambda _:None,deactivate=False)
"""
    if close_stderr:
        script += "\nos.close(2)\n"
    child = subprocess.run([sys.executable, "-B", "-c", script],
                           capture_output=True, text=True, timeout=20)
    assert child.returncode == 78
    if not close_stderr:
        assert json.loads(child.stderr)["exit_latch"]


@pytest.mark.parametrize("close_before,close_stdin", [(False, False), (True, False), (True, True)])
def test_exit_latch_never_writes_reused_descriptor(tmp_path, close_before, close_stdin):
    script = """
import atexit,os,socket,sys
from crypto_trader.collector.bootstrap import guarded_import,StartupGuard
if sys.argv[2]=='before':
    os.close(2)
if sys.argv[3]=='closed':
    os.close(0)
g=StartupGuard()
def importer(name):
    def late():
        try:
            socket.create_connection(('name.invalid',443),timeout=0.1)
        except OSError:
            pass
    atexit.register(late)
    return object()
guarded_import(importer=importer,sleep=lambda _:None,deactivate=False,guard=g)
assert g._stderr_fd is None or g._stderr_fd>2
if sys.argv[2]!='before':
    os.close(2)
fd=os.open(sys.argv[1],os.O_WRONLY|os.O_CREAT|os.O_EXCL)
os.write(fd,b'unchanged')
os.fsync(fd)
"""
    path = tmp_path / "unrelated-file"
    child = subprocess.run([sys.executable, "-B", "-c", script, str(path),
                            "before" if close_before else "after",
                            "closed" if close_stdin else "open"],
                           capture_output=True, text=True, timeout=20)
    assert child.returncode == 78
    assert path.read_bytes() == b"unchanged"
    if not close_before:
        report = json.loads(child.stderr)
        assert report["exit_latch"]
        assert "name.invalid" in report["first_attempt"]["destination"]
        assert len(child.stderr.encode("utf-8")) < 512
