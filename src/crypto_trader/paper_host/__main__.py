"""Paper session CLI: start | resume | stop | status. Simulated execution only.

    python -m crypto_trader.paper_host start  --dir D --config config.json --provider crew
    python -m crypto_trader.paper_host resume --dir D --provider crew
    python -m crypto_trader.paper_host stop   --dir D
    python -m crypto_trader.paper_host status --dir D
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from .host import STOP_REQUEST, HostConfig, PaperHost, create_session, load_manifest, read_status


def _crew_provider(directory: Path) -> Any:
    from crypto_trader.crew_paper_bridge import crew_plan_provider
    os.environ.setdefault("CREWAI_TRACING_ENABLED", "false")
    os.environ.setdefault("CREWAI_DISABLE_TELEMETRY", "true")
    os.environ.setdefault("OTEL_SDK_DISABLED", "true")
    workdir = directory / "crew_cwd"
    workdir.mkdir(exist_ok=True)
    os.chdir(workdir)
    return crew_plan_provider


def _stub_provider(path: Path) -> Any:
    """Replays a local file of plans. It proves wiring only and is labeled stub everywhere."""
    plans = json.loads(path.read_text(encoding="utf-8"))

    def provider(inputs: dict[str, Any]) -> str:
        index = int(inputs["market_reference"].rsplit("#", 1)[1])
        return plans[index % len(plans)]
    return provider


def _run(args: argparse.Namespace) -> int:
    from .public_market import BinancePublicReader

    directory = Path(args.dir).resolve()
    if args.command == "start":
        values = json.loads(Path(args.config).read_text(encoding="utf-8"))
        values["provider_kind"] = args.provider
        create_session(directory, HostConfig.create(**values))
    config = load_manifest(directory)
    if config.provider_kind != args.provider:
        raise SystemExit("provider differs from the sealed session manifest.")
    provider = _crew_provider(directory) if args.provider == "crew" else _stub_provider(Path(args.plans))
    reader = BinancePublicReader(config.symbol)
    host = PaperHost(directory, source=lambda after: reader.poll(after_trade_id=after),
                     plan_provider=provider, provenance={"source": "binance_public_rest"})
    print(host.run(poll_seconds=args.poll_seconds, max_cycles=args.max_cycles))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="crypto_trader.paper_host")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("start", "resume"):
        command = commands.add_parser(name)
        command.add_argument("--dir", required=True)
        command.add_argument("--provider", choices=("crew", "stub"), required=True)
        command.add_argument("--plans", help="stub only: JSON list of raw plan texts")
        command.add_argument("--poll-seconds", type=float, default=5.0)
        command.add_argument("--max-cycles", type=int)
        if name == "start":
            command.add_argument("--config", required=True)
    for name in ("stop", "status"):
        commands.add_parser(name).add_argument("--dir", required=True)
    args = parser.parse_args(argv)
    if args.command == "stop":
        (Path(args.dir) / STOP_REQUEST).touch()
        print("stop requested")
        return 0
    if args.command == "status":
        print(json.dumps(read_status(Path(args.dir)), indent=1, default=str))
        return 0
    return _run(args)


if __name__ == "__main__":
    sys.exit(main())
