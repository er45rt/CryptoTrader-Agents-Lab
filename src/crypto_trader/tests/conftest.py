"""Pytest command-line safety gate for explicitly requested live Testnet tests.

Registration alone does NOT permit a live venue request. The live-only module
also requires an exact CLI argument plus an independent opt-in and credentials.
"""
import os

# Offline test process (owner decision D36): CrewAI starts a background OTLP telemetry exporter when its first
# module is imported unless this flag is already set, and that exporter dials telemetry.crewai.com. The flag is set
# here, before any test module imports crewai; tests' network guards stay unchanged and still fail on any attempt.
os.environ["CREWAI_DISABLE_TELEMETRY"] = "true"


def pytest_addoption(parser):
    parser.addoption(
        "--allow-live-testnet",
        action="store_true",
        default=False,
        help="Explicit direct CLI opt-in for LIVE Spot Testnet tests; environment opt-in and testnet credentials are also required.",
    )
