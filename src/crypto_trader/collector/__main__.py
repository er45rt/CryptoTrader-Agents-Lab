"""Dedicated collector entrypoint. No dependency imports before guarded bootstrap."""
import argparse
import json
import sys

from .bootstrap import guarded_import

STARTUP_FAILURE_EXIT = 78


def assert_process_isolation():
    forbidden = ("crypto_trader.agents", "crypto_trader.crew", "crypto_trader.main",
                 "crypto_trader.risk", "crypto_trader.execution", "crypto_trader.production",
                 "crypto_trader.recovery", "crypto_trader.paper")
    if any(name == prefix or name.startswith(prefix + ".")
           for name in sys.modules for prefix in forbidden):
        raise RuntimeError("collector_requires_own_process")


def main():
    parser = argparse.ArgumentParser(description="Phase 1 raw snapshot collector")
    parser.add_argument("--check-startup", action="store_true",
                        help="validate guarded imports only; never poll or open archive")
    parser.add_argument("--archive", help="absolute external archive directory")
    parser.add_argument("--pilot-seconds", type=int, choices=(3600,),
                        help="one-hour supervised run; monotonic stop and durable clean_stop")
    parser.add_argument("--host-time-sync-acknowledged", action="store_true",
                        help="operator acknowledges required host time synchronization")
    args = parser.parse_args()
    try:
        assert_process_isolation()
    except RuntimeError as error:
        print(str(error), file=sys.stderr)
        return STARTUP_FAILURE_EXIT

    def load_dependencies(name):
        import importlib
        imported = importlib.import_module(name)
        # All service/adapter dependencies load while the guard is active,
        # before the post-import grace. Check-only exercises this same path.
        importlib.import_module("crypto_trader.collector.service")
        return imported

    _, report = guarded_import(importer=load_dependencies, deactivate=not args.check_startup)
    if not report["startup_validated"]:
        print(json.dumps(report, sort_keys=True), file=sys.stderr)
        return STARTUP_FAILURE_EXIT
    if args.check_startup:
        print(json.dumps(report, sort_keys=True))
        sys.stdout.flush()
        return 0
    # No service import occurs at module scope or before guarded_import.
    from pathlib import Path
    import signal
    from .journal import ArchiveLock, ArchiveLocked, Journal
    from .policy import Config, EXIT_CONFIGURATION, EXIT_LOCKED
    from .service import Collector
    if not args.archive:
        print("absolute_external_archive_argument_required", file=sys.stderr)
        return EXIT_CONFIGURATION
    try:
        config = Config(Path(args.archive),
                        host_time_sync_acknowledged=args.host_time_sync_acknowledged)
        config.validate()
        with ArchiveLock(config.archive):
            from uuid import uuid4
            journal = Journal(config.archive, uuid4().hex)
            handlers = {}
            try:
                collector = Collector(config, journal, bootstrap=report)
                for sig in (signal.SIGINT, signal.SIGTERM):
                    handlers[sig] = signal.getsignal(sig)
                    signal.signal(sig, lambda signum, frame: collector.request_stop(
                        "signal_" + str(signum)))
                return collector.run(max_duration_ns=(args.pilot_seconds * 1_000_000_000
                                                       if args.pilot_seconds else None))
            finally:
                journal.close()
                for sig, handler in handlers.items():
                    signal.signal(sig, handler)
    except ArchiveLocked:
        print("archive_already_locked: no collection started", file=sys.stderr)
        return EXIT_LOCKED
    except (OSError, ValueError) as error:
        print("collector_configuration_or_archive_unusable:" + type(error).__name__,
              file=sys.stderr)
        return EXIT_CONFIGURATION


if __name__ == "__main__":
    raise SystemExit(main())
