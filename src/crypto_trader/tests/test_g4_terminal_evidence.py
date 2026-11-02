"""RED: prove all three G4 bypasses offline with ScriptedTransport; never a real venue."""
from __future__ import annotations
from decimal import Decimal
import pytest
from crypto_trader.execution.ports import OrderLifecycleState
from crypto_trader.tests.g4_evidence_fixtures import (
    WRITE_TIME_MS, answer, fill, legacy_state, run, CONTROLS,
    cancel_answer, duplicate_then,
)


def _canceled_duplicate_fills(r):
    # Select a stable trade identity whose journal event sorts before the terminal
    # lifecycle event; this avoids relying on an unrelated replay-order failure.
    from crypto_trader.tests import testnet_fixtures as fx
    from crypto_trader.execution.binance_testnet.translate import testnet_order_to_phase3_events
    from uuid import UUID
    parser = fx.client(fx.ScriptedTransport([]))
    for trade_id in range(1, 501):
        result = answer(r, status="CANCELED", executed="0.00100000",
                        fills=[fill("0.00050000", trade_id),
                               fill("0.00050000", trade_id)], when=WRITE_TIME_MS + 500)
        order = parser._parse_order(result.body)
        events = testnet_order_to_phase3_events(order, correlation_id=UUID(int=0))
        if events[1].event_id.int < events[0].event_id.int:
            return result
    raise AssertionError("no fill id sorting before terminal state")


def _canceled_missing_qty(r):
    result = answer(r, status="CANCELED", executed="0.00050000", drop_fills=True, when=WRITE_TIME_MS + 500)
    result.body.pop("executedQty", None)
    return result


def _old_cancel(reference):
    # Simulate a journal written before the new optional execution-evidence field.
    events = legacy_state(OrderLifecycleState.CANCELED, "0.0005")(reference)
    return [event.model_copy(update={"payload": event.payload.model_copy(
        update={"venue_executed_quantity": None})}) for event in events]


def _refresh(view):
    def invoke(port, transport, reference):
        transport.responses.append(view(reference))
        port.refresh(reference)
    return invoke


@pytest.mark.parametrize("case,kwargs", [
    ("refresh-canceled-duplicate-fills",
     dict(first=lambda r: [answer(r, status="NEW")],
          after=_refresh(_canceled_duplicate_fills),
          venue_view=lambda r: answer(r, status="CANCELED", executed="0.001",
                                      when=WRITE_TIME_MS + 1000), base="0.0005")),
    ("refresh-canceled-missing-executedQty",
     dict(first=lambda r: [answer(r, status="NEW")],
          after=_refresh(_canceled_missing_qty), venue_view=_canceled_missing_qty, base="0")),
    ("legacy-canceled-after-unproved-partial",
     dict(first=lambda r: [answer(r, status="NEW")],
          legacy=legacy_state(OrderLifecycleState.CANCELED, "0.0005"),
          venue_view=lambda r: answer(r, status="CANCELED", executed="0.0005",
                                      when=WRITE_TIME_MS + 1000))),
], ids=lambda x: x if isinstance(x, str) else None)
def test_g4_unproved_cancel_blocks_new_post(tmp_path, case, kwargs):
    result = run(tmp_path, **kwargs)
    print("CASE", case, result)
    assert result["second_POST"] == 0, result


@pytest.mark.parametrize("case", [
    "write_answer_new_then_cancel_without_execution",
    "write_answer_filled_with_fills",
    "adoption_filled_then_its_fills_arrive_bound_to_our_order",
])
def test_existing_healthy_controls_still_post(tmp_path, case):
    outcome = run(tmp_path, **CONTROLS[case])
    assert outcome["second_POST"] == 1, outcome


def test_adopted_terminal_without_journaled_venue_identity_stays_blocked(tmp_path):
    # R-9 supersedes the former positive control. A signed status lookup by
    # clientOrderId may belong to a different venue order with the same ID.
    # The startup response is complete and plausible: rejection must come from
    # missing independent venue-order identity, NOT a malformed mock response.
    case = CONTROLS["adoption_canceled_without_execution"]
    outcome = run(
        tmp_path, **case,
        startup_terminal_status=lambda r: answer(
            r, status="CANCELED", executed="0", order_id=4242,
            when=WRITE_TIME_MS + 1000,
        ),
    )
    assert outcome["first"] == "submitted", outcome
    assert outcome["restart"].startswith("ProductionRecoveryRequiredError"), outcome
    assert "terminal venue order identity unproven" in outcome["restart"], outcome
    assert outcome["second_POST"] == 0, outcome


def test_partial_cancel_with_proven_fill_still_posts(tmp_path):
    valid_cancel = lambda r: answer(r, status="CANCELED", executed="0.00050000",
                                    fills=[fill("0.00050000", 903)], when=WRITE_TIME_MS + 500)
    outcome = run(tmp_path, first=lambda r: [answer(r, status="NEW")],
                  after=_refresh(valid_cancel),
                  venue_view=lambda r: answer(r, status="CANCELED", executed="0.00050000",
                                              when=WRITE_TIME_MS + 1000),
                  base="0.0005")
    assert outcome["second_POST"] == 1, outcome


