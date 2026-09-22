"""Phase 15: the locked pilot envelope, price rules, FOK policy and documented limitations."""

from __future__ import annotations

from decimal import Decimal

import pytest

from crypto_trader.execution import ExecutionSide
from crypto_trader.mainnet import (
    CANARY_ENTRY_ORDER_TYPE,
    CANARY_ENTRY_TIME_IN_FORCE,
    DRY_RUN_RECONCILIATION_GAP,
    EXECUTION_PRICE_RULES,
    FIRST_CANARY_TRADES_REVIEW_CHECKPOINT,
    FOK_DOCUMENTED_LIMITATIONS,
    INSUFFICIENT_EXECUTION_SAMPLE,
    LOCKED_PILOT_ENVELOPE,
    MIN_FOK_FILLS_FOR_OWNER_REVIEW,
    RESIDUAL_LIMITATIONS,
    CanaryPolicyError,
    CanaryEntryOrder,
    EnvelopeActivation,
    LockedEnvelopeError,
    NoChasingPolicy,
    OldSnapshotReuseError,
    absolute_bound_price,
    assert_envelope_is_reference_only,
    build_canary_entry,
    classify_execution_sample,
    documented_limitations,
    envelope_record,
    fill_slippage_record,
    realized_slippage_bps,
    require_admissible_spread,
    require_within_absolute_bound,
    spread_bps,
)


# --- the locked envelope ---------------------------------------------------

def test_the_locked_envelope_carries_the_owner_approved_values_reference_only():
    envelope = LOCKED_PILOT_ENVELOPE.to_record()

    assert envelope["capital_envelope_sar"] == "300"
    assert envelope["max_order_value_sar"] == "50"
    assert envelope["max_open_exposure_sar"] == "150"
    assert envelope["daily_loss_halt_sar"] == "15"
    assert envelope["max_open_orders"] == 2
    assert envelope["symbols"] == ["BTCUSDT", "ETHUSDT"]
    assert envelope["venue_scope"] == "SPOT"
    assert envelope["margin_allowed"] is False
    assert envelope["futures_allowed"] is False
    assert envelope["leverage_allowed"] is False
    assert envelope["borrowing_allowed"] is False
    assert envelope["withdrawals_allowed"] is False
    assert envelope["activation"] == EnvelopeActivation.REFERENCE_ONLY.value
    assert envelope["permission_to_trade"] is False


def test_the_envelope_is_reference_only_and_is_not_trading_permission():
    assert_envelope_is_reference_only()
    record = envelope_record()
    assert record["permission_to_trade"] is False
    assert "no order may be placed" in str(record["note"])
    # The envelope also refuses to be constructed with an out-of-scope capability.
    from crypto_trader.mainnet import LockedPilotEnvelope

    with pytest.raises(LockedEnvelopeError):
        LockedPilotEnvelope(
            capital_envelope_sar=Decimal("300"),
            max_order_value_sar=Decimal("50"),
            max_open_exposure_sar=Decimal("150"),
            daily_loss_halt_sar=Decimal("15"),
            max_open_orders=2,
            symbols=("BTCUSDT",),
            margin_allowed=True,
        )


# --- price rules -----------------------------------------------------------

def test_the_price_rules_are_the_owner_approved_values():
    record = EXECUTION_PRICE_RULES.to_record()

    assert record["max_spread_bps"] == "30"
    assert record["max_normal_slippage_bps"] == "15"
    assert record["absolute_price_bound_bps"] == "50"
    assert any("not an expectation" in note for note in record["notes"])


def test_spread_is_a_top_of_book_admission_check():
    assert spread_bps(Decimal("26999.99"), Decimal("27000.01")) < Decimal("1")
    assert require_admissible_spread(Decimal("26999.99"), Decimal("27000.01")) >= 0
    with pytest.raises(CanaryPolicyError):
        require_admissible_spread(Decimal("26000"), Decimal("27000"))
    with pytest.raises(CanaryPolicyError):
        spread_bps(Decimal("27000.00"), Decimal("26999.00"))


def test_the_absolute_bound_is_the_catastrophic_execution_ceiling():
    mid = Decimal("27000.00")
    upper = absolute_bound_price(mid, ExecutionSide.BUY)
    lower = absolute_bound_price(mid, ExecutionSide.SELL)

    assert upper == Decimal("27135.0000")
    assert lower == Decimal("26865.0000")
    require_within_absolute_bound(mid, Decimal("27000.00"), ExecutionSide.BUY)
    with pytest.raises(CanaryPolicyError):
        require_within_absolute_bound(mid, Decimal("27200.00"), ExecutionSide.BUY)
    with pytest.raises(CanaryPolicyError):
        require_within_absolute_bound(mid, Decimal("26800.00"), ExecutionSide.SELL)


# --- the FOK canary entry policy -------------------------------------------

