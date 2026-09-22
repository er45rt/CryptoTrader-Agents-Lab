"""Phase 3 scope and regression boundaries.

Proves that the event-contract layer stayed contracts-only and that Phase 1 (Binance
public/read-only) and Phase 2 (four-agent DeepSeek layer) are untouched.
"""

from pathlib import Path

import yaml
from crewai.llms.base_llm import BaseLLM

import crypto_trader.crew as crew_module
import crypto_trader.events as events_package
from crypto_trader.crew import CryptoTraderCrew
from crypto_trader.llm import ACTIVE_PROFILE_NAMES
from crypto_trader.tests.agent_expectations import (
    AGENT_PROFILE_MAP,
    EXPECTED_PROFILE_NAMES,
    LEGACY_AGENT_NAMES,
)
from crypto_trader.tools.binance_public_tool import BinancePublicTool

PROJECT_ROOT = Path(__file__).resolve().parents[3]
PACKAGE_DIR = PROJECT_ROOT / "src" / "crypto_trader"
EVENTS_DIR = PACKAGE_DIR / "events"
CONFIG_DIR = PACKAGE_DIR / "config"

FORBIDDEN_RUNTIME_PACKAGES = (
    "tinyfish",
    "oms",
    "simulator",
    "event_bus",
    "redis",
    "broker",
    "state_store",
)


def test_risk_package_is_only_the_deterministic_engine():
    """Phase 7 authorised a deterministic, non-LLM Hard Risk engine - nothing else."""
    risk_dir = PACKAGE_DIR / "risk"
    assert risk_dir.is_dir(), "the Phase 7 risk package is expected to exist"

    for name in ("oms", "simulator", "event_bus", "broker", "state_store", "redis"):
        assert not (risk_dir / name).exists(), f"unexpected component inside risk/: {name}"

    for file in risk_dir.rglob("*.py"):
        text = file.read_text(encoding="utf-8")
        for token in ("import nautilus", "from nautilus", "crewai", "crypto_trader.llm", "import redis", "requests.post"):
            assert token not in text, f"risk/{file.name} must not reference {token}"

FORBIDDEN_TRADING_TOKENS = (
    "create_order",
    "cancel_order",
    "place_order",
    "execute_trade",
    "api/v3/order",
)

#: Phase 11 authorises authenticated Binance SPOT TESTNET capability, and only there. Every other
#: production module (including the rest of the execution layer) stays free of these tokens.
#: ``test_testnet_scope.py`` proves the exemption is narrow and testnet-only.
PHASE11_AUTHORISED_TESTNET_DIR = PACKAGE_DIR / "execution" / "binance_testnet"

#: Phase 12 adds an OFFLINE audit harness that must NAME the tokens it forbids and drive the
#: testnet adapter through a scripted transport. ``test_audit_scope.py`` proves the harness has no
#: network capability, no credentials and never constructs the live transport.
PHASE12_AUDIT_DIR = PACKAGE_DIR / "audit"


class FakeLLM(BaseLLM):
    """Stand-in LLM so the boundary test never contacts a provider."""

    def __init__(self):
        super().__init__(model="fake/deepseek")

    def call(self, messages, tools=None, callbacks=None, available_functions=None,
             from_task=None, from_agent=None):
        return "fake"


def _runtime_python_files() -> list[Path]:
    return [
        path
        for path in PACKAGE_DIR.rglob("*.py")
        if "tests" not in path.parts and "__pycache__" not in path.parts
    ]


def _runtime_python_files_outside_authorised_testnet() -> list[Path]:
    """Every production module except the Phase 11 testnet adapter and the Phase 12 audit harness."""
    return [
        path
        for path in _runtime_python_files()
        if not path.is_relative_to(PHASE11_AUTHORISED_TESTNET_DIR)
        and not path.is_relative_to(PHASE12_AUDIT_DIR)
    ]


# --- Phase 3 added contracts only ---

def test_events_package_exists_with_expected_modules():
    for module in ("__init__.py", "base.py", "contracts.py", "errors.py", "registry.py"):
        assert (EVENTS_DIR / module).is_file(), f"missing events/{module}"


def test_no_broker_or_runtime_component_was_added():
    for name in FORBIDDEN_RUNTIME_PACKAGES:
        assert not (PACKAGE_DIR / name).exists(), f"unexpected module/package: {name}"


def test_execution_package_is_only_the_boundary_and_adapter():
    """Phase 4 authorised an execution *boundary*, not an OMS/risk/simulator/broker stack."""
    execution_dir = PACKAGE_DIR / "execution"
    assert execution_dir.is_dir(), "the Phase 4 execution boundary package is expected to exist"

    # No runtime components may hide inside the authorised package.
    for name in ("oms", "risk", "simulator", "event_bus", "broker", "state_store", "redis"):
        assert not (execution_dir / name).exists(), f"unexpected component inside execution/: {name}"

    # The boundary itself stays engine-independent and free of broker clients / persistence.
    for file in (execution_dir / "ports.py", execution_dir / "errors.py", execution_dir / "fake.py"):
        text = file.read_text(encoding="utf-8")
        assert "import nautilus" not in text and "from nautilus" not in text
        for token in ("import redis", "import kafka", "sqlite", "requests.post"):
            assert token not in text, f"{file.name} must not contain {token}"


