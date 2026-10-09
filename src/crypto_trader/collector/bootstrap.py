"""Own-process bootstrap; no dependency patching or network client.

CPython socket.connect (also connect_ex) and socket.getaddrinfo events only.
Windows Proactor literal-IP ConnectEx, native resolvers, other DNS/UDP/native
paths may bypass the hook. Flags prevent egress; this guard detects regressions.
Hooks cannot be removed; after validated service handoff this hook is inert.
"""
import atexit
import importlib
import importlib.metadata
import json
import os
import sys
import threading
import time
import traceback

BOOTSTRAP_POLICY_VERSION = "collector-bootstrap-1"
# Observe tasks deferred by import threads/timers for five seconds. Scarf's
# urllib timeout does NOT bound scheduling; audit raises before the socket call.
# This finite window cannot rule out arbitrary later activity.
POST_IMPORT_GRACE_SECONDS = 5.0
MAX_RECORDED_ATTEMPTS = 32  # Bounded diagnostics, not permission for retries.
EXPECTED_DEPENDENCY_VERSIONS = {"crewai": "0.201.0", "litellm": "1.74.9"}
BOOTSTRAP_VALUES = {
    "CREWAI_DISABLE_TELEMETRY": "true",
    "OTEL_SDK_DISABLED": "true",
    "CREWAI_DISABLE_TRACKING": "true",
    "LITELLM_LOCAL_MODEL_COST_MAP": "True",
}
LOOPBACK_LITERALS = frozenset({"127.0.0.1", "::1"})


class StartupOutboundDenied(OSError):
    pass


def reserve_standard_fds():
    """Own-process FD hygiene: archive handles must never become stdio slots.

    Repeated before archive opens as well as bootstrap. This is not atomic
    against a hostile thread closing descriptors concurrently.
    """
    reserved = []
    for target in (0, 1, 2):
        try:
            os.fstat(target)
        except OSError:
            opened = os.open(os.devnull, os.O_RDWR | getattr(os, "O_BINARY", 0))
            if opened != target:
                try:
                    os.dup2(opened, target)
                finally:
                    os.close(opened)
            reserved.append(target)
    return reserved


class StartupGuard:
    def __init__(self):
        self.active = True
        self.phase = "bootstrap"
        self.attempts = []
        self.attempt_count = 0
        self._lock = threading.Lock()

    def audit(self, event, args):
        if event not in ("socket.connect", "socket.getaddrinfo"):
            return
        with self._lock:
            if not self.active:
                return
            address = args[1] if event == "socket.connect" else args[:2]
            host = address[0] if isinstance(address, tuple) and address else None
            if isinstance(host, str) and host in LOOPBACK_LITERALS:
                return
            self.attempt_count += 1
            record = {
                "mechanism": event, "destination": repr(address), "phase": self.phase,
                "wall_ms": time.time_ns() // 1_000_000,
                "monotonic_ns": time.monotonic_ns(),
                "monotonic_source": "time.monotonic_ns", "monotonic_unit": "ns",
                "stack": [{"file": f.filename, "line": f.lineno, "function": f.name}
                          for f in traceback.StackSummary.extract(
                              traceback.walk_stack(None), limit=24, lookup_lines=False)],
            }
            if len(self.attempts) < MAX_RECORDED_ATTEMPTS:
                self.attempts.append(record)
        raise StartupOutboundDenied("external_startup_attempt_forbidden")

    def activate(self):
        sys.addaudithook(self.audit)

    def finish(self, *, deactivate):
        with self._lock:
            if self.attempt_count:
                return False
            if deactivate:
                self.active = False
                self.phase = "complete"
            else:
                self.phase = "held_until_exit"
            return True


def _preloaded_dependencies():
    return sorted(name for name in sys.modules
                  if name.split(".")[0] in ("crewai", "litellm", "opentelemetry")
                  or name == "crypto_trader.tools")


