"""Attack suite for the two execution-evidence guards (write guard, read guard). Offline.

Every scenario goes through ProductionAdmissionController.run_once, a restart on the same durable
journal, and a NEW intent. The safety contract (SAFE) checked after the restart:
  a second POST is allowed only when the first order's execution is either zero (nothing executed)
  or proved by fills bound to our order in the journal (our write answer, or trade backfill).
Balances are scripted to MATCH THE JOURNAL (the dangerous shape), unless a case says otherwise.
"""
from __future__ import annotations

import json
from datetime import timedelta
from decimal import Decimal
from uuid import UUID

import pytest

from crypto_trader.events import EXECUTION_FILL_EVENT_TYPE, ORDER_STATE_EVENT_TYPE
from crypto_trader.execution.binance_testnet import TestnetOrder
from crypto_trader.execution.binance_testnet.translate import (
    deterministic_order_uuid,
    testnet_order_to_phase3_events as build_events,
)
from crypto_trader.execution.ports import OrderLifecycleState
from crypto_trader.recovery import reconstruct_order_states
from crypto_trader.tests import testnet_fixtures as fx
from crypto_trader.tests.production_fixtures import (
    BASE,
    bind_test_admission,
    build_runtime,
    canonical_risk_refresh_responses,
    control_for,
    intent,
    seed_canonical_equity_root,
    startup_responses,
)
from crypto_trader.tests.test_testnet_durable_submission import _r03_journal
from crypto_trader.production import ProductionRiskStateSource
from crypto_trader.tests.production_fixtures import INSTRUMENT, clock as fixture_clock
from crypto_trader.execution.binance_testnet import TestnetOrderRuntime as OrderRuntime

WRITE_TIME_MS = int(BASE.timestamp() * 1000) + 1000

def _account(base="0", quote="100000"):
    response = fx.account_response()
    for balance in response.body["balances"]:
        if balance["asset"] == "BTC":
            balance.update(free=base, locked="0")
        elif balance["asset"] == "USDT":
            balance.update(free=quote, locked="0")
    return response

def _risk_reads(base="0", quote="100000", *, open_orders=()):
    return [fx.time_response(fx.SERVER_TIME_MS + 1),
            fx.open_orders_response(*open_orders),
            _account(base=base, quote=quote),
            fx.price_response("27000.00"),
            fx.open_orders_response(*open_orders)]

def _risk_source(journal, control, client):
    return ProductionRiskStateSource(
        journal=journal, control=control, client=client,
        instrument=INSTRUMENT, environment="binance-spot-testnet",
        venue_account_id="123456789", max_account_age_seconds=13*60*60,
        max_mark_age_seconds=13*60*60)

def _port(client, journal):
    return OrderRuntime(client=client, instrument=INSTRUMENT, journal=journal,
                        clock=fixture_clock(BASE))


TERMINAL = {"filled", "canceled", "expired", "rejected"}


def answer(reference, *, status, executed="0", fills=None, order_id=4242, when=WRITE_TIME_MS, drop_fills=False):
    reply = fx.order_response(reference, status=status, order_id=order_id, executed_qty=executed,
                              cummulative_quote=str(Decimal(executed) * Decimal("27000.00")),
                              fills=tuple(fills or ()), transact_time=when)
    if drop_fills:
        reply.body.pop("fills", None)
    return reply


def fill(qty, trade_id=901):
    return fx.fill(qty=qty, commission="0", asset="USDT", trade_id=trade_id)


def cancel_answer(reference, *, executed):
    reply = answer(reference, status="CANCELED", executed=executed, drop_fills=True, when=WRITE_TIME_MS + 500)
    reply.body["origClientOrderId"] = reference
    reply.body["clientOrderId"] = "venue-cancel-id"
    return reply


