"""G5b durable account-equity chain reconstruction tests."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import UUID

import pytest

from crypto_trader.events import AccountEquityRecorded, AccountEquityRecordPayload
from crypto_trader.persistence import SQLiteEventJournal
from crypto_trader.production import ProductionStateError, load_equity_chain

UTC = timezone.utc
BASE = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)
SYMBOL = "BTCUSDT"
BASE_ASSET = "BTC"
QUOTE_ASSET = "USDT"
ENV = "binance-spot-testnet"
ACCOUNT = "123456789"
ROOT_ID = UUID("10000000-0000-4000-8000-000000000001")
PEAK_ID = UUID("10000000-0000-4000-8000-000000000002")
REBASE_ID = UUID("10000000-0000-4000-8000-000000000003")


@pytest.fixture
def journal(tmp_path):
    with SQLiteEventJournal(tmp_path / "equity.db") as active:
        yield active


def equity_event(
    *,
    event_id: UUID,
    kind: str,
    equity: str,
    quote_balance: str,
    baseline_id: UUID,
    previous_id: UUID | None,
    seconds: int,
    environment: str = ENV,
    account: str = ACCOUNT,
    fence_term: int | None = None,
) -> AccountEquityRecorded:
    occurred = BASE + timedelta(seconds=seconds)
    return AccountEquityRecorded(
        event_id=event_id,
        occurred_at=occurred,
        correlation_id=UUID("20000000-0000-4000-8000-000000000001"),
        producer="application.production.equity",
        symbol=SYMBOL,
        payload=AccountEquityRecordPayload(
            kind=kind,
            environment=environment,
            venue_account_id=account,
            base_asset=BASE_ASSET,
            quote_asset=QUOTE_ASSET,
            base_balance=Decimal("1"),
            quote_balance=Decimal(quote_balance),
            mark_price=Decimal("30000"),
            mark_reference=f"market:{SYMBOL}:{seconds}",
            mark_event_ts=occurred,
            equity_quote=Decimal(equity),
            peak_equity_quote=Decimal(equity),
            baseline_event_id=baseline_id,
            previous_event_id=previous_id,
            fence_term=max(1, seconds + 1) if fence_term is None else fence_term,
            reason=f"synthetic {kind}",
        ),
    )


def load(journal, *, environment=ENV, account=ACCOUNT):
    return load_equity_chain(
        journal,
        symbol=SYMBOL,
        environment=environment,
        venue_account_id=account,
        base_asset=BASE_ASSET,
        quote_asset=QUOTE_ASSET,
    )


def test_equity_chain_rebuilds_baseline_and_strictly_increasing_peak(journal):
    journal.append(equity_event(
        event_id=ROOT_ID, kind="baseline", equity="31000", quote_balance="1000",
        baseline_id=ROOT_ID, previous_id=None, seconds=0,
    ))
    journal.append(equity_event(
        event_id=PEAK_ID, kind="peak", equity="31500", quote_balance="1500",
        baseline_id=ROOT_ID, previous_id=ROOT_ID, seconds=1,
    ))

    state = load(journal)

    assert state.baseline_event_id == ROOT_ID
    assert state.last_event_id == PEAK_ID
    assert state.last_kind == "peak"
    assert state.peak_equity_quote == Decimal("31500")


def test_equity_chain_refuses_missing_baseline(journal):
    with pytest.raises(ProductionStateError, match="no durable account-equity baseline"):
        load(journal)


def test_equity_chain_refuses_discontinuous_previous_link(journal):
    journal.append(equity_event(
        event_id=ROOT_ID, kind="baseline", equity="31000", quote_balance="1000",
        baseline_id=ROOT_ID, previous_id=None, seconds=0,
    ))
    journal.append(equity_event(
        event_id=PEAK_ID, kind="peak", equity="31500", quote_balance="1500",
        baseline_id=ROOT_ID,
        previous_id=UUID("30000000-0000-4000-8000-000000000099"),
        seconds=1,
    ))
    with pytest.raises(ProductionStateError, match="discontinuous"):
        load(journal)


def test_equity_chain_refuses_second_baseline_without_rebaseline(journal):
    journal.append(equity_event(
        event_id=ROOT_ID, kind="baseline", equity="31000", quote_balance="1000",
        baseline_id=ROOT_ID, previous_id=None, seconds=0,
    ))
    second = UUID("10000000-0000-4000-8000-000000000009")
    journal.append(equity_event(
        event_id=second, kind="baseline", equity="31100", quote_balance="1100",
        baseline_id=second, previous_id=None, seconds=1,
    ))
    with pytest.raises(ProductionStateError, match="second baseline"):
        load(journal)


def test_equity_chain_refuses_non_increasing_peak(journal):
    journal.append(equity_event(
        event_id=ROOT_ID, kind="baseline", equity="31000", quote_balance="1000",
        baseline_id=ROOT_ID, previous_id=None, seconds=0,
    ))
    journal.append(equity_event(
        event_id=PEAK_ID, kind="peak", equity="31000", quote_balance="1000",
        baseline_id=ROOT_ID, previous_id=ROOT_ID, seconds=1,
    ))
    with pytest.raises(ProductionStateError, match="strictly exceed"):
        load(journal)


def test_equity_chain_refuses_scope_change_inside_peak(journal):
    journal.append(equity_event(
        event_id=ROOT_ID, kind="baseline", equity="31000", quote_balance="1000",
        baseline_id=ROOT_ID, previous_id=None, seconds=0,
    ))
    journal.append(equity_event(
        event_id=PEAK_ID, kind="peak", equity="31500", quote_balance="1500",
        baseline_id=ROOT_ID, previous_id=ROOT_ID, seconds=1, account="987654321",
    ))
    with pytest.raises(ProductionStateError, match="immutable chain scope"):
        load(journal)


def test_equity_chain_accepts_explicit_rebaseline_without_changing_scope(journal):
    journal.append(equity_event(
        event_id=ROOT_ID, kind="baseline", equity="31000", quote_balance="1000",
        baseline_id=ROOT_ID, previous_id=None, seconds=0,
    ))
    journal.append(equity_event(
        event_id=PEAK_ID, kind="peak", equity="31500", quote_balance="1500",
        baseline_id=ROOT_ID, previous_id=ROOT_ID, seconds=1,
    ))
    journal.append(equity_event(
        event_id=REBASE_ID, kind="rebaseline", equity="32000", quote_balance="2000",
        baseline_id=REBASE_ID, previous_id=PEAK_ID, seconds=2,
    ))

    state = load(journal)

    assert state.baseline_event_id == REBASE_ID
    assert state.last_event_id == REBASE_ID
    assert state.last_kind == "rebaseline"
    assert state.peak_equity_quote == Decimal("32000")


def test_equity_chain_refuses_active_scope_that_does_not_match_runtime_config(journal):
    journal.append(equity_event(
        event_id=ROOT_ID, kind="baseline", equity="31000", quote_balance="1000",
        baseline_id=ROOT_ID, previous_id=None, seconds=0,
    ))
    with pytest.raises(ProductionStateError, match="configured account/instrument scope"):
        load(journal, account="987654321")


def test_equity_chain_refuses_rebaseline_scope_change(journal):
    journal.append(equity_event(
        event_id=ROOT_ID, kind="baseline", equity="31000", quote_balance="1000",
        baseline_id=ROOT_ID, previous_id=None, seconds=0,
    ))
    journal.append(equity_event(
        event_id=REBASE_ID, kind="rebaseline", equity="32000", quote_balance="2000",
        baseline_id=REBASE_ID, previous_id=ROOT_ID, seconds=1, account="987654321",
    ))
    with pytest.raises(ProductionStateError, match="immutable chain scope"):
        load(journal)


def test_equity_chain_accepts_rebaseline_that_resets_high_water_mark_lower(journal):
    journal.append(equity_event(
        event_id=ROOT_ID, kind="baseline", equity="32000", quote_balance="2000",
        baseline_id=ROOT_ID, previous_id=None, seconds=0,
    ))
    journal.append(equity_event(
        event_id=REBASE_ID, kind="rebaseline", equity="31000", quote_balance="1000",
        baseline_id=REBASE_ID, previous_id=ROOT_ID, seconds=1,
    ))

    state = load(journal)

    assert state.baseline_event_id == REBASE_ID
    assert state.peak_equity_quote == Decimal("31000")


def test_equity_chain_refuses_fence_term_regression(journal):
    journal.append(equity_event(
        event_id=ROOT_ID, kind="baseline", equity="31000", quote_balance="1000",
        baseline_id=ROOT_ID, previous_id=None, seconds=0, fence_term=5,
    ))
    journal.append(equity_event(
        event_id=PEAK_ID, kind="peak", equity="31500", quote_balance="1500",
        baseline_id=ROOT_ID, previous_id=ROOT_ID, seconds=1, fence_term=4,
    ))
    with pytest.raises(ProductionStateError, match="fence term regressed"):
        load(journal)


def test_equity_chain_refuses_peak_before_baseline(journal):
    journal.append(equity_event(
        event_id=PEAK_ID, kind="peak", equity="31500", quote_balance="1500",
        baseline_id=ROOT_ID, previous_id=ROOT_ID, seconds=1,
    ))
    with pytest.raises(ProductionStateError, match="before a durable baseline"):
        load(journal)


def test_equity_chain_refuses_peak_that_changes_baseline_identity(journal):
    journal.append(equity_event(
        event_id=ROOT_ID, kind="baseline", equity="31000", quote_balance="1000",
        baseline_id=ROOT_ID, previous_id=None, seconds=0,
    ))
    other_baseline = UUID("10000000-0000-4000-8000-000000000099")
    journal.append(equity_event(
        event_id=PEAK_ID, kind="peak", equity="31500", quote_balance="1500",
        baseline_id=other_baseline, previous_id=ROOT_ID, seconds=1,
    ))
    with pytest.raises(ProductionStateError, match="changed baseline identity"):
        load(journal)


def test_equity_chain_reloads_after_real_journal_reopen(tmp_path):
    path = tmp_path / "equity-restart.db"
    with SQLiteEventJournal(path) as first:
        first.append(equity_event(
            event_id=ROOT_ID, kind="baseline", equity="31000", quote_balance="1000",
            baseline_id=ROOT_ID, previous_id=None, seconds=0,
        ))
        first.append(equity_event(
            event_id=PEAK_ID, kind="peak", equity="31500", quote_balance="1500",
            baseline_id=ROOT_ID, previous_id=ROOT_ID, seconds=1,
        ))

    with SQLiteEventJournal(path) as reopened:
        state = load(reopened)

    assert state.baseline_event_id == ROOT_ID
    assert state.last_event_id == PEAK_ID
    assert state.peak_equity_quote == Decimal("31500")
    assert state.fence_term == 2


def test_equity_chain_refuses_time_regression_even_if_source_returns_insertion_order():
    root = equity_event(
        event_id=ROOT_ID, kind="baseline", equity="31000", quote_balance="1000",
        baseline_id=ROOT_ID, previous_id=None, seconds=0, fence_term=5,
    )
    older_peak = equity_event(
        event_id=PEAK_ID, kind="peak", equity="31500", quote_balance="1500",
        baseline_id=ROOT_ID, previous_id=ROOT_ID, seconds=-1, fence_term=5,
    )

    class InsertionOrderJournal:
        def read_range(self):
            return [root, older_peak]

    with pytest.raises(ProductionStateError, match="time moved backwards"):
        load(InsertionOrderJournal())


def test_peak_after_rebaseline_uses_new_baseline_identity(journal):
    next_peak = UUID("10000000-0000-4000-8000-000000000004")
    journal.append(equity_event(
        event_id=ROOT_ID, kind="baseline", equity="32000", quote_balance="2000",
        baseline_id=ROOT_ID, previous_id=None, seconds=0,
    ))
    journal.append(equity_event(
        event_id=REBASE_ID, kind="rebaseline", equity="31000", quote_balance="1000",
        baseline_id=REBASE_ID, previous_id=ROOT_ID, seconds=1,
    ))
    journal.append(equity_event(
        event_id=next_peak, kind="peak", equity="31500", quote_balance="1500",
        baseline_id=REBASE_ID, previous_id=REBASE_ID, seconds=2,
    ))

    state = load(journal)

    assert state.baseline_event_id == REBASE_ID
    assert state.last_event_id == next_peak
    assert state.peak_equity_quote == Decimal("31500")


def test_peak_preserves_original_active_root_accounting_inputs(journal):
    journal.append(equity_event(
        event_id=ROOT_ID, kind="baseline", equity="31000", quote_balance="1000",
        baseline_id=ROOT_ID, previous_id=None, seconds=0,
    ))
    journal.append(equity_event(
        event_id=PEAK_ID, kind="peak", equity="31500", quote_balance="1500",
        baseline_id=ROOT_ID, previous_id=ROOT_ID, seconds=1,
    ))

    state = load(journal)

    assert state.root_occurred_at == BASE
    assert state.root_base_balance == Decimal("1")
    assert state.root_quote_balance == Decimal("1000")
    assert state.root_mark_price == Decimal("30000")
    assert state.root_mark_reference == f"market:{SYMBOL}:0"


def test_rebaseline_becomes_new_active_root_accounting_inputs(journal):
    journal.append(equity_event(
        event_id=ROOT_ID, kind="baseline", equity="32000", quote_balance="2000",
        baseline_id=ROOT_ID, previous_id=None, seconds=0,
    ))
    journal.append(equity_event(
        event_id=REBASE_ID, kind="rebaseline", equity="31000", quote_balance="1000",
        baseline_id=REBASE_ID, previous_id=ROOT_ID, seconds=2,
    ))

    state = load(journal)

    assert state.root_occurred_at == BASE + timedelta(seconds=2)
    assert state.root_base_balance == Decimal("1")
    assert state.root_quote_balance == Decimal("1000")
    assert state.root_mark_reference == f"market:{SYMBOL}:2"
