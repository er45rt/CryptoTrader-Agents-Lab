"""Explicit read-only market bootstrap for a trusted, no-submit Testnet host.

The host owns the clock, account, budget, runtime and admission. This module
issues no orders and never turns an advisory receipt into permission to submit.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING

from crypto_trader.crew_intent_policy import InstrumentFilters, VerifiedMarketQuote
from crypto_trader.execution import InstrumentSpec
from crypto_trader.execution.binance_testnet.market_data import (
    BinanceTestnetTickerQuoteSource, TestnetTickerQuoteError, filters_from_testnet_rules,
)
from crypto_trader.execution.binance_testnet.models import TestnetSymbolRules
from crypto_trader.validity import LiveMarketStateProvider, MarketObservation, MarketStateProvider, ReplayMarketStateProvider

if TYPE_CHECKING:
    from crypto_trader.breakglass import BreakGlassControl
    from crypto_trader.execution.binance_testnet import BinanceSpotTestnetClient
    from crypto_trader.persistence import EventJournal
    from crypto_trader.production.risk_state import ProductionRiskStateSource


class TestnetHostMarketBindingError(ValueError):
    """Host has not supplied authoritative, matching market facts."""


@dataclass(frozen=True)
class TestnetNoSubmitMarketBinding:
    """Immutable initial quote, venue filters and a per-refresh risk-mark factory."""

    quote: VerifiedMarketQuote
    observation: MarketObservation
    market_state: MarketStateProvider
    instrument_filters: InstrumentFilters
    instrument: InstrumentSpec
    _ticker_factory: Callable[[], BinanceTestnetTickerQuoteSource]
    _bootstrap_owner: object | None = None

    @classmethod
    def capture(
        cls,
        *,
        instrument: InstrumentSpec,
        venue_rules: TestnetSymbolRules,
        ticker_factory: Callable[[], BinanceTestnetTickerQuoteSource],
    ) -> "TestnetNoSubmitMarketBinding":
        if type(instrument) is not InstrumentSpec or instrument.venue != "BINANCE_SPOT_TESTNET":
            raise TestnetHostMarketBindingError("requires a strict Binance Spot Testnet instrument.")
        if type(venue_rules) is not TestnetSymbolRules or venue_rules.symbol != instrument.symbol:
            raise TestnetHostMarketBindingError("exchange rules do not match instrument.")
        if venue_rules.base_asset != instrument.base_currency or venue_rules.quote_asset != instrument.quote_currency:
            raise TestnetHostMarketBindingError("exchange asset identities do not match instrument.")
        if not callable(ticker_factory):
            raise TestnetHostMarketBindingError("host must provide an explicit per-read ticker factory.")
        filters = filters_from_testnet_rules(venue_rules)
        reader = ticker_factory()
        if type(reader) is not BinanceTestnetTickerQuoteSource or reader.symbol != instrument.symbol:
            raise TestnetHostMarketBindingError("initial market source is not the pinned Testnet ticker.")
        quote, observation = reader.read_once_bound()
        if quote.reference_id != observation.market_reference or quote.event_ts != observation.market_event_ts:
            raise TestnetHostMarketBindingError("initial quote and validity observation disagree.")
        return cls(
            quote=quote,
            observation=observation,
            market_state=ReplayMarketStateProvider((observation,)),
            instrument_filters=filters,
            instrument=instrument,
            _ticker_factory=ticker_factory,
        )

    def new_risk_reader(self) -> BinanceTestnetTickerQuoteSource:
        """Return a new one-shot source; ProductionRiskStateSource validates it at use."""
        reader = self._ticker_factory()
        if type(reader) is not BinanceTestnetTickerQuoteSource or reader.symbol != self.instrument.symbol:
            raise TestnetHostMarketBindingError("risk market reader is not the pinned Testnet ticker.")
        return reader

    def canonical_risk_source(
        self,
        *,
        journal: "EventJournal",
        control: "BreakGlassControl",
        client: "BinanceSpotTestnetClient",
        venue_account_id: str,
        max_account_age_seconds: float = 5.0,
        max_mark_age_seconds: float = 5.0,
        max_decision_mark_move_bps: Decimal | None = None,
    ) -> "ProductionRiskStateSource":
        """Construct the canonical source BEFORE runtime startup, never by private mutation."""
        from crypto_trader.production.risk_state import ProductionRiskStateSource

        if client.venue_label != "binance-spot-testnet":
            raise TestnetHostMarketBindingError("market evidence is bound to Testnet only.")
        return ProductionRiskStateSource(
            journal=journal,
            control=control,
            client=client,
            instrument=self.instrument,
            environment="binance-spot-testnet",
            venue_account_id=venue_account_id,
            max_account_age_seconds=max_account_age_seconds,
            max_mark_age_seconds=max_mark_age_seconds,
            mark_source_factory=self.new_risk_reader,
            max_decision_mark_move_bps=max_decision_mark_move_bps,
        )

class TestnetPostStartMarketBootstrap:
    """Prepare risk before startup; capture decision quote strictly after startup.

    This is an explicit, single-use, read-only composition boundary. The market
    provider starts empty (fail closed) and is populated exactly once after a
    successful RUNNING runtime fence. It does not enqueue or submit orders.
    """

    def __init__(
        self, *, instrument: InstrumentSpec, venue_rules: TestnetSymbolRules,
        ticker_factory: Callable[[], BinanceTestnetTickerQuoteSource],
    ) -> None:
        if (type(instrument) is not InstrumentSpec
                or instrument.venue != "BINANCE_SPOT_TESTNET"
                or type(venue_rules) is not TestnetSymbolRules
                or venue_rules.symbol != instrument.symbol
                or venue_rules.base_asset != instrument.base_currency
                or venue_rules.quote_asset != instrument.quote_currency):
            raise TestnetHostMarketBindingError("post-start market bootstrap requires pinned testnet instrument and rules.")
        if not callable(ticker_factory):
            raise TestnetHostMarketBindingError("post-start market bootstrap requires ticker factory.")
        self.instrument = instrument
        self.instrument_filters = filters_from_testnet_rules(venue_rules)
        self._ticker_factory = ticker_factory
        self._observation: MarketObservation | None = None
        self._captured = False
        self._risk_source = None
        self.market_state = LiveMarketStateProvider(
            lambda symbol: self._observation if symbol == instrument.symbol else None,
        )

    def new_risk_reader(self) -> BinanceTestnetTickerQuoteSource:
        reader = self._ticker_factory()
        if type(reader) is not BinanceTestnetTickerQuoteSource or reader.symbol != self.instrument.symbol:
            raise TestnetHostMarketBindingError("post-start risk reader source or symbol is untrusted.")
        return reader

    def canonical_risk_source(
        self, *, journal: "EventJournal", control: "BreakGlassControl",
        client: "BinanceSpotTestnetClient", venue_account_id: str,
        max_decision_mark_move_bps: Decimal,
        max_account_age_seconds: float = 5.0, max_mark_age_seconds: float = 5.0,
    ) -> "ProductionRiskStateSource":
        from crypto_trader.production.risk_state import ProductionRiskStateSource
        if self._risk_source is not None:
            raise TestnetHostMarketBindingError("post-start risk source is single-use per runtime.")
        if client.venue_label != "binance-spot-testnet":
            raise TestnetHostMarketBindingError("post-start risk source requires Testnet.")
        source = ProductionRiskStateSource(
            journal=journal, control=control, client=client, instrument=self.instrument,
            environment="binance-spot-testnet", venue_account_id=venue_account_id,
            max_account_age_seconds=max_account_age_seconds,
            max_mark_age_seconds=max_mark_age_seconds,
            max_decision_mark_move_bps=max_decision_mark_move_bps,
            mark_source_factory=self.new_risk_reader,
        )
        self._risk_source = source
        return source

    def capture_after_start(self, runtime) -> TestnetNoSubmitMarketBinding:
        from crypto_trader.execution import ExecutionRuntimeState
        from crypto_trader.production.runtime import ProductionRuntime

        if self._captured:
            raise TestnetHostMarketBindingError("post-start decision capture is single-use.")
        if (type(runtime) is not ProductionRuntime
                or runtime.state() is not ExecutionRuntimeState.RUNNING
                or runtime._risk_state_provider is not self._risk_source
                or runtime._market_state is not self.market_state
                or runtime._instrument is not self.instrument
                or runtime._startup is None):
            raise TestnetHostMarketBindingError("runtime must be started with this exact prebound market and risk source.")
        runtime._require_owner_thread()
        runtime._require_session_evidence()
        self._captured = True   # even a failed read cannot be retried implicitly
        quote, observation = self.new_risk_reader().read_once_bound()
        if quote.event_ts < runtime._startup.started_at or quote.received_at < runtime._startup.started_at:
            raise TestnetHostMarketBindingError("decision quote predates authenticated runtime session start.")
        if quote.reference_id != observation.market_reference or quote.event_ts != observation.market_event_ts:
            raise TestnetHostMarketBindingError("quote and market observation lineage mismatch.")
        self._observation = observation
        return TestnetNoSubmitMarketBinding(
            quote=quote, observation=observation, market_state=self.market_state,
            instrument_filters=self.instrument_filters, instrument=self.instrument,
            _ticker_factory=self._ticker_factory, _bootstrap_owner=self,
        )