def run(tmp_path, *, first, after=None, legacy=None, venue_view=None, base="0", open_orders=False, measure_cache=False):
    """first(reference) -> venue replies after the risk refresh; after(port, transport, reference)."""
    one = intent("guard-first", quantity=Decimal("0.00100"))
    reference = f"ct-{one.intent_id.hex[:20]}"
    path = tmp_path / "guard.db"
    with _r03_journal(path) as journal:
        seed_canonical_equity_root(journal)
    out = {}
    with _r03_journal(path) as journal:
        transport = fx.ScriptedTransport([*startup_responses(), *canonical_risk_refresh_responses(),
                                          *first(reference)])
        client = fx.client(transport)
        control = control_for(journal, tmp_path / "state")
        port = _port(client, journal)
        runtime = build_runtime(journal=journal, state_dir=tmp_path / "state", execution=port, control=control,
                                risk_state_source=_risk_source(journal, control, client))
        admission = bind_test_admission(runtime)
        runtime.start()
        assert admission.offer(one) == (True, "accepted")
        try:
            outcome = admission.run_once()
            out["first"] = "submitted" if outcome is not None and outcome.submitted else repr(outcome)
        except Exception as exc:  # noqa: BLE001
            out["first"] = type(exc).__name__
        if after is not None:
            try:
                after(port, transport, reference)
                out["after"] = "ok"
            except Exception as exc:  # noqa: BLE001
                out["after"] = type(exc).__name__
        if legacy is not None:
            for event in legacy(reference):
                journal.append(event)
        out["journal"] = [e.event_type.split(".")[-2] + ":" + (getattr(e.payload, "state", "") or "")
                          for e in journal.read_range()
                          if e.order_id == deterministic_order_uuid(reference)
                          and e.event_type in (ORDER_STATE_EVENT_TYPE, EXECUTION_FILL_EVENT_TYPE)]
        local = reconstruct_order_states(journal)[0].state_for(reference)
        out["rebuilt"] = None if local is None else [local.state.value, str(local.filled_quantity)]
        runtime.stop()

    now = BASE + timedelta(seconds=2)
    two = intent("guard-new", decision_started_at=now, decision_completed_at=now, market_event_ts=now,
                 market_reference="guard-new-snapshot")
    listed = (reference,) if open_orders else ()
    replies = [fx.time_response(), fx.open_orders_response(*listed)]
    if out["rebuilt"] is None or out["rebuilt"][0] not in TERMINAL:
        replies.append(venue_view(reference) if venue_view else fx.rejected(-2013))
    quote = str(Decimal("100000") - Decimal(base) * Decimal("27000"))
    risk_reads = list(_risk_reads(base=base, quote=quote, open_orders=listed))
    # Isolated trial: the candidate performs a strict signed terminal-order read at risk refresh.
    if out["rebuilt"] is not None and out["rebuilt"][0] in {"canceled", "expired", "rejected"}:
        terminal_status, terminal_filled = out["rebuilt"]
        confirmed = (venue_view(reference) if venue_view else
                     answer(reference, status=terminal_status.upper(),
                            executed=terminal_filled, when=WRITE_TIME_MS + 1000))
        risk_reads.insert(1, confirmed)
    replies += [*fx.environment_responses(), *risk_reads,
                answer(f"ct-{two.intent_id.hex[:20]}", status="NEW", order_id=4243, when=WRITE_TIME_MS + 2000)]
    with _r03_journal(path) as journal:
        transport = fx.ScriptedTransport(replies)
        client = fx.client(transport)
        control = control_for(journal, tmp_path / "state", when=now)
        port = _port(client, journal)
        runtime = build_runtime(journal=journal, state_dir=tmp_path / "state", execution=port, control=control,
                                risk_state_source=_risk_source(journal, control, client), when=now)
        admission = bind_test_admission(runtime, when=now)
        try:
            runtime.start()
            out["restart"] = "running"
        except Exception as exc:  # noqa: BLE001
            out["restart"] = f"{type(exc).__name__}: {str(exc)[:240]}"
        admission.offer(two)
        try:
            outcome = admission.run_once()
            out["next"] = "submitted" if outcome is not None and outcome.submitted else repr(outcome)
        except Exception as exc:  # noqa: BLE001
            out["next"] = f"{type(exc).__name__}: {str(exc)[:150]}"
            if exc.__cause__ is not None:
                out["next_cause"] = f"{type(exc.__cause__).__name__}: {str(exc.__cause__)[:200]}"
        out["second_POST"] = sum(r.method == "POST" for r in transport.requests)
        if measure_cache and out["next"] == "submitted":
            # After one signed proof, the SAME unchanged terminal must not cause another GET.
            source = runtime._risk_state_provider
            captured = tuple(journal.read_range())
            chain = source._chain(captured)
            reads = []
            saved_method = client.startup_order_status
            def refuse_repeated_read(*args, **kwargs):
                reads.append((args, kwargs))
                raise AssertionError("same terminal was looked up twice")
            client.startup_order_status = refuse_repeated_read
            try:
                source._check_terminal_execution_evidence(captured, chain)
            finally:
                client.startup_order_status = saved_method
            out["cache_re_reads"] = len(reads)
        runtime.stop()
    print("GUARD " + json.dumps(out))
    return out


def duplicate_then(view):
    return lambda reference: [fx.rejected(code=-2010, msg="Duplicate order sent."), view(reference)]


def legacy_state(state, executed):
    def build(reference):
        order = TestnetOrder(symbol="BTCUSDT", client_order_id=reference, venue_order_id="4242",
                             state=state, status=state.value.upper(), original_quantity=Decimal("0.001"),
                             executed_quantity=Decimal(executed),
                             cummulative_quote_quantity=Decimal(executed) * Decimal("27000"),
                             transact_time=BASE + timedelta(milliseconds=1500))
        return build_events(order, correlation_id=UUID(int=7))
    return build


# --- attacks: an execution claimed without its fills must never admit new risk ------------------

