"""Pytest command-line safety gate for explicitly requested live Testnet tests.

Registration alone does NOT permit a live venue request. The live-only module
also requires an exact CLI argument plus an independent opt-in and credentials.
"""


def pytest_addoption(parser):
    parser.addoption(
        "--allow-live-testnet",
        action="store_true",
        default=False,
        help="Explicit direct CLI opt-in for LIVE Spot Testnet tests; environment opt-in and testnet credentials are also required.",
    )
