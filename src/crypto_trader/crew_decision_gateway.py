"""Opt-in, in-process CrewAI advisory-to-origin boundary (offline candidate).

The CrewAI plan is untrusted advice, NEVER a submission grant. Only a separately
configured deterministic host policy may construct a ProductionIntent. This
session-scoped authority stops *journal-only* event injection from fabricating
new admitted roots; it is not a cryptographic identity or a process-compromise
defence. No mainnet/testnet integration or auto-execution is provided.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, replace
from decimal import Decimal
from pathlib import Path
from typing import Callable
from uuid import UUID, uuid4, uuid5

from crypto_trader.events import (
    StrategyDecisionPayload, StrategyDecisionRecorded,
    TradeIntentCreated, TradeIntentPayload,
)
from crypto_trader.execution import ExecutionOrderType, ExecutionSide
from crypto_trader.llm.schemas import StructuredOutputError, TradingPlan, parse_trading_plan

from crypto_trader.production.errors import ProductionStateError
from crypto_trader.production.runtime import CrewOriginAuthority, ProductionIntent, ProductionRuntime

_SOURCE_NAMESPACE = UUID("f5a9b7ba-a3fb-4a58-9abe-877251452edf")
_JSON_BLOCK = re.compile(r"```json\s*.*?```", re.IGNORECASE | re.DOTALL)
_PRODUCER_PATTERN = re.compile(r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)*$")
_SYMBOL_PATTERN = re.compile(r"^[A-Z0-9]{5,20}$")
_DELIVERY_MAX_BYTES = 2_000_000


@dataclass(frozen=True)
class CrewDeliveryAttempt:
    """Host-owned identity reserved durably before a CrewAI kickoff."""

    delivery_id: UUID
    symbol: str
    directory: Path


class CrewDeliveryLedger:
    """Fail-closed local origin ledger; persistent evidence, session-only issuance.

    A journal writer cannot create a trusted delivery merely by inventing a UUID,
    because this host object must have minted and completed it in this process.
    Files are proof of what was emitted, not a cryptographic signature. A restart
    does not restore the authority to issue old deliveries automatically.
    """

    def __init__(self) -> None:
        self._pending: dict[UUID, CrewDeliveryAttempt] = {}
        self._issued: dict[UUID, tuple[CrewDeliveryAttempt, str, Path, bytes]] = {}

    @staticmethod
    def _record_bytes(values: dict[str, str | int]) -> bytes:
        return (json.dumps(values, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")

    @staticmethod
    def _durable_exclusive_write(path: Path, contents: bytes) -> None:
        with path.open("xb") as stream:
            stream.write(contents)
            stream.flush()
            os.fsync(stream.fileno())

    def begin(self, output_dir: Path, *, symbol: str) -> CrewDeliveryAttempt:
        if not isinstance(symbol, str) or not _SYMBOL_PATTERN.fullmatch(symbol):
            raise ProductionStateError("Crew delivery requires a validated uppercase symbol.")
        path = Path(output_dir)
        if path.is_symlink() or not path.is_dir():
            raise ProductionStateError("Crew delivery output directory must exist and not be a symlink.")
        directory = path.resolve(strict=True)
        attempt = CrewDeliveryAttempt(uuid4(), symbol, directory)
        pending_path = directory / f"crew_delivery_{attempt.delivery_id}.pending.json"
        self._durable_exclusive_write(pending_path, self._record_bytes({
            "version": 1, "state": "pending", "delivery_id": str(attempt.delivery_id),
            "symbol": symbol,
        }))
        self._pending[attempt.delivery_id] = attempt
        return attempt

    def complete(self, attempt: CrewDeliveryAttempt, raw_plan: str, plan_path: Path) -> None:
        if (not isinstance(attempt, CrewDeliveryAttempt)
                or self._pending.get(attempt.delivery_id) is not attempt
                or attempt.delivery_id in self._issued):
            raise ProductionStateError("Crew delivery identity was not minted by this host session.")
        if not isinstance(raw_plan, str):
            raise ProductionStateError("Crew delivery output must be text.")
        raw_bytes = raw_plan.encode("utf-8")
        if not raw_bytes or len(raw_bytes) > _DELIVERY_MAX_BYTES:
            raise ProductionStateError("Crew delivery output exceeds accepted bounds.")
        if len(_JSON_BLOCK.findall(raw_plan)) != 1:
            raise ProductionStateError("Crew delivery output must contain one JSON plan block.")
        try:
            parsed = parse_trading_plan(raw_plan)
        except StructuredOutputError as exc:
            raise ProductionStateError("Crew delivery failed structured plan validation.") from exc
        if parsed.symbol != attempt.symbol:
            raise ProductionStateError("Crew output symbol differs from host-requested market.")
        expected_path = attempt.directory / f"trading_plan_{attempt.symbol}_{attempt.delivery_id}.md"
        path = Path(plan_path)
        if path != expected_path or path.is_symlink() or not path.is_file():
            raise ProductionStateError("Crew delivery output path is not the reserved immutable artifact.")
        if path.stat().st_size != len(raw_bytes) or path.read_bytes() != raw_bytes:
            raise ProductionStateError("Crew delivery output bytes disagree with persisted artifact.")
        digest = hashlib.sha256(raw_bytes).hexdigest()
        payload = self._record_bytes({
            "version": 1, "state": "completed", "delivery_id": str(attempt.delivery_id),
            "symbol": attempt.symbol, "plan_file": path.name, "plan_sha256": digest,
        })
        finished = attempt.directory / f"crew_delivery_{attempt.delivery_id}.completed.json"
        self._durable_exclusive_write(finished, payload)
        self._issued[attempt.delivery_id] = (attempt, digest, path, payload)

    def verify(self, source_delivery_id: UUID, raw_plan: str) -> None:
        if not isinstance(source_delivery_id, UUID) or not isinstance(raw_plan, str):
            raise ProductionStateError("Crew delivery is not a trusted host identity.")
        sealed = self._issued.get(source_delivery_id)
        if sealed is None:
            raise ProductionStateError("Crew delivery is not finalized in this host session.")
        attempt, digest, artifact, manifest = sealed
        encoded = raw_plan.encode("utf-8")
        completed = attempt.directory / f"crew_delivery_{source_delivery_id}.completed.json"
        pending = attempt.directory / f"crew_delivery_{source_delivery_id}.pending.json"
        expected_pending = self._record_bytes({
            "version": 1, "state": "pending", "delivery_id": str(source_delivery_id),
            "symbol": attempt.symbol,
        })
        if (hashlib.sha256(encoded).hexdigest() != digest
                or not artifact.is_file() or artifact.is_symlink()
                or not completed.is_file() or completed.is_symlink()
                or not pending.is_file() or pending.is_symlink()
                or artifact.stat().st_size != len(encoded)
                or artifact.read_bytes() != encoded
                or completed.read_bytes() != manifest
                or pending.read_bytes() != expected_pending):
            raise ProductionStateError("Crew delivery durable content or origin receipt changed.")


class CrewDecisionGateway:
    """Trusted host bridge; an LLM can propose, but cannot mint a durable origin alone.

    The host supplies a unique, stable *trusted delivery id* and an independently
    reviewed deterministic policy. Neither may be taken from the model's text.
    """

    def __init__(
        self, *, journal, runtime: ProductionRuntime, authority: CrewOriginAuthority,
        producer: str, policy: Callable[[TradingPlan], ProductionIntent],
        delivery_ledger: CrewDeliveryLedger | None = None,
    ) -> None:
        if (type(authority) is not CrewOriginAuthority
                or runtime._origin_authority is not authority
                or not runtime._require_origin_binding
                or not runtime._durable_venue_writes):
            raise ProductionStateError("Crew gateway requires an authenticated durable runtime binding.")
        if (not isinstance(producer, str) or not _PRODUCER_PATTERN.fullmatch(producer)
                or producer != runtime._trusted_origin_producer):
            raise ProductionStateError("Crew gateway producer differs from the durable runtime binding.")
        if (journal is not runtime._journal or not callable(policy)
                or (delivery_ledger is not None and type(delivery_ledger) is not CrewDeliveryLedger)):
            raise ProductionStateError("Crew gateway requires the runtime journal and trusted policy.")
        self._journal, self._runtime = journal, runtime
        self._authority, self._producer, self._policy = authority, producer, policy
        self.delivery_ledger = delivery_ledger if delivery_ledger is not None else CrewDeliveryLedger()
        authority._bind(self)

    def issue(self, raw_plan: str, *, source_delivery_id: UUID) -> ProductionIntent:
        """Persist one new decision/intent pair, then issue only a *session* receipt.

        This does not call preparation, admission, execution, or venue APIs.
        """
        self._runtime._require_owner_thread()
        self._runtime._require_running()
        self._runtime._require_session_evidence()
        if (not isinstance(source_delivery_id, UUID)
                or self._runtime.startup_report() is None):
            raise ProductionStateError("Crew plan requires a running session and trusted delivery identity.")
        self.delivery_ledger.verify(source_delivery_id, raw_plan)
        if not isinstance(raw_plan, str) or len(_JSON_BLOCK.findall(raw_plan)) != 1:
            raise ProductionStateError("Crew plan must contain exactly one machine-readable JSON block.")
        try:
            plan = parse_trading_plan(raw_plan)
        except StructuredOutputError as exc:
            raise ProductionStateError("Crew plan failed strict schema validation.") from exc
        if (plan.bias == "neutral" or plan.confidence == "low"
                or plan.symbol != self._runtime.instrument.symbol):
            raise ProductionStateError("Crew plan is neutral, low confidence or the wrong symbol.")

        # The non-AI deterministic host policy, not the LLM, decides exact economics.
        candidate = self._policy(plan)
        if not isinstance(candidate, ProductionIntent):
            raise ProductionStateError("Crew policy did not produce a typed, checked intent.")
        direction = ExecutionSide.BUY if plan.bias == "bullish" else ExecutionSide.SELL
        if (candidate.side is not direction
                or candidate.symbol != plan.symbol
                or candidate.order_type is not ExecutionOrderType.LIMIT
                or candidate.limit_price is None
                or not (Decimal(str(plan.entry_low)) <= candidate.limit_price
                        <= Decimal(str(plan.entry_high)))):
            raise ProductionStateError("Crew plan and deterministic order economics disagree.")

        decision_id = uuid5(_SOURCE_NAMESPACE, f"crew-decision:{source_delivery_id}")
        intent_id = uuid5(_SOURCE_NAMESPACE, f"crew-intent:{source_delivery_id}")
        if self._journal.read_by_id(decision_id) or self._journal.read_by_id(intent_id):
            raise ProductionStateError("Crew source delivery already journaled; replay refused.")
        economic_fingerprint = hashlib.sha256(json.dumps({
            "symbol": candidate.symbol, "strategy": candidate.strategy_id,
            "side": candidate.side.value, "order_type": candidate.order_type.value,
            "quantity": str(candidate.quantity.normalize()),
            "limit_price": str(candidate.limit_price.normalize()),
            "market_reference": candidate.market_reference,
            "decision_scope": candidate.decision_scope,
        }, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
        rationale = f"crew_economics_sha256:{economic_fingerprint}"
        if any(
            isinstance(event, StrategyDecisionRecorded)
            and event.producer == self._producer and event.payload.rationale == rationale
            for event in self._journal.read_range()
        ):
            raise ProductionStateError("Crew identical market proposal replay refused.")
        candidate = replace(
            candidate, origin_decision_event_id=decision_id, intent_id=intent_id,
        )
        occurred_at = self._runtime._now()
        decision = StrategyDecisionRecorded(
            event_id=decision_id, occurred_at=occurred_at,
            correlation_id=candidate.correlation_id,
            producer=self._producer, symbol=candidate.symbol,
            strategy_id=candidate.strategy_id,
            payload=StrategyDecisionPayload(
                action=candidate.side.value, confidence=plan.confidence,
                rationale=rationale,
            ),
        )
        created = TradeIntentCreated(
            event_id=intent_id, occurred_at=occurred_at,
            correlation_id=candidate.correlation_id, causation_id=decision_id,
            producer=self._producer, symbol=candidate.symbol,
            strategy_id=candidate.strategy_id,
            payload=TradeIntentPayload(
                side=candidate.side.value, quantity=candidate.quantity,
                order_type=candidate.order_type.value, limit_price=candidate.limit_price,
            ),
        )
        self._journal.append_batch([decision, created])
        self._authority._seal(self, candidate, decision, created)
        return candidate
