"""Offline CrewAI source-bound origin proof; never calls admission.run_once or venue POST."""
import json
from pathlib import Path
from dataclasses import replace
from uuid import uuid4

import pytest

from crypto_trader.persistence import SQLiteEventJournal
from crypto_trader.production import ProductionStateError
from crypto_trader.crew_decision_gateway import CrewDecisionGateway
from crypto_trader.production.runtime import CrewOriginAuthority
from crypto_trader.tests.production_fixtures import (
    BASE, bind_test_admission, seed_canonical_equity_root, startup_responses,
)
from crypto_trader.tests.test_production_startup_evidence import wired
from crypto_trader.tests.test_r03a1b_origin_binding import NOW, fresh, record_pair


def plan(*, bias="bullish", confidence="high", symbol="BTCUSDT"):
    return "```json\n" + json.dumps({
        "symbol": symbol, "bias": bias, "confidence": confidence,
        "human_readable_plan": "Offline plan; human-readable only.",
        "position_size_pct": 1.0, "entry_low": 26900.0, "entry_high": 27100.0,
        "stop_loss": 26500.0,
        "take_profit_targets": [{"level": 29000.0, "allocation_pct": 100.0}],
        "max_loss_pct": 1.0, "risk_reward_ratio": 1.5,
        "plan_validity": "60 seconds only", "invalidation_conditions": ["market gap"],
    }) + "\n```"


def trusted_issue(gateway, raw_plan: str, output_dir: Path):
    """Synthetic host-delivery fixture; actual kickoff path is tested separately."""
    from crypto_trader.llm.schemas import parse_trading_plan

    symbol = parse_trading_plan(raw_plan).symbol
    ledger = gateway.delivery_ledger
    attempt = ledger.begin(output_dir, symbol=symbol)
    artifact = attempt.directory / f"trading_plan_{symbol}_{attempt.delivery_id}.md"
    artifact.write_bytes(raw_plan.encode("utf-8"))
    ledger.complete(attempt, raw_plan, artifact)
    return gateway.issue(raw_plan, source_delivery_id=attempt.delivery_id), attempt.delivery_id


@pytest.fixture
def verified(tmp_path):
    with SQLiteEventJournal(tmp_path / "crew-origin.db", venue_writes=True) as journal:
        seed_canonical_equity_root(journal)
        authority = CrewOriginAuthority()
        production, _, _, transport = wired(
            journal, tmp_path, startup_responses(), when=NOW,
            bind_admission=False, require_origin_binding=True,
            trusted_origin_producer="gateway.crew_strategist",
            origin_authority=authority,
        )
        admission = bind_test_admission(production, when=NOW)
        production.start()
        yield journal, authority, production, admission, transport, tmp_path


def test_forged_same_economics_and_trusted_label_cannot_mint_authority(verified):
    journal, authority, runtime, admission, transport, output_dir = verified
    malicious = fresh("gateway-forgery")
    record_pair(journal, malicious, producer="gateway.crew_strategist")
    assert admission.offer(malicious) == (True, "accepted")
    with pytest.raises(ProductionStateError, match="authority|attest"):
        runtime.prepare_intent(malicious, _admission_token=admission._runtime_token)
    assert not [req for req in transport.requests if req.method == "POST"]


def test_real_gateway_mints_once_and_attests_exact_journal_bytes(verified):
    journal, authority, runtime, admission, transport, output_dir = verified
    gateway = CrewDecisionGateway(
        journal=journal, runtime=runtime, authority=authority,
        producer="gateway.crew_strategist", policy=lambda parsed: fresh("crew"),
    )
    candidate, delivery_id = trusted_issue(gateway, plan(), output_dir)
    assert admission.offer(candidate) == (True, "accepted")
    runtime._verify_durable_origin(candidate)
    with pytest.raises(ProductionStateError, match="already|replay"):
        gateway.issue(plan(), source_delivery_id=delivery_id)
    with pytest.raises(ProductionStateError, match="replay"):
        trusted_issue(gateway, plan(), output_dir)
    with pytest.raises(ProductionStateError, match="conflicting|attest"):
        runtime._verify_durable_origin(replace(candidate, quantity=candidate.quantity * 2))
    assert not [req for req in transport.requests if req.method == "POST"]


@pytest.mark.parametrize("untrusted_plan", [
    plan(bias="neutral"), plan(confidence="low"), plan(symbol="ETHUSDT"),
])
def test_gateway_fails_closed_before_writing_for_non_actionable_plan(verified, untrusted_plan):
    journal, authority, runtime, admission, transport, output_dir = verified
    gateway = CrewDecisionGateway(
        journal=journal, runtime=runtime, authority=authority,
        producer="gateway.crew_strategist", policy=lambda parsed: fresh("crew-bad"),
    )
    n_before = len(journal.read_range())
    with pytest.raises(ProductionStateError, match="plan|neutral|confidence|symbol"):
        trusted_issue(gateway, untrusted_plan, output_dir)
    assert len(journal.read_range()) == n_before
    assert not [req for req in transport.requests if req.method == "POST"]