ATTACKS = {
    "adoption_filled_without_fills": dict(
        first=duplicate_then(lambda r: answer(r, status="FILLED", executed="0.00100000", drop_fills=True)),
        venue_view=lambda r: answer(r, status="FILLED", executed="0.00100000", drop_fills=True)),
    "adoption_canceled_partial_without_fills": dict(
        first=duplicate_then(lambda r: answer(r, status="CANCELED", executed="0.00050000", drop_fills=True)),
        venue_view=lambda r: answer(r, status="CANCELED", executed="0.00050000", drop_fills=True)),
    "refresh_filled_without_fills": dict(
        first=lambda r: [answer(r, status="NEW")],
        after=lambda port, transport, r: (transport.responses.append(
            answer(r, status="FILLED", executed="0.00100000", drop_fills=True, when=WRITE_TIME_MS + 500)),
            port.refresh(r)),
        venue_view=lambda r: answer(r, status="FILLED", executed="0.00100000", drop_fills=True)),
    "cancel_after_a_partial_fill_without_fills": dict(
        first=lambda r: [answer(r, status="NEW")],
        after=lambda port, transport, r: (transport.responses.append(cancel_answer(r, executed="0.00050000")),
                                          port.cancel(r)),
        venue_view=lambda r: answer(r, status="CANCELED", executed="0.00050000", drop_fills=True)),
    "write_partial_then_refresh_filled_without_the_rest": dict(
        first=lambda r: [answer(r, status="PARTIALLY_FILLED", executed="0.00050000", fills=[fill("0.00050000")])],
        after=lambda port, transport, r: (transport.responses.append(
            answer(r, status="FILLED", executed="0.00100000", drop_fills=True, when=WRITE_TIME_MS + 500)),
            port.refresh(r)),
        venue_view=lambda r: answer(r, status="FILLED", executed="0.00100000", drop_fills=True),
        base="0.0005"),
    "legacy_journal_filled_without_fills": dict(
        first=lambda r: [answer(r, status="NEW")],
        legacy=legacy_state(OrderLifecycleState.FILLED, "0.001")),
    "legacy_journal_partially_filled_without_fills": dict(
        first=lambda r: [answer(r, status="NEW")],
        legacy=legacy_state(OrderLifecycleState.PARTIALLY_FILLED, "0.0005"), open_orders=True),
}


@pytest.mark.parametrize("case", sorted(ATTACKS))
def test_an_unproven_execution_never_admits_new_risk(tmp_path, case):
    out = run(tmp_path, **ATTACKS[case])
    assert out["second_POST"] == 0, out


# --- controls: correct trading must keep working ------------------------------------------------

def _backfilled_fill(reference):
    order = TestnetOrder(symbol="BTCUSDT", client_order_id=reference, venue_order_id="4242",
                         state=OrderLifecycleState.FILLED, status="FILLED", original_quantity=Decimal("0.001"),
                         executed_quantity=Decimal("0.001"), cummulative_quote_quantity=Decimal("27"),
                         fills=(fx_fill_model(),), transact_time=BASE + timedelta(milliseconds=1500))
    return [e for e in build_events(order, correlation_id=UUID(int=7))
            if e.event_type == EXECUTION_FILL_EVENT_TYPE]


def fx_fill_model():
    from crypto_trader.execution.binance_testnet.models import TestnetFill
    return TestnetFill(price=Decimal("27000"), quantity=Decimal("0.001"), commission=Decimal("0"),
                       commission_asset="USDT", trade_id="901")


CONTROLS = {
    "write_answer_filled_with_fills": dict(
        first=lambda r: [answer(r, status="FILLED", executed="0.00100000", fills=[fill("0.00100000")])],
        base="0.001"),
    "write_answer_new_then_cancel_without_execution": dict(
        first=lambda r: [answer(r, status="NEW")],
        after=lambda port, transport, r: (transport.responses.append(cancel_answer(r, executed="0")),
                                          port.cancel(r))),
    "write_partial_then_refresh_filled_with_the_rest_already_journaled": dict(
        first=lambda r: [answer(r, status="FILLED", executed="0.00100000",
                                fills=[fill("0.00050000", 901), fill("0.00050000", 902)])],
        after=lambda port, transport, r: (transport.responses.append(
            answer(r, status="FILLED", executed="0.00100000", drop_fills=True, when=WRITE_TIME_MS + 500)),
            port.refresh(r)),
        base="0.001"),
    "adoption_canceled_without_execution": dict(
        first=duplicate_then(lambda r: answer(r, status="CANCELED", executed="0", drop_fills=True))),
    "adoption_filled_then_its_fills_arrive_bound_to_our_order": dict(
        first=duplicate_then(lambda r: answer(r, status="FILLED", executed="0.00100000", drop_fills=True)),
        legacy=_backfilled_fill,
        venue_view=lambda r: answer(r, status="FILLED", executed="0.00100000", drop_fills=True),
        base="0.001"),
}


@pytest.mark.parametrize("case", sorted(CONTROLS))
def test_a_proven_or_empty_execution_still_trades(tmp_path, case):
    out = run(tmp_path, **CONTROLS[case])
    assert out["second_POST"] == 1, out
