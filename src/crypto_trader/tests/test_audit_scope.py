"""Phase 12 scope boundary: the audit harness itself must stay offline, secret-free and read-only."""

import re
from pathlib import Path

import crypto_trader.audit as audit_package

PROJECT_ROOT = Path(__file__).resolve().parents[3]
PACKAGE_DIR = PROJECT_ROOT / "src" / "crypto_trader"
AUDIT_DIR = PACKAGE_DIR / "audit"

_AUTHORIZED_MODULES = {"__init__.py", "checks.py", "errors.py", "matrix.py", "scenarios.py"}


def _files() -> list[Path]:
    return [file for file in AUDIT_DIR.rglob("*.py") if "__pycache__" not in file.parts]


def test_audit_package_contains_only_authorized_components():
    assert {file.name for file in _files()} == _AUTHORIZED_MODULES


def test_audit_performs_no_network_io():
    network_import = re.compile(
        r"^\s*(?:import|from)\s+(requests|httpx|socket|urllib|aiohttp|websockets)\b",
        re.MULTILINE,
    )
    network_call = re.compile(r"(?:requests|httpx|socket)\.\w+\(", re.MULTILINE)
    url_literal = re.compile(r"https?://[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
    for file in _files():
        text = file.read_text(encoding="utf-8")
        assert not network_import.search(text), f"{file.name} imports network code"
        assert not network_call.search(text), f"{file.name} performs a network call"
        assert not url_literal.search(text), f"{file.name} contains a URL literal"


def test_audit_reads_no_credentials_and_never_persists_one():
    for file in _files():
        if file.name == "checks.py":
            # The checker NAMES the persistence tokens it forbids; it must not call any of them.
            text = file.read_text(encoding="utf-8")
            for call in ("write_text(", "json.dump(", "pickle.load", "open("):
                assert call not in text, f"checks.py performs persistence: {call}"
            continue
        text = file.read_text(encoding="utf-8")
        for token in ("getenv", "os.environ", "keyring", ".netrc", "pickle", "shelve"):
            assert token not in text, f"{file.name} touches credentials or unsafe persistence: {token}"
    # The audit must never load real credentials: it uses the synthetic fixture key pair only.
    joined = "\n".join(file.read_text(encoding="utf-8") for file in _files())
    assert "credentials_from_environment" not in joined
    assert "txf.credentials()" in joined
    # The HTTP library is never used (the `transport.requests` ATTRIBUTE of the scripted double is
    # a different thing and is allowed).
    for call in ("import requests", "requests.get(", "requests.post(", "requests.request("):
        assert call not in joined, f"the audit uses the HTTP library: {call}"


def test_audit_never_places_a_mainnet_order_or_withdraws():
    """`checks.py` names the tokens it forbids; every other module must not use them at all."""
    for file in _files():
        if file.name == "checks.py":
            continue
        lowered = file.read_text(encoding="utf-8").lower()
        # Endpoint tokens, not the English word "withdrawal" in an audit note.
        for token in ("api/v3", "x-mbx", "withdraw/apply", "/sapi/", "binance.com"):
            assert token not in lowered, f"{file.name} references {token}"
        for call in ("import requests", "requests.get(", "requests.post("):
            assert call not in lowered, f"{file.name} has network capability: {call}"


def test_audit_only_ever_uses_the_scripted_testnet_transport():
    """Order/cancel calls inside the audit are provably offline: scripted transport only."""
    scenarios = (AUDIT_DIR / "scenarios.py").read_text(encoding="utf-8")

    assert "ScriptedTransport" in scenarios
    assert "RequestsTransport" not in scenarios


def test_audit_uses_the_adapter_instead_of_raw_engine_wiring():
    """The engine may be referenced only through the Phase 4 adapter, never wired by hand."""
    joined = "\n".join(file.read_text(encoding="utf-8") for file in _files())

    for raw_engine_api in ("BacktestEngine(", "add_venue(", "add_instrument(", "BacktestVenueConfig"):
        assert raw_engine_api not in joined, f"audit wires the engine directly: {raw_engine_api}"
    assert "NautilusSimulationRunner" in joined


def test_audit_reuses_the_real_components_instead_of_reimplementing_them():
    """No duplicate business logic: the audit imports the phases it audits."""
    source = "\n".join(file.read_text(encoding="utf-8") for file in _files())

    for module in (
        "crypto_trader.events",
        "crypto_trader.persistence",
        "crypto_trader.validity",
        "crypto_trader.risk",
        "crypto_trader.recovery",
        "crypto_trader.safety",
        "crypto_trader.paper",
    ):
        assert module in source, f"the audit must exercise {module}"


def test_domain_packages_do_not_depend_on_the_audit():
    import_pattern = re.compile(r"^\s*(?:import|from)\s+crypto_trader\.audit", re.MULTILINE)
    for package in ("events", "llm", "tools", "validity", "risk", "recovery", "safety", "paper"):
        for file in (PACKAGE_DIR / package).rglob("*.py"):
            assert not import_pattern.search(file.read_text(encoding="utf-8")), (
                f"{file.name} depends on the audit harness"
            )


def test_public_api_exposes_the_verdict_vocabulary():
    exported = set(audit_package.__all__)

    assert {"run_audit", "AuditReport", "VERDICT_READY", "VERDICT_NOT_READY"} <= exported
    assert audit_package.VERDICT_READY == "TECHNICALLY READY FOR OWNER REVIEW"
    assert audit_package.VERDICT_NOT_READY == "NOT READY"
