"""Phase 10: mode/queue-aware configuration guards and public feed tests."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import UUID

import pytest

from crypto_trader.paper import (
    FrozenMarketDataFeed,
    MarketObservation,
    PaperConfigError,
    PublicMarketDataFeed,
    QueueAwareSettings,
    TradingMode,
    load_session_config,
)
from crypto_trader.paper import modes as paper_modes

BASE = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
SESSION = UUID("55555555-5555-4555-8555-555555555555")


def test_mode_vocabulary_is_exactly_shadow_and_paper():
    assert {mode.value for mode in TradingMode} == {"shadow", "paper"}
    assert TradingMode.SHADOW.places_simulated_orders is False
    assert TradingMode.PAPER.places_simulated_orders is True


def test_tested_queue_aware_settings_are_the_proven_combination():
    settings = paper_modes.tested_queue_aware_settings()

    assert settings.queue_position is True
    assert settings.liquidity_consumption is True
    assert settings.book_type == "L2_MBP"
    assert paper_modes.require_tested_queue_aware_settings(settings) == settings


@pytest.mark.parametrize(
    "settings",
    [
        QueueAwareSettings(queue_position=False, liquidity_consumption=True, book_type="L2_MBP"),
        QueueAwareSettings(queue_position=True, liquidity_consumption=False, book_type="L2_MBP"),
        QueueAwareSettings(queue_position=True, liquidity_consumption=True, book_type="L1_MBP"),
        QueueAwareSettings(queue_position=True, liquidity_consumption=True, book_type="L3_MBO"),
    ],
)
def test_queue_aware_configuration_fails_closed_when_deviating(settings):
    with pytest.raises(PaperConfigError):
        paper_modes.require_tested_queue_aware_settings(settings)


def test_require_tested_queue_aware_settings_rejects_non_settings():
    with pytest.raises(PaperConfigError):
        paper_modes.require_tested_queue_aware_settings("L2_MBP")  # type: ignore[arg-type]


# --- session configuration -------------------------------------------------

def test_session_config_requires_mode_symbol_and_strategy():
    for missing in ("mode", "symbol", "strategy_id"):
        config = {"mode": "paper", "symbol": "ETHUSDT", "strategy_id": "s1"}
        del config[missing]
        with pytest.raises(PaperConfigError):
            load_session_config(config)


def test_session_config_rejects_unknown_keys():
    with pytest.raises(PaperConfigError):
        load_session_config(
            {
                "mode": "paper",
                "symbol": "ETHUSDT",
                "strategy_id": "s1",
                "api_key": "nope",
            }
        )


def test_session_config_rejects_an_unknown_mode():
    with pytest.raises(PaperConfigError):
        load_session_config({"mode": "live", "symbol": "ETHUSDT", "strategy_id": "s1"})


def test_paper_mode_refuses_to_disable_queue_aware_simulation():
    with pytest.raises(PaperConfigError):
        load_session_config(
            {"mode": "paper", "symbol": "ETHUSDT", "strategy_id": "s1", "queue_aware": False}
        )


def test_paper_mode_refuses_partial_queue_aware_settings():
    with pytest.raises(PaperConfigError):
        load_session_config(
            {
                "mode": "paper",
                "symbol": "ETHUSDT",
                "strategy_id": "s1",
                "queue_position": True,
                "liquidity_consumption": True,
                "book_type": "L1_MBP",
            }
        )


def test_session_config_validates_symbol_and_equity():
    with pytest.raises(PaperConfigError):
        load_session_config({"mode": "shadow", "symbol": "ethusdt", "strategy_id": "s1"})
    with pytest.raises(PaperConfigError):
        load_session_config(
            {
                "mode": "shadow",
                "symbol": "ETHUSDT",
                "strategy_id": "s1",
                "starting_equity_quote": "0",
            }
        )
    with pytest.raises(PaperConfigError):
        load_session_config(
            {
                "mode": "shadow",
                "symbol": "ETHUSDT",
                "strategy_id": "s1",
                "starting_equity_quote": "not-a-number",
            }
        )


def test_shadow_defaults_to_not_placing_and_can_be_configured_to_simulate():
    quiet = load_session_config({"mode": "shadow", "symbol": "ETHUSDT", "strategy_id": "s1"})
    loud = load_session_config(
        {"mode": "shadow", "symbol": "ETHUSDT", "strategy_id": "s1", "shadow_submits": True}
    )

    assert quiet.places_simulated_orders is False
    assert loud.places_simulated_orders is True
    assert quiet.mode is TradingMode.SHADOW


def test_session_config_accepts_an_explicit_session_id():
    config = load_session_config(
        {"mode": "paper", "symbol": "ETHUSDT", "strategy_id": "s1", "session_id": str(SESSION)}
    )

    assert config.session_id == SESSION




def test_shadow_submission_mode_refuses_to_disable_queue_aware_simulation():
    with pytest.raises(PaperConfigError):
        load_session_config(
            {
                "mode": "shadow",
                "symbol": "ETHUSDT",
                "strategy_id": "s1",
                "shadow_submits": True,
                "queue_aware": False,
            }
        )


def test_quiet_shadow_mode_may_disable_queue_aware_simulation_because_it_places_nothing():
    config = load_session_config(
        {
            "mode": "shadow",
            "symbol": "ETHUSDT",
            "strategy_id": "s1",
            "shadow_submits": False,
            "queue_aware": False,
        }
    )

    assert config.places_simulated_orders is False
    assert config.queue_aware is False


# --- feeds ----------------------------------------------------------------

def test_frozen_feed_rejects_out_of_order_observations():
    with pytest.raises(PaperConfigError):
        FrozenMarketDataFeed(
            entries=(
                MarketObservation("ETHUSDT", Decimal("1"), BASE, "ref#1"),
                MarketObservation(
                    "ETHUSDT", Decimal("1"), BASE - timedelta(seconds=1), "ref#0"
                ),
            )
        )


def test_frozen_feed_from_index_is_a_tail_slice():
    feed = FrozenMarketDataFeed(
        entries=tuple(
            MarketObservation("ETHUSDT", Decimal("2700"), BASE, f"ref#{index}")
            for index in range(3)
        )
    )

    assert [entry.market_reference for entry in feed.from_index(1)] == ["ref#1", "ref#2"]
    assert len(feed) == 3
    assert len(feed.observations()) == 3


def test_frozen_feed_rejects_a_negative_index():
    with pytest.raises(PaperConfigError):
        FrozenMarketDataFeed(entries=()).from_index(-1)


def test_public_feed_pulls_public_batches_without_network():
    calls: list[tuple[str, datetime | None]] = []

    def fetch(symbol: str, after: datetime | None):
        calls.append((symbol, after))
        return [MarketObservation(symbol, Decimal("2700"), BASE, "public.kline.1m#0")]

    feed = PublicMarketDataFeed(symbol="ETHUSDT", fetch_batch=fetch)

    assert feed.pull() == 1
    assert feed.cursor == 1
    assert [entry.market_reference for entry in feed.observations()] == ["public.kline.1m#0"]
    assert calls == [("ETHUSDT", None)]
    assert [entry.market_reference for entry in feed.from_index(0)] == ["public.kline.1m#0"]


def test_public_feed_rejects_observations_for_another_symbol():
    feed = PublicMarketDataFeed(
        symbol="ETHUSDT",
        fetch_batch=lambda symbol, after: [
            MarketObservation("BTCUSDT", Decimal("2700"), BASE, "public.kline.1m#0")
        ],
    )

    with pytest.raises(PaperConfigError):
        feed.pull()


def test_market_observation_validation():
    with pytest.raises(PaperConfigError):
        MarketObservation("ETHUSDT", Decimal("0"), BASE, "ref#0")
    with pytest.raises(PaperConfigError):
        MarketObservation("ETHUSDT", Decimal("1"), datetime(2026, 1, 1), "ref#0")
    with pytest.raises(PaperConfigError):
        MarketObservation("ETHUSDT", Decimal("1"), BASE, "")
    with pytest.raises(PaperConfigError):
        MarketObservation("ETHUSDT", Decimal("1"), BASE, "ref#0", volume=Decimal("-1"))
