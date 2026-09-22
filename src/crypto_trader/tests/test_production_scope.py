"""Phase 14 scope boundary: the production runtime stays testnet-only, secret-free and fenced.

Phase 14 adds the first production *decision boundary* that can reach an authenticated venue. These
guards prove the addition stays narrow and does not weaken any Phase 1-13 invariant.
"""

from __future__ import annotations

import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]
PACKAGE_DIR = PROJECT_ROOT / "src" / "crypto_trader"
PRODUCTION_DIR = PACKAGE_DIR / "production"

_AUTHORIZED_MODULES = {
    "__init__.py",
    "environment.py",
    "errors.py",
    "fence.py",
    "runtime.py",
}

#: Capability tokens that must never appear in the production runtime implementation.
_AUTHENTICATED_OR_PRODUCTION_TOKENS = (
    "api/v3/order",
    "api/v3/account",
    "x-mbx-apikey",
    "/sapi/",
    "fapi.",
    "binance.us",
    "listenkey",
    "recvwindow",
)

_CREDENTIAL_TOKENS = (
    "credentials_from_environment",
    "BINANCE_TESTNET_API_KEY",
    "BINANCE_TESTNET_API_SECRET",
    "api_key",
    "api_secret",
    "keyring",
    ".netrc",
    "dotenv",
)

_ORDER_CAPABILITY_TOKENS = (
    "place_order",
    "create_order",
    "cancel_order",
    "execute_trade",
)

_WITHDRAWAL_TOKENS = ("withdraw/apply", "universaltransfer")

_FORBIDDEN_IMPORTS = re.compile(
    r"^\s*(?:import|from)\s+"
    r"(nautilus_trader|nautilus|crewai|crypto_trader\.llm|crypto_trader\.crew|"
    r"crypto_trader\.paper|redis|kafka|pika|nats|sqlalchemy)\b",
    re.MULTILINE,
)


def _files() -> list[Path]:
    return [file for file in PRODUCTION_DIR.rglob("*.py") if "__pycache__" not in file.parts]


def test_the_production_package_contains_only_authorized_components():
    assert {file.name for file in _files()} == _AUTHORIZED_MODULES
    for forbidden in ("oms", "broker", "simulator", "event_bus", "state_store", "redis"):
        assert not (PRODUCTION_DIR / forbidden).exists()


def test_the_production_runtime_carries_no_mainnet_or_credential_capability():
    for file in _files():
        text = file.read_text(encoding="utf-8").lower()
        for token in (*_AUTHENTICATED_OR_PRODUCTION_TOKENS, *_CREDENTIAL_TOKENS, *_ORDER_CAPABILITY_TOKENS):
            assert token.lower() not in text, f"{file.name} contains {token}"


def test_the_production_runtime_has_no_withdrawal_capability():
    for file in _files():
        text = file.read_text(encoding="utf-8").lower()
        for token in _WITHDRAWAL_TOKENS:
            assert token not in text, f"{file.name} contains {token}"


def test_the_production_runtime_has_no_engine_broker_or_llm_dependency():
    for file in _files():
        match = _FORBIDDEN_IMPORTS.search(file.read_text(encoding="utf-8"))
        assert match is None, f"{file.name} must not import {match.group(1) if match else ''}"
    # The runtime integrates the Phase 11 adapter; the adapter must not depend back on it.
    adapter = (PACKAGE_DIR / "execution" / "binance_testnet").rglob("*.py")
    for file in adapter:
        assert "crypto_trader.production" not in file.read_text(encoding="utf-8")


def test_the_production_runtime_reads_no_credential_configuration_of_its_own():
    joined = "\n".join(file.read_text(encoding="utf-8") for file in _files())

    assert "CRYPTO_TRADER_ENV" in joined, "the non-secret environment selector is expected"
    for token in (
        "BINANCE_TESTNET_API_KEY",
        "BINANCE_TESTNET_API_SECRET",
        "BINANCE_API_KEY",
        "keyring",
        ".netrc",
        "putenv",
    ):
        assert token not in joined, f"the runtime reads credential configuration: {token}"


def test_the_production_environment_defaults_to_testnet_and_production_is_disabled():
    source = (PRODUCTION_DIR / "environment.py").read_text(encoding="utf-8")

    assert 'DEFAULT_ENVIRONMENT = "testnet"' in source
    assert re.search(r"^MAINNET_ENABLED = False$", source, re.MULTILINE)
    assert "owner authorization gate" in source


def test_the_final_fence_is_revalidated_immediately_before_the_venue_call():
    """Structural proof that a submission cannot skip the final safety/fence check."""
    source = (PRODUCTION_DIR / "runtime.py").read_text(encoding="utf-8")
    submit_body = source.split("def submit_prepared", 1)[1].split("\n    def ", 1)[0]
    fence_index = submit_body.index("authorize_new_risk")
    venue_index = submit_body.index("_execution.submit(")

    assert fence_index < venue_index
    assert submit_body.count("_execution.submit(") == 1
    assert source.count("_execution.submit(") == 1, "the venue call must exist only in the fenced path"


def test_phase14_adds_no_event_contract():
    contracts = (PACKAGE_DIR / "events" / "contracts.py").read_text(encoding="utf-8")
    event_types = set(re.findall(r'= "([a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+)"', contracts))

    assert event_types == {
        "strategy.decision.recorded",
        "trade.intent.created",
        "risk.decision.recorded",
        "order.state.changed",
        "execution.fill.recorded",
        "recovery.state.changed",
        "validity.disposition.recorded",
        "risk.evaluation.recorded",
        "safety.state.changed",
        "safety.operator_command.recorded",
    }


def test_domain_packages_do_not_depend_on_the_production_runtime():
    import_pattern = re.compile(
        r"^\s*(?:import|from)\s+crypto_trader\.production", re.MULTILINE
    )
    for package in ("events", "llm", "tools", "validity", "risk", "recovery", "safety", "paper"):
        for file in (PACKAGE_DIR / package).rglob("*.py"):
            assert not import_pattern.search(file.read_text(encoding="utf-8")), (
                f"{file.name} depends on the production runtime"
            )


def test_no_push_was_performed_by_this_phase():
    git_dir = PROJECT_ROOT / ".git"
    config = (git_dir / "config").read_text(encoding="utf-8")

    for name, body in re.findall(
        r'\[branch "([^"]+)"\]\s*(.*?)(?=\n\[|\Z)', config, re.DOTALL
    ):
        if name.startswith("phase-14"):
            assert "remote =" not in body, f"the {name} branch has a push upstream"
    remote_refs = list((git_dir / "refs" / "remotes").rglob("*"))
    assert not any("phase-14" in ref.name for ref in remote_refs if ref.is_file())