@pytest.mark.parametrize("bad_case", ["wrong_side", "market_order", "untyped"])
def test_gateway_never_lets_untrusted_plan_supply_execution_terms(verified, bad_case):
    from crypto_trader.execution import ExecutionOrderType, ExecutionSide

    journal, authority, runtime, admission, transport, output_dir = verified

    def bad_policy(parsed):
        current = fresh("bad-economics")
        if bad_case == "wrong_side":
            return replace(current, side=ExecutionSide.SELL)
        if bad_case == "market_order":
            return replace(current, order_type=ExecutionOrderType.MARKET, limit_price=None)
        return {"symbol": "BTCUSDT", "quantity": "0.01"}

    gateway = CrewDecisionGateway(
        journal=journal, runtime=runtime, authority=authority,
        producer="gateway.crew_strategist", policy=bad_policy,
    )
    before = len(journal.read_range())
    with pytest.raises(ProductionStateError, match="economics|typed"):
        trusted_issue(gateway, plan(), output_dir)
    assert len(journal.read_range()) == before
    assert not [req for req in transport.requests if req.method == "POST"]


def test_gateway_binding_cannot_be_reused_for_another_producer(verified):
    journal, authority, runtime, admission, transport, output_dir = verified
    gateway = CrewDecisionGateway(
        journal=journal, runtime=runtime, authority=authority,
        producer="gateway.crew_strategist", policy=lambda parsed: fresh(),
    )
    with pytest.raises(ProductionStateError, match="already bound"):
        CrewDecisionGateway(
            journal=journal, runtime=runtime, authority=authority,
            producer="gateway.crew_strategist", policy=lambda parsed: fresh(),
        )
    assert not [req for req in transport.requests if req.method == "POST"]


def test_gateway_issued_intent_passes_actual_prepare_without_venue_submit(tmp_path):
    from crypto_trader.tests.production_fixtures import canonical_risk_refresh_responses

    with SQLiteEventJournal(tmp_path / "gateway-prepare.db", venue_writes=True) as journal:
        seed_canonical_equity_root(journal)
        authority = CrewOriginAuthority()
        runtime, _, _, transport = wired(
            journal, tmp_path,
            [*startup_responses(), *canonical_risk_refresh_responses()],
            when=NOW, bind_admission=False, require_origin_binding=True,
            trusted_origin_producer="gateway.crew_strategist",
            origin_authority=authority,
        )
        admission = bind_test_admission(runtime, when=NOW)
        runtime.start()
        gateway = CrewDecisionGateway(
            journal=journal, runtime=runtime, authority=authority,
            producer="gateway.crew_strategist",
            policy=lambda parsed: fresh("e2e-gateway"),
        )
        candidate, _ = trusted_issue(gateway, plan(), tmp_path)
        assert admission.offer(candidate) == (True, "accepted")
        preparation = runtime.prepare_intent(
            candidate, _admission_token=admission._runtime_token,
        )
        assert preparation.approved
        assert not [req for req in transport.requests if req.method == "POST"]


def test_actual_main_kickoff_binds_host_delivery_to_journal_without_post(verified, monkeypatch):
    from crypto_trader import main as cli

    journal, authority, runtime, admission, transport, output_dir = verified
    gateway = CrewDecisionGateway(
        journal=journal, runtime=runtime, authority=authority,
        producer="gateway.crew_strategist", policy=lambda parsed: fresh("real-host"),
    )
    prior_events = journal.read_range()
    before = len(prior_events)
    prior_ids = {event.event_id for event in prior_events}

    class FakeCrew:
        def crew(self):
            return self

        def kickoff(self, *, inputs):
            assert inputs["target_symbol"] == "BTCUSDT"
            pending = list(output_dir.glob("crew_delivery_*.pending.json"))
            assert len(pending) == 1  # Persisted BEFORE CrewAI kickoff.
            assert list(output_dir.glob("crew_delivery_*.completed.json")) == []
            assert len(journal.read_range()) == before
            return plan()

    monkeypatch.setattr(cli, "CryptoTraderCrew", FakeCrew)
    result = cli.run_crew(
        {"target_symbol": "BTCUSDT", "filename_datetime": "ignored"},
        output_dir, advisory_gateway=gateway,
    )
    assert result == plan()
    completed = list(output_dir.glob("crew_delivery_*.completed.json"))
    assert len(completed) == 1
    manifest = json.loads(completed[0].read_bytes())
    artifact = output_dir / manifest["plan_file"]
    assert artifact.read_bytes() == plan().encode("utf-8")
    import hashlib

    assert manifest["plan_sha256"] == hashlib.sha256(artifact.read_bytes()).hexdigest()
    from crypto_trader.events import StrategyDecisionRecorded, TradeIntentCreated

    additions = [event for event in journal.read_range() if event.event_id not in prior_ids]
    assert len(additions) == 2
    assert sum(isinstance(event, StrategyDecisionRecorded) for event in additions) == 1
    assert sum(isinstance(event, TradeIntentCreated) for event in additions) == 1
    assert not [req for req in transport.requests if req.method == "POST"]


