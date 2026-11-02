"""One-shot, host-owned CrewAI origin session. NO admission dequeue or venue submission.

This is an opt-in composition boundary, not a deployable market/account bootstrap.
All market facts and limits are supplied independently by the trusted host. The
returned evidence is inert: Hard Risk, reconciliation and final venue guards
remain mandatory before any future trading decision.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from uuid import UUID

from crypto_trader import main as cli
from crypto_trader.crew_decision_gateway import CrewAdvisoryReceipt, CrewDecisionGateway
from crypto_trader.crew_testnet_host_market import TestnetNoSubmitMarketBinding
from crypto_trader.crew_intent_policy import (
    ApprovedIntentConfig, HostDecisionTimes, InstrumentFilters,
    VerifiedMarketQuote, make_policy,
)
from crypto_trader.events import StrategyDecisionRecorded, TradeIntentCreated
from crypto_trader.execution import ExecutionRuntimeState
from crypto_trader.production.admission import ProductionAdmissionController
from crypto_trader.production.errors import ProductionStateError
from crypto_trader.production.runtime import CrewOriginAuthority, ProductionRuntime
from crypto_trader.risk import RiskLimits


@dataclass(frozen=True)
class HostNoSubmitEvidence:
    """Validated decision lineage and bounded economics, NOT an order approval."""

    receipt: CrewAdvisoryReceipt
    decision_event_id: UUID
    intent_event_id: UUID
    notional_quote: Decimal


class CrewNoSubmitHostSession:
    """Validate one real opt-in CLI receipt while preserving an empty admission queue.

    The host MUST have constructed and started its durable runtime separately.
    This class intentionally provides no offer(), run_once() or execution path.
    """

    def __init__(
        self, *, journal, runtime: ProductionRuntime,
        admission: ProductionAdmissionController, authority: CrewOriginAuthority,
        producer: str, config: ApprovedIntentConfig,
        market_quote: VerifiedMarketQuote, host_times: HostDecisionTimes,
        instrument_filters: InstrumentFilters, risk_limits: RiskLimits,
        testnet_market_binding: TestnetNoSubmitMarketBinding | None = None,
    ) -> None:
        if (
            type(runtime) is not ProductionRuntime
            or type(admission) is not ProductionAdmissionController
            or admission._runtime is not runtime
            or runtime._admission_token is not admission._runtime_token
            or journal is not runtime._journal
            or type(authority) is not CrewOriginAuthority
            or type(config) is not ApprovedIntentConfig
            or type(market_quote) is not VerifiedMarketQuote
            or type(host_times) is not HostDecisionTimes
            or type(instrument_filters) is not InstrumentFilters
            or type(risk_limits) is not RiskLimits
            or risk_limits is not runtime._limits
        ):
            raise ProductionStateError(
                "no-submit host requires one explicitly bound runtime, journal, admission and risk policy."
            )
        if testnet_market_binding is not None:
            self._assert_strict_market_binding(
                runtime, testnet_market_binding, market_quote, instrument_filters,
            )
        self._testnet_market_binding = testnet_market_binding
        self._instrument_filters = instrument_filters
        self._runtime = runtime
        self._journal = journal
        self._admission = admission
        self._config = config
        self._quote = market_quote
        self._limits = risk_limits
        self._used = False
        self._gateway = CrewDecisionGateway(
            journal=journal, runtime=runtime, authority=authority,
            producer=producer,
            policy=make_policy(
                config=config, market_quote=market_quote, host_times=host_times,
                instrument_filters=instrument_filters, risk_limits=risk_limits,
            ),
        )

    @staticmethod
    def _assert_strict_market_binding(
        runtime: ProductionRuntime,
        binding: TestnetNoSubmitMarketBinding,
        market_quote: VerifiedMarketQuote,
        instrument_filters: InstrumentFilters,
    ) -> None:
        """Revalidate the exact source and continuity fence at construction and run."""
        source = runtime._risk_state_provider
        factory = getattr(source, "_mark_source_factory", None)
        owner = getattr(binding, "_bootstrap_owner", None) or binding
        continuity = getattr(source, "_continuity_max_move_bps", None)
        if (
            type(binding) is not TestnetNoSubmitMarketBinding
            or market_quote is not binding.quote
            or instrument_filters is not binding.instrument_filters
            or runtime._market_state is not binding.market_state
            or getattr(factory, "__self__", None) is not owner
            or getattr(factory, "__func__", None) is not getattr(type(owner), "new_risk_reader", None)
            or runtime._instrument is not binding.instrument
            or type(continuity) is not Decimal
            or not continuity.is_finite()
            or continuity <= 0
            or continuity > runtime._validity_gate.policy.max_market_move_bps
        ):
            raise ProductionStateError(
                "strict Testnet host requires one pre-start bound market and risk evidence source."
            )

    def _require_current_risk_policy(self) -> None:
        """Refuse evidence after the bound runtime risk policy is replaced."""
        if self._runtime._limits is not self._limits:
            raise ProductionStateError(
                "host risk policy binding changed during Crew session."
            )

    def run(self) -> HostNoSubmitEvidence:
        """Invoke one advisory kickoff, verify exact persisted lineage; never enqueue."""
        if self._used:
            raise ProductionStateError("no-submit host session cannot be replayed.")
        if self._testnet_market_binding is not None:
            self._assert_strict_market_binding(
                self._runtime, self._testnet_market_binding,
                self._quote, self._instrument_filters,
            )
        self._runtime._require_owner_thread()
        if self._runtime.state() is not ExecutionRuntimeState.RUNNING:
            raise ProductionStateError("no-submit host requires a running production session.")
        self._runtime._require_session_evidence()
        self._require_current_risk_policy()
        if self._admission.depth() != 0 or self._admission.active_intent_id() is not None:
            raise ProductionStateError("no-submit host refuses nonempty or active admission.")
        before_ids = {event.event_id for event in self._journal.read_range()}
        self._require_current_risk_policy()
        self._used = True
        try:
            receipt = cli.run(
                advisory_gateway=self._gateway, return_origin_receipt=True,
            )
        except SystemExit as exc:
            raise ProductionStateError(
                "Crew advisory failed; no admission or submission attempted."
            ) from exc
        self._require_current_risk_policy()
        if type(receipt) is not CrewAdvisoryReceipt:
            raise ProductionStateError("host must receive an exact typed Crew advisory receipt.")
        self._gateway.delivery_ledger.verify(receipt.source_delivery_id, receipt.raw_plan)
        intent = receipt.intent
        self._runtime._verify_durable_origin(intent)
        decision = self._journal.read_by_id(intent.origin_decision_event_id)
        created = self._journal.read_by_id(intent.intent_id)
        if not isinstance(decision, StrategyDecisionRecorded) or not isinstance(
            created, TradeIntentCreated
        ):
            raise ProductionStateError("host decision/intent journal lineage is missing.")
        after_ids = {event.event_id for event in self._journal.read_range()}
        if after_ids - before_ids != {decision.event_id, created.event_id}:
            raise ProductionStateError("host kickoff produced unexpected journal events.")
        price = intent.limit_price
        notional = intent.quantity * price if price is not None else Decimal("NaN")
        limits = self._limits
        ceilings = (
            self._config.approved_notional_quote,
            limits.max_order_notional_quote,
            limits.max_symbol_exposure_quote,
            limits.max_gross_exposure_quote,
            limits.max_net_exposure_quote,
            limits.max_strategy_exposure_quote,
        )
        if (
            intent.symbol != self._config.symbol
            or intent.strategy_id != self._config.strategy_id
            or intent.market_reference != self._quote.reference_id
            or intent.market_event_ts != self._quote.event_ts
            or not notional.is_finite()
            or notional <= 0
            or any(notional > cap for cap in ceilings)
            or limits.trading_halted
            or limits.reduce_only_mode
        ):
            raise ProductionStateError("host receipt exceeds approved facts or static risk bounds.")
        if self._admission.depth() != 0 or self._admission.active_intent_id() is not None:
            raise ProductionStateError("no-submit host cannot leave queued or active intents.")
        self._require_current_risk_policy()
        return HostNoSubmitEvidence(
            receipt=receipt, decision_event_id=decision.event_id,
            intent_event_id=created.event_id, notional_quote=notional,
        )