"""Phase 6: deterministic validity/staleness gate tests."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

import pytest

from crypto_trader.validity import (
    DecisionValidityGate,
    Disposition,
    LiveMarketStateProvider,
    MarketObservation,
    MarketStateError,
    ReplayMarketStateProvider,
    ValidityConfigError,
    ValidityPolicy,
    ValidityRequest,
    load_policy,
)

BASE = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
POLICY = ValidityPolicy(policy_id="strat-1-v3", max_age_seconds=5.0, max_market_move_bps=Decimal("25"))


def observation(offset_s: float, price: str, *, reference: str = "obs-1") -> MarketObservation:
    return MarketObservation(
        symbol="ETHUSDT",
        price=Decimal(price),
        market_event_ts=BASE + timedelta(seconds=offset_s),
        local_ingest_ts=BASE + timedelta(seconds=offset_s + 0.01),
        market_reference=reference,
    )


def request(*, completed_offset_s: float = 1.0, market_offset_s: float = 0.0, price: str = "2700.00",
            valid_until: datetime | None = None) -> ValidityRequest:
    return ValidityRequest(
        correlation_id=uuid4(),
        symbol="ETHUSDT",
        decision_started_at=BASE + timedelta(seconds=market_offset_s),
        decision_completed_at=BASE + timedelta(seconds=completed_offset_s),
        market_event_ts=BASE + timedelta(seconds=market_offset_s),
        reference_price=Decimal(price),
        market_reference="obs-1",
        valid_until=valid_until,
    )


def gate(policy: ValidityPolicy = POLICY) -> DecisionValidityGate:
    return DecisionValidityGate(policy)


# --- policy loading (fail closed) ---

def test_policy_loads_from_explicit_configuration():
    policy = load_policy({"policy_id": "p1", "max_age_seconds": 3, "max_market_move_bps": "15"})

    assert policy.policy_id == "p1"
    assert policy.max_age_seconds == 3.0
    assert policy.max_market_move_bps == Decimal("15")
    assert policy.valid_until_seconds is None


@pytest.mark.parametrize(
    "config",
    [
        {"max_age_seconds": 3, "max_market_move_bps": "15"},            # missing policy_id
        {"policy_id": "p", "max_market_move_bps": "15"},                 # missing max_age
        {"policy_id": "p", "max_age_seconds": 3},                        # missing move tolerance
        {"policy_id": "p", "max_age_seconds": 0, "max_market_move_bps": "15"},
        {"policy_id": "p", "max_age_seconds": "abc", "max_market_move_bps": "15"},
        {"policy_id": "p", "max_age_seconds": 3, "max_market_move_bps": "0"},
        {"policy_id": "p", "max_age_seconds": 3, "max_market_move_bps": "15", "typo_key": 1},
    ],
)
def test_policy_configuration_fails_closed(config):
    with pytest.raises(ValidityConfigError):
        load_policy(config)




@pytest.mark.parametrize(
    "config",
    [
        {"policy_id": "p", "max_age_seconds": "inf", "max_market_move_bps": "15"},
        {"policy_id": "p", "max_age_seconds": "nan", "max_market_move_bps": "15"},
        {"policy_id": "p", "max_age_seconds": 3, "max_market_move_bps": "Infinity"},
        {"policy_id": "p", "max_age_seconds": 3, "max_market_move_bps": "NaN"},
        {
            "policy_id": "p",
            "max_age_seconds": 3,
            "max_market_move_bps": "15",
            "valid_until_seconds": "inf",
        },
        {
            "policy_id": "p",
            "max_age_seconds": 3,
            "max_market_move_bps": "15",
            "valid_until_seconds": "nan",
        },
    ],
)
def test_non_finite_validity_thresholds_fail_closed(config):
    with pytest.raises(ValidityConfigError):
        load_policy(config)

def test_gate_requires_a_resolved_policy():
    with pytest.raises(ValidityConfigError):
        DecisionValidityGate(None)  # type: ignore[arg-type]


# --- dispositions ---

def test_accepts_a_fresh_intent_with_small_move():
    decision = gate().evaluate(
        request(completed_offset_s=2.0),
        market_state=LiveMarketStateProvider(lambda symbol: observation(2.0, "2700.50")),
        now=BASE + timedelta(seconds=3),
    )

    assert decision.disposition is Disposition.ACCEPTED
    assert decision.reason_code == "within_policy"
    assert decision.policy_id == "strat-1-v3"
    assert decision.market_move_bps is not None and decision.market_move_bps < 25


def test_expired_when_explicit_deadline_passed():
    decision = gate().evaluate(
        request(completed_offset_s=1.0, valid_until=BASE + timedelta(seconds=2)),
        market_state=LiveMarketStateProvider(lambda symbol: observation(1.0, "2700.00")),
        now=BASE + timedelta(seconds=3),
    )

    assert decision.disposition is Disposition.EXPIRED
    assert decision.reason_code == "valid_until_exceeded"


def test_stale_age_is_measured_from_the_original_market_observation_not_the_decision():
    """A slow model does not reset freshness: the 6s old observation is what counts."""
    slow_request = request(completed_offset_s=6.0, market_offset_s=0.0)

    decision = gate().evaluate(
        slow_request,
        market_state=LiveMarketStateProvider(lambda symbol: observation(6.0, "2700.00")),
        now=BASE + timedelta(seconds=6),
    )

    assert decision.disposition is Disposition.STALE
    assert decision.reason_code == "max_age_exceeded"
    assert decision.decision_age_seconds == pytest.approx(6.0)
    assert decision.inputs["decision_completed_at"] == slow_request.decision_completed_at.isoformat()


def test_market_moved_when_time_valid_but_reference_price_moved_beyond_tolerance():
    decision = gate().evaluate(
        request(completed_offset_s=1.0),
        # 2700.00 -> 2710.00 = 10/2700 = ~37.04 bps, above the 25 bps tolerance.
        market_state=LiveMarketStateProvider(lambda symbol: observation(1.0, "2710.00")),
        now=BASE + timedelta(seconds=2),
    )

    assert decision.disposition is Disposition.MARKET_MOVED
    assert decision.reason_code == "market_move_exceeded"
    assert decision.market_move_bps == pytest.approx(37.037, rel=1e-3)


def test_superseded_when_a_newer_intent_exists_for_the_scope():
    newest = uuid4()
    decision = gate().evaluate(
        request(completed_offset_s=1.0),
        market_state=LiveMarketStateProvider(lambda symbol: observation(1.0, "2700.00")),
        now=BASE + timedelta(seconds=2),
        latest_scope_intent_id=newest,
    )

    assert decision.disposition is Disposition.SUPERSEDED
    assert decision.reason_code == "newer_intent_for_scope"


def test_backpressure_rejected_when_bounded_capacity_is_exhausted():
    decision = gate().evaluate(
        request(completed_offset_s=1.0),
        market_state=LiveMarketStateProvider(lambda symbol: observation(1.0, "2700.00")),
        now=BASE + timedelta(seconds=2),
        capacity_available=False,
    )

    assert decision.disposition is Disposition.BACKPRESSURE_REJECTED
    assert decision.reason_code == "bounded_capacity_exhausted"


def test_missing_market_state_fails_closed_as_stale():
    decision = gate().evaluate(
        request(completed_offset_s=1.0),
        market_state=LiveMarketStateProvider(lambda symbol: None),
        now=BASE + timedelta(seconds=2),
    )

    assert decision.disposition is Disposition.STALE
    assert decision.reason_code == "market_state_unavailable"


# --- precedence ---

def test_expired_takes_precedence_over_market_moved_and_superseded():
    decision = gate().evaluate(
        request(completed_offset_s=1.0, valid_until=BASE + timedelta(seconds=1)),
        market_state=LiveMarketStateProvider(lambda symbol: observation(1.0, "2800.00")),
        now=BASE + timedelta(seconds=2),
        latest_scope_intent_id=uuid4(),
        capacity_available=False,
    )

    assert decision.disposition is Disposition.EXPIRED


def test_stale_takes_precedence_over_market_moved():
    decision = gate().evaluate(
        request(completed_offset_s=9.0, market_offset_s=0.0),
        market_state=LiveMarketStateProvider(lambda symbol: observation(9.0, "2800.00")),
        now=BASE + timedelta(seconds=9),
    )

    assert decision.disposition is Disposition.STALE


def test_market_moved_takes_precedence_over_superseded_and_backpressure():
    decision = gate().evaluate(
        request(completed_offset_s=1.0),
        market_state=LiveMarketStateProvider(lambda symbol: observation(1.0, "2800.00")),
        now=BASE + timedelta(seconds=2),
        latest_scope_intent_id=uuid4(),
        capacity_available=False,
    )

    assert decision.disposition is Disposition.MARKET_MOVED


# --- determinism / replay ---

def test_same_inputs_and_history_produce_the_same_disposition():
    history = [observation(0.0, "2700.00"), observation(1.0, "2701.50")]
    first = ReplayMarketStateProvider(history)
    second = ReplayMarketStateProvider(list(history))
    intent = request(completed_offset_s=1.0, price="2700.00")

    decision_a = gate().evaluate(intent, market_state=first, now=BASE + timedelta(seconds=2))
    decision_b = gate().evaluate(intent, market_state=second, now=BASE + timedelta(seconds=2))

    assert decision_a.disposition is decision_b.disposition
    assert decision_a.reason_code == decision_b.reason_code
    assert decision_a.inputs == decision_b.inputs


def test_replay_provider_advances_deterministically():
    provider = ReplayMarketStateProvider(
        [
            observation(0.0, "2700.00", reference="obs-0"),
            observation(1.0, "2701.00", reference="obs-1"),
        ]
    )

    assert provider.latest("ETHUSDT").market_reference == "obs-0"
    assert provider.advance("ETHUSDT").market_reference == "obs-1"
    assert provider.latest("ETHUSDT").market_reference == "obs-1"
    assert provider.advance("ETHUSDT") is None
    assert provider.latest("ETHUSDT") is None
    assert provider.advance("ETHUSDT") is None
    assert provider.latest("MISSING") is None


def test_naive_timestamps_are_rejected():
    with pytest.raises(ValidityConfigError):
        request().__class__(
            correlation_id=uuid4(),
            symbol="ETHUSDT",
            decision_started_at=datetime(2026, 1, 1, 12, 0, 0),  # naive
            decision_completed_at=BASE,
            market_event_ts=BASE,
            reference_price=Decimal("2700.00"),
            market_reference="obs-1",
        )


def test_market_observation_validation():
    with pytest.raises(MarketStateError):
        MarketObservation(
            symbol="ETHUSDT",
            price=Decimal("0"),
            market_event_ts=BASE,
            local_ingest_ts=BASE,
            market_reference="obs-1",
        )

    with pytest.raises(MarketStateError):
        MarketObservation(
            symbol="ETHUSDT",
            price=Decimal("1"),
            market_event_ts=datetime(2026, 1, 1),  # naive
            local_ingest_ts=BASE,
            market_reference="obs-1",
        )

def test_a_future_market_event_ts_is_rejected_as_stale_not_accepted():
    """A market_event_ts ahead of `now` (clock skew or a bad producer) fails closed."""
    future_request = request(market_offset_s=5.0, completed_offset_s=5.0)

    decision = gate().evaluate(
        future_request,
        market_state=LiveMarketStateProvider(lambda symbol: None),
        now=BASE,
    )

    assert decision.disposition is Disposition.STALE
    assert decision.reason_code == "market_event_ts_in_future"
    assert decision.decision_age_seconds < 0

