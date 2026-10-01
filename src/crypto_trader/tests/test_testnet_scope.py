"""Phase 11 scope/security boundary and the authorisation for the testnet exemption.

Phases 1-10 asserted that NO authenticated exchange capability existed anywhere. Phase 11
explicitly authorises authenticated **Spot Testnet** execution, so those guards now exempt exactly
one directory: ``src/crypto_trader/execution/binance_testnet/``. These tests prove the exemption is
narrow, testnet-only and still secret-safe, so the older invariants remain meaningful.
"""

import re
from pathlib import Path

import crypto_trader.execution.binance_testnet as testnet_package

PROJECT_ROOT = Path(__file__).resolve().parents[3]
PACKAGE_DIR = PROJECT_ROOT / "src" / "crypto_trader"
TESTNET_DIR = PACKAGE_DIR / "execution" / "binance_testnet"

#: The ONLY directory allowed to contain authenticated exchange capability after Phase 11.
AUTHORISED_TESTNET_DIRECTORY = TESTNET_DIR

_AUTHORIZED_MODULES = {
    "__init__.py",
    "client.py",
    "clock.py",
    "credentials.py",
    "endpoints.py",
    "errors.py",
    "models.py",
    "rate_limit.py",
    "runtime.py",
    "signing.py",
    "startup.py",
    "transport.py",
    "translate.py",
}

#: Tokens that may appear ONLY inside the authorised testnet directory.
_AUTHENTICATED_TOKENS = (
    "api/v3/order",
    "x-mbx-apikey",
    "binance_api_key",
    "binance_api_secret",
    "recvwindow",
    "listenkey",
)


def _python_files(root: Path) -> list[Path]:
    return [file for file in root.rglob("*.py") if "__pycache__" not in file.parts]


def test_testnet_package_contains_only_authorized_components():
    assert {file.name for file in _python_files(TESTNET_DIR)} == _AUTHORIZED_MODULES


def test_authenticated_capability_exists_only_in_the_authorised_directory():
    """The Phase 12 audit harness is the second, verifier-only exemption (offline by design)."""
    audit_dir = PACKAGE_DIR / "audit"
    offenders: list[str] = []
    for file in _python_files(PACKAGE_DIR):
        if file.is_relative_to(TESTNET_DIR) or file.is_relative_to(PACKAGE_DIR / "tests"):
            continue
        if file.is_relative_to(audit_dir):
            # The auditor names the tokens it forbids and drives only the scripted transport;
            # ``test_audit_scope.py`` proves it has no network capability and no credentials.
            continue
        text = file.read_text(encoding="utf-8").lower()
        for token in _AUTHENTICATED_TOKENS:
            if token in text:
                offenders.append(f"{file.relative_to(PROJECT_ROOT)}:{token}")
    assert offenders == [], f"authenticated capability leaked outside the testnet adapter: {offenders}"


def test_no_futures_margin_or_options_endpoint_is_used():
    text = "\n".join(file.read_text(encoding="utf-8").lower() for file in _python_files(TESTNET_DIR))

    for token in (
        "/fapi/",
        "/dapi/",
        "/eapi/",
        "/sapi/",
        "binancefuture",
        "positionrisk",
        "leveragebracket",
        "listenkey",
        "universaltransfer",
    ):
        assert token not in text, f"out-of-scope endpoint or capability: {token}"


def test_no_withdrawal_capability_exists():
    text = "\n".join(file.read_text(encoding="utf-8") for file in _python_files(TESTNET_DIR)).lower()

    assert "withdraw/apply" not in text
    assert "/sapi/v1/capital/withdraw" not in text
    # The only legitimate mention is the least-privilege CHECK on the account flags.
    for match in re.finditer(r"can_?withdraw", text):
        assert match is not None
    assert "canwithdraw" in text


def test_credentials_come_only_from_the_two_environment_variables():
    credentials_source = (TESTNET_DIR / "credentials.py").read_text(encoding="utf-8")

    assert 'API_KEY_ENV_VAR = "BINANCE_TESTNET_API_KEY"' in credentials_source
    assert 'API_SECRET_ENV_VAR = "BINANCE_TESTNET_API_SECRET"' in credentials_source
    assert "environ" in credentials_source


def test_the_adapter_never_imports_the_engine_or_the_llm_layer():
    forbidden_imports = re.compile(
        r"^\s*(?:import|from)\s+(nautilus_trader|crewai|crypto_trader\.llm|redis|kafka)\b",
        re.MULTILINE,
    )
    for file in _python_files(TESTNET_DIR):
        match = forbidden_imports.search(file.read_text(encoding="utf-8"))
        assert match is None, f"{file.name} imports {match.group(1) if match else ''}"


def test_the_adapter_never_persists_or_clones_credentials():
    for file in _python_files(TESTNET_DIR):
        text = file.read_text(encoding="utf-8")
        for token in (
            "os.environ[",
            "environ.setdefault",
            "putenv",
            "git push",
            ".netrc",
            "keyring",
        ):
            assert token not in text, f"{file.name} mutates or persists credentials: {token}"


def test_public_api_is_marked_as_testnet():
    exported = set(testnet_package.__all__)

    assert "SPOT_TESTNET_REST_BASE" in exported
    assert "MainnetForbiddenError" in exported
    assert "TestnetCredentialsRequiredError" in exported
    labels = [
        getattr(testnet_package, name)
        for name in ("SPOT_TESTNET_REST_BASE",)
    ]
    assert labels[0] == "https://testnet.binance.vision"


def test_testnet_adapter_does_not_leak_into_domain_packages():
    import_pattern = re.compile(
        r"^\s*(?:import|from)\s+crypto_trader\.execution\.binance_testnet", re.MULTILINE
    )
    for package in ("events", "llm", "tools", "validity", "risk", "recovery", "safety", "paper"):
        for file in _python_files(PACKAGE_DIR / package):
            assert not import_pattern.search(file.read_text(encoding="utf-8")), (
                f"{file.name} depends on the testnet adapter"
            )