def test_main_failed_crew_leaves_pending_record_but_no_authoritative_delivery(verified, monkeypatch):
    from crypto_trader import main as cli

    journal, authority, runtime, admission, transport, output_dir = verified
    gateway = CrewDecisionGateway(
        journal=journal, runtime=runtime, authority=authority,
        producer="gateway.crew_strategist", policy=lambda parsed: fresh("failed-host"),
    )
    before = len(journal.read_range())

    class FailingCrew:
        def crew(self):
            return self

        def kickoff(self, *, inputs):
            raise RuntimeError("synthetic model failure")

    monkeypatch.setattr(cli, "CryptoTraderCrew", FailingCrew)
    assert cli.run_crew(
        {"target_symbol": "BTCUSDT", "filename_datetime": "unused"},
        output_dir, advisory_gateway=gateway,
    ) is None
    assert len(list(output_dir.glob("crew_delivery_*.pending.json"))) == 1
    assert not list(output_dir.glob("crew_delivery_*.completed.json"))
    assert len(journal.read_range()) == before
    assert not [req for req in transport.requests if req.method == "POST"]


@pytest.mark.parametrize("changed_file", ["plan", "completed", "pending"])
def test_gateway_rejects_persisted_delivery_tamper(verified, changed_file):
    journal, authority, runtime, admission, transport, output_dir = verified
    gateway = CrewDecisionGateway(
        journal=journal, runtime=runtime, authority=authority,
        producer="gateway.crew_strategist", policy=lambda parsed: fresh("tampered"),
    )
    attempt = gateway.delivery_ledger.begin(output_dir, symbol="BTCUSDT")
    artifact = output_dir / f"trading_plan_BTCUSDT_{attempt.delivery_id}.md"
    artifact.write_bytes(plan().encode("utf-8"))
    gateway.delivery_ledger.complete(attempt, plan(), artifact)
    paths = {
        "plan": artifact,
        "completed": output_dir / f"crew_delivery_{attempt.delivery_id}.completed.json",
        "pending": output_dir / f"crew_delivery_{attempt.delivery_id}.pending.json",
    }
    paths[changed_file].write_bytes(paths[changed_file].read_bytes() + b"UNTRUSTED")
    before = len(journal.read_range())
    with pytest.raises(ProductionStateError, match="content|origin|receipt"):
        gateway.issue(plan(), source_delivery_id=attempt.delivery_id)
    assert len(journal.read_range()) == before
    assert not [req for req in transport.requests if req.method == "POST"]


def test_unknown_or_restarted_host_cannot_adopt_deliveries(verified):
    from crypto_trader.crew_decision_gateway import CrewDeliveryLedger

    journal, authority, runtime, admission, transport, output_dir = verified
    gateway = CrewDecisionGateway(
        journal=journal, runtime=runtime, authority=authority,
        producer="gateway.crew_strategist", policy=lambda parsed: fresh("old"),
    )
    with pytest.raises(ProductionStateError, match="not finalized"):
        gateway.issue(plan(), source_delivery_id=uuid4())
    attempt = gateway.delivery_ledger.begin(output_dir, symbol="BTCUSDT")
    artifact = output_dir / f"trading_plan_BTCUSDT_{attempt.delivery_id}.md"
    artifact.write_bytes(plan().encode("utf-8"))
    gateway.delivery_ledger.complete(attempt, plan(), artifact)
    replacement = CrewDeliveryLedger()
    with pytest.raises(ProductionStateError, match="not finalized"):
        replacement.verify(attempt.delivery_id, plan())
    assert not [req for req in transport.requests if req.method == "POST"]


def test_main_legacy_run_remains_advisory_without_delivery_or_journal(verified, monkeypatch):
    from crypto_trader import main as cli

    journal, authority, runtime, admission, transport, output_dir = verified
    before = len(journal.read_range())

    class FakeCrew:
        def crew(self):
            return self

        def kickoff(self, *, inputs):
            return plan()

    monkeypatch.setattr(cli, "CryptoTraderCrew", FakeCrew)
    assert cli.run_crew(
        {"target_symbol": "BTCUSDT", "filename_datetime": "20000101_010101"},
        output_dir,
    ) == plan()
    assert (output_dir / "trading_plan_BTCUSDT_20000101_010101.md").is_file()
    assert not list(output_dir.glob("crew_delivery_*.pending.json"))
    assert len(journal.read_range()) == before
    assert not [req for req in transport.requests if req.method == "POST"]