def guarded_import(*, importer=None, sleep=None, guard=None, deactivate=True, register_exit=None):
    guard = StartupGuard() if guard is None else guard
    guard.activate()
    preloaded = _preloaded_dependencies()
    try:
        os.fstat(2)
        stderr_was_open = True
    except OSError:
        stderr_was_open = False
    reserved_stdio = reserve_standard_fds()
    # Fill stdio first, or dup could occupy closed fd0/1 and remain vulnerable.
    try:
        stderr_fd = os.dup(2) if stderr_was_open else None
    except OSError:
        stderr_fd = None
    guard._stderr_fd = stderr_fd
    def exit_latch():
        if guard.active and guard.attempt_count:
            try:
                first = guard.attempts[0] if guard.attempts else {}
                evidence = {"startup_validated": False, "exit_latch": True,
                            "external_attempt_count": guard.attempt_count,
                            "first_attempt": {key: first.get(key) for key in
                                ("destination", "phase", "wall_ms", "monotonic_ns", "mechanism")}}
                if stderr_fd is not None:
                    os.write(stderr_fd, (json.dumps(evidence) + "\n").encode("utf-8"))
            finally:
                os._exit(78)
        if stderr_fd is not None:
            os.close(stderr_fd)
    (register_exit or atexit.register)(exit_latch)
    inherited = {key: os.environ.get(key) for key in BOOTSTRAP_VALUES}
    for key, value in BOOTSTRAP_VALUES.items():
        os.environ[key] = value
    versions = {}
    for package in ("crewai", "litellm", "aiodns", "pycares"):
        try:
            versions[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            versions[package] = None
    report = {
        "bootstrap_policy_version": BOOTSTRAP_POLICY_VERSION,
        "inherited_values": inherited, "set_values": dict(BOOTSTRAP_VALUES),
        "package_versions": {name: versions[name] for name in EXPECTED_DEPENDENCY_VERSIONS},
        "expected_package_versions": EXPECTED_DEPENDENCY_VERSIONS,
        "optional_native_resolvers": {name: versions[name] for name in ("aiodns", "pycares")},
        "preloaded_dependencies": preloaded,
        "reserved_standard_fds": reserved_stdio,
        "sys_prefix": sys.prefix, "sys_base_prefix": sys.base_prefix,
        "interpreter": sys.executable, "python_version": sys.version,
        "grace_seconds": POST_IMPORT_GRACE_SECONDS,
        "guard_surface": ["socket.connect", "socket.getaddrinfo"],
        "loopback_literals": sorted(LOOPBACK_LITERALS),
        "wall_ms": time.time_ns() // 1_000_000,
        "monotonic_ns": time.monotonic_ns(),
        "monotonic_source": "time.monotonic_ns", "monotonic_unit": "ns",
    }
    module = None
    import_error = None
    guard.phase = "import"
    try:
        if preloaded:
            raise RuntimeError("dependencies_preloaded_before_guard")
        if report["package_versions"] != EXPECTED_DEPENDENCY_VERSIONS:
            raise RuntimeError("dependency_versions_require_bootstrap_revalidation")
        module = (importer or importlib.import_module)(
            "crypto_trader.tools.binance_futures_public_tool")
    except Exception as error:
        import_error = type(error).__name__
    guard.phase = "post_import_grace"
    (sleep or time.sleep)(POST_IMPORT_GRACE_SECONDS)
    success = guard.finish(deactivate=deactivate and import_error is None)
    report.update({
        "external_attempts": list(guard.attempts),
        "external_attempt_count": guard.attempt_count,
        "dependency_module_files": {name: getattr(sys.modules.get(name), "__file__", None)
                                    for name in (*EXPECTED_DEPENDENCY_VERSIONS,
                                                 "crypto_trader.tools.binance_futures_public_tool")},
        "import_error_type": import_error,
        "startup_validated": success and import_error is None,
        "completed_wall_ms": time.time_ns() // 1_000_000,
        "completed_monotonic_ns": time.monotonic_ns(),
    })
    return module, report