def test_no_broker_client_imports_in_runtime_code():
    forbidden = (
        "import redis",
        "from redis",
        "import kafka",
        "from kafka",
        "import pika",
        "import aioredis",
        "from aiokafka",
        "import nats",
    )

    for path in _runtime_python_files():
        text = path.read_text(encoding="utf-8").lower()
        for token in forbidden:
            assert token not in text, f"{path.name} imports a broker client: {token}"


def test_event_layer_performs_no_io_or_network_calls():
    forbidden = ("requests.", "httpx", "socket.", "subprocess", "urllib", "open(")

    for path in EVENTS_DIR.rglob("*.py"):
        text = path.read_text(encoding="utf-8").lower()
        for token in forbidden:
            assert token not in text, f"events/{path.name} performs I/O: {token}"


def test_event_layer_is_not_an_event_bus():
    source = "\n".join(path.read_text(encoding="utf-8") for path in EVENTS_DIR.rglob("*.py"))

    for forbidden in (
        "class EventBus",
        "def publish(",
        "def subscribe(",
        "def dispatch(",
        "def consume(",
        "asyncio.Queue",
        "threading.Thread",
    ):
        assert forbidden not in source, f"event layer implements runtime behaviour: {forbidden}"


def test_event_layer_does_not_persist_state():
    source = "\n".join(path.read_text(encoding="utf-8") for path in EVENTS_DIR.rglob("*.py"))
    lowered = source.lower()

    for forbidden in ("sqlite", "sqlalchemy", "shelve", "pickle", "json.dump(", "write_text("):
        assert forbidden not in lowered, f"event layer persists state: {forbidden}"


def test_events_package_public_api_is_contracts_and_registry_only():
    exported = set(events_package.__all__)

    for forbidden in ("EventBus", "Publisher", "Subscriber", "Consumer", "Dispatcher"):
        assert forbidden not in exported
    assert {"EventEnvelope", "DomainEvent", "EventRegistry", "decode_event"} <= exported


# --- No trading / authenticated exchange capability ---

def test_no_order_placement_or_cancellation_capability_exists():
    for path in _runtime_python_files_outside_authorised_testnet():
        text = path.read_text(encoding="utf-8").lower()
        for token in FORBIDDEN_TRADING_TOKENS:
            assert token not in text, f"{path.name} contains trading capability: {token}"


def test_no_authenticated_binance_capability_exists():
    for path in _runtime_python_files_outside_authorised_testnet():
        text = path.read_text(encoding="utf-8")
        for token in (
            "BINANCE_API_KEY",
            "BINANCE_API_SECRET",
            "BINANCE_SECRET_KEY",
            "X-MBX-APIKEY",
            "listenKey",
            "recvWindow",
        ):
            assert token not in text, f"{path.name} contains authenticated Binance usage: {token}"


def test_binance_tool_stays_public_and_read_only():
    tool = BinancePublicTool()

    assert tool.base_url == "https://api.binance.com"
    assert tool.kline_endpoint_path == "/api/v3/klines"
    assert tool.price_ticker_endpoint_path == "/api/v3/ticker/price"
    assert tool.ticker_24hr_endpoint_path == "/api/v3/ticker/24hr"
    assert not hasattr(tool, "rapidapi_key")

    source = (PACKAGE_DIR / "tools" / "binance_public_tool.py").read_text(encoding="utf-8").lower()
    assert "requests.get" in source
    assert "requests.post" not in source


# --- Phase 2 four-agent layer untouched ---

def test_all_four_agents_still_resolve_through_the_llm_factory(monkeypatch):
    recorded: list[str] = []
    fake = FakeLLM()

    def fake_get_llm(profile, **kwargs):
        recorded.append(profile)
        return fake

    monkeypatch.setattr(crew_module, "get_llm", fake_get_llm)
    crew = CryptoTraderCrew(verbose=False)

    for name in AGENT_PROFILE_MAP:
        assert getattr(crew, name)().llm is fake

    assert sorted(recorded) == sorted(EXPECTED_PROFILE_NAMES)
    assert tuple(ACTIVE_PROFILE_NAMES) == tuple(EXPECTED_PROFILE_NAMES)
    assert LEGACY_AGENT_NAMES == ()
    assert "deepseek_reasoner" not in ACTIVE_PROFILE_NAMES


def test_agent_definitions_and_prompts_are_unchanged():
    agents = yaml.safe_load((CONFIG_DIR / "agents.yaml").read_text(encoding="utf-8"))

    assert set(agents) == {
        "binance_data_analyst",
        "news_and_sentiment_researcher",
        "crypto_analyst",
        "trading_plan_strategist",
    }
    assert not any("llm" in config for config in agents.values())


def test_no_new_tools_were_added():
    tools_dir = PACKAGE_DIR / "tools"
    tool_modules = {path.name for path in tools_dir.glob("*.py")}

    assert tool_modules == {"__init__.py", "binance_public_tool.py"}


def test_events_layer_does_not_import_llm_or_agent_internals():
    """Contracts must stay independent of the agent/LLM layer."""
    for path in EVENTS_DIR.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for forbidden in ("crewai", "crypto_trader.llm", "crypto_trader.crew"):
            assert forbidden not in text, f"events/{path.name} depends on {forbidden}"