@pytest.mark.parametrize("label,remote", [
    ("wrong-venue-id", lambda r: answer(r, status="CANCELED", executed="0",
                                        order_id=4243, when=WRITE_TIME_MS + 1000)),
    ("status-regressed-to-new", lambda r: answer(r, status="NEW", executed="0",
                                                 when=WRITE_TIME_MS + 1000)),
    ("missing-quantity-at-strict-read", _canceled_missing_qty),
])
def test_terminal_attestation_mismatch_blocks_new_post(tmp_path, label, remote):
    def do_cancel(port, transport, reference):
        transport.responses.append(cancel_answer(reference, executed="0"))
        port.cancel(reference)
    outcome=run(tmp_path, first=lambda r: [answer(r, status="NEW")],
                after=do_cancel, venue_view=remote)
    assert outcome["second_POST"]==0, (label,outcome)


def test_terminal_proof_is_reused_only_in_same_session(tmp_path):
    def do_cancel(port, transport, reference):
        transport.responses.append(cancel_answer(reference, executed="0"))
        port.cancel(reference)
    observed = run(tmp_path, first=lambda r: [answer(r, status="NEW")],
                   after=do_cancel, measure_cache=True)
    assert observed["second_POST"] == 1, observed
    assert observed["cache_re_reads"] == 0, observed


# Astra G4 duplicate-fill identity; the same 0.00025 trade appears twice, while
# executedQty reports 0.0005. A single trade identity is only 0.00025 evidence.
from uuid import UUID
from crypto_trader.tests import testnet_fixtures as fx
from crypto_trader.execution.binance_testnet.translate import (
    testnet_order_to_phase3_events as build_events,
)

def duplicate_partial_response(reference):
    # Two same-trade-ID fills of 0.00025, reported executedQty 0.0005.
    # Select trade id whose event sorts before terminal lifecycle at equal timestamp.
    parser = fx.client(fx.ScriptedTransport([]))
    for trade_id in range(1, 1500):
        raw = answer(reference, status="CANCELED", executed="0.00050000",
                     fills=[fill("0.00025000", trade_id), fill("0.00025000", trade_id)],
                     when=WRITE_TIME_MS + 500)
        order = parser._parse_order(raw.body)
        events = build_events(order, correlation_id=UUID(int=0))
        if events[1].event_id.int < events[0].event_id.int:
            return raw
    raise AssertionError("no suitable replay ordering found")



def _cancel_duplicate(r):
 raw=duplicate_partial_response(r)
 raw.body["origClientOrderId"]=r
 raw.body["clientOrderId"]="cancel-ack"
 return raw

def _after(path):
 def do(port,transport,r):
  if path=="refresh":
   transport.responses.append(duplicate_partial_response(r))
   port.refresh(r)
  elif path=="recover_order":
   transport.responses.append(duplicate_partial_response(r))
   port.recover_order(port._intents[r])
  elif path=="cancel":
   transport.responses.append(_cancel_duplicate(r))
   port.cancel(r)
  elif path=="cancel_all":
   listed=fx.open_orders_response(r)
   listed.body[0]['orderId']=4242  # Match the accepted order's durable venue identity.
   transport.responses.append(listed)
   transport.responses.append(_cancel_duplicate(r))
   port.cancel_all()
  else:
   raise AssertionError(path)
 return do

@pytest.mark.parametrize("path",["refresh","recover_order","cancel","cancel_all","adoption"])
def test_duplicate_partial_fill_blocks_restarted_post(tmp_path,path):
 if path=="adoption":
  args={"first":duplicate_then(duplicate_partial_response)}
 else:
  args={"first":lambda r:[answer(r,status="NEW")],
        "after":_after(path)}
 out=run(tmp_path,base="0.00025",**args)
 print("ROUTE",path,out,flush=True)
 assert out["second_POST"]==0,out

# G4 parser kill-switch mutation coverage (offline, direct parser boundary).
"""Direct order-read parser guard test; offline, no transport requests."""
import pytest
from crypto_trader.tests import testnet_fixtures as fx
from crypto_trader.execution.binance_testnet import BinanceTestnetError

@pytest.mark.parametrize("status",["CANCELED","EXPIRED","REJECTED","FILLED","NEW"])
def test_every_venue_order_read_requires_explicit_executed_qty(status):
    client=fx.client(fx.ScriptedTransport([]))
    body=fx.order_response("ct-abcdef123456abcdef12",status=status,
                           executed_qty="0").body
    body.pop("executedQty")
    with pytest.raises(BinanceTestnetError, match="lacks explicit executedQty"):
        client._parse_order(body)

def test_boolean_executed_qty_rejected():
    client=fx.client(fx.ScriptedTransport([]))
    body=fx.order_response("ct-abcdef123456abcdef12",status="CANCELED",
                           executed_qty="0").body
    body["executedQty"]=False
    with pytest.raises(BinanceTestnetError, match="lacks explicit executedQty"):
        client._parse_order(body)

# Direct wiring coverage: a separately proven terminal-status read must be
# checked before the subsequent risk decision, not merely block by side effect.
from crypto_trader.production import ProductionRiskStateSource

def test_terminal_attestation_callsite_is_invoked_before_new_risk(tmp_path, monkeypatch):
    original = ProductionRiskStateSource._check_terminal_execution_evidence
    observed = []

    def traced(self, events, chain):
        if any(event.event_type == "order.state.changed" for event in events):
            observed.append("terminal-path")
        return original(self, events, chain)

    monkeypatch.setattr(
        ProductionRiskStateSource, "_check_terminal_execution_evidence", traced
    )

    def canceled(port, transport, reference):
        transport.responses.append(cancel_answer(reference, executed="0"))
        port.cancel(reference)

    outcome = run(
        tmp_path, first=lambda reference: [answer(reference, status="NEW")],
        after=canceled,
    )
    assert observed, ("TERMINAL_ATTESTATION_SKIPPED", outcome)
    assert outcome["second_POST"] == 1, outcome