def test_the_only_canary_entry_shape_is_a_price_bounded_limit_fok():
    entry = build_canary_entry(
        symbol="BTCUSDT",
        side=ExecutionSide.BUY,
        quantity=Decimal("0.00100"),
        limit_price=Decimal("27000.00"),
        reference_mid=Decimal("27000.00"),
        market_snapshot_id="snap-1",
        order_type="limit",
        time_in_force="fok",
    )

    assert isinstance(entry, CanaryEntryOrder)
    assert entry.order_type == CANARY_ENTRY_ORDER_TYPE == "LIMIT"
    assert entry.time_in_force == CANARY_ENTRY_TIME_IN_FORCE == "FOK"


@pytest.mark.parametrize(
    "order_type,time_in_force",
    [("MARKET", "FOK"), ("LIMIT", "IOC"), ("LIMIT", "GTC"), ("LIMIT", "GTX")],
)
def test_any_other_entry_type_or_time_in_force_is_refused(order_type: str, time_in_force: str):
    with pytest.raises(CanaryPolicyError):
        build_canary_entry(
            symbol="BTCUSDT",
            side=ExecutionSide.BUY,
            quantity=Decimal("0.00100"),
            limit_price=Decimal("27000.00"),
            reference_mid=Decimal("27000.00"),
            market_snapshot_id="snap-1",
            order_type=order_type,
            time_in_force=time_in_force,
        )


def test_a_price_beyond_the_absolute_bound_is_refused_at_entry_construction():
    with pytest.raises(CanaryPolicyError):
        build_canary_entry(
            symbol="BTCUSDT",
            side=ExecutionSide.BUY,
            quantity=Decimal("0.00100"),
            limit_price=Decimal("27200.00"),
            reference_mid=Decimal("27000.00"),
            market_snapshot_id="snap-1",
            order_type="LIMIT",
            time_in_force="FOK",
        )


# --- no chasing / single-use snapshots -------------------------------------

def test_a_market_snapshot_can_never_be_reused():
    policy = NoChasingPolicy()

    assert policy.no_chasing_enabled is True
    policy.consume("snap-1")
    with pytest.raises(OldSnapshotReuseError):
        policy.consume("snap-1")
    policy.consume("snap-2")
    assert policy.consumed == frozenset({"snap-1", "snap-2"})
    with pytest.raises(CanaryPolicyError):
        policy.consume("")


# --- fill records / sample classification ----------------------------------

def test_every_future_fill_can_record_the_four_required_values():
    record = fill_slippage_record(
        reference_mid=Decimal("27000.00"),
        limit_price=Decimal("27010.00"),
        average_fill_price=Decimal("27005.00"),
        side=ExecutionSide.BUY,
    ).to_record()

    assert set(record) >= {
        "reference_mid",
        "limit_price",
        "average_fill_price",
        "realized_slippage_bps",
    }
    assert Decimal(record["realized_slippage_bps"]) > 0
    # A favourable buy fill records negative (adverse-positive) slippage.
    assert realized_slippage_bps(
        Decimal("27000.00"), Decimal("26990.00"), ExecutionSide.BUY
    ) < 0
    assert realized_slippage_bps(
        Decimal("27000.00"), Decimal("26990.00"), ExecutionSide.SELL
    ) > 0


def test_the_future_canary_sample_rule_classifies_without_widening_anything():
    assert MIN_FOK_FILLS_FOR_OWNER_REVIEW == 5
    assert FIRST_CANARY_TRADES_REVIEW_CHECKPOINT == 10
    assert classify_execution_sample(0) == INSUFFICIENT_EXECUTION_SAMPLE
    assert classify_execution_sample(4) == INSUFFICIENT_EXECUTION_SAMPLE
    assert classify_execution_sample(5) != INSUFFICIENT_EXECUTION_SAMPLE
    with pytest.raises(CanaryPolicyError):
        classify_execution_sample(-1)


# --- documented limitations ------------------------------------------------

def test_every_required_limitation_is_documented_and_not_claimed_solved():
    record = documented_limitations()
    joined = " ".join(
        [
            *record["fok_limitations"],
            record["dry_run_reconciliation_gap"],
            *record["residual_limitations"],
        ]
    ).lower()

    for phrase in (
        "not the permanent optimal execution strategy",
        "opportunity cost",
        "does not eliminate transport ambiguity",
        "multiple price levels",
        "maximum allowed normal execution deviation",
        "documented gap",
        "toctou",
        "dynamic latency",
        "hidden/iceberg",
        "ioc/gtc",
        "controlled live canary",
    ):
        assert phrase in joined, f"the limitation {phrase!r} is not documented"
    assert len(FOK_DOCUMENTED_LIMITATIONS) >= 6
    assert DRY_RUN_RECONCILIATION_GAP in RESIDUAL_LIMITATIONS
