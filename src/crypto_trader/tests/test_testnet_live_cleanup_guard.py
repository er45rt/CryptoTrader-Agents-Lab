"""Offline adverse teardown tests. No credentials, network or testnet orders.

Use the actual module-scoped pytest fixture generator with a synthetic client
rather than invoking the live fixture through pytest.
"""
from types import SimpleNamespace

import pytest

from crypto_trader.tests import test_testnet_live_integration as live


class FakeLiveTestnetClient:
    symbol = "BTCUSDT"

    def __init__(self, failure=None, *, clean_preflight=False):
        self.failure = failure
        self.clean_preflight = clean_preflight
        self._test_owned_order_ids = {"synthetic-order"}
        self.cancel_calls = 0
        self.open_calls = 0

    def verify_environment(self):
        return SimpleNamespace(base_url="synthetic-only")

    def cancel_order(self, symbol, reference):
        assert symbol == self.symbol and reference == "synthetic-order"
        self.cancel_calls += 1
        if self.failure == "cancel":
            raise RuntimeError("synthetic cancellation outage; secret=NEVER_LOG")
        return SimpleNamespace(client_order_id=reference)

    def cancel_all_open_orders(self, symbol):
        raise AssertionError("broad cancellation must never occur")

    def startup_open_orders(self, symbol):
        assert symbol == self.symbol
        self.open_calls += 1
        if self.clean_preflight and self.open_calls == 1:
            return ()
        if self.failure == "inspect":
            raise TimeoutError("synthetic status outage; secret=NEVER_LOG")
        if self.failure in ("remaining", "cancel"):
            return (SimpleNamespace(client_order_id="synthetic-order"),)
        if self.failure == "invalid":
            return None
        return ()


@pytest.mark.parametrize("failure", ["cancel", "inspect", "remaining", "invalid"])
def test_teardown_reports_unverified_open_orders(monkeypatch, failure):
    client = FakeLiveTestnetClient(failure, clean_preflight=True)
    monkeypatch.setattr(live, "_new_client", lambda: client)
    fixture = live.live_client.__wrapped__()
    assert next(fixture) is client
    with pytest.raises(AssertionError, match="Testnet.*(unverified|unresolved)"):
        next(fixture)
    assert client.cancel_calls == (1 if failure in ("cancel", "remaining") else 0)
    assert client.open_calls == (3 if failure == "remaining" else 2)


def test_teardown_verifies_empty_open_orders(monkeypatch):
    client = FakeLiveTestnetClient()
    monkeypatch.setattr(live, "_new_client", lambda: client)
    fixture = live.live_client.__wrapped__()
    assert next(fixture) is client
    with pytest.raises(StopIteration):
        next(fixture)
    assert client.cancel_calls == 0
    assert client.open_calls == 3


def test_teardown_failure_does_not_echo_underlying_exception_details(monkeypatch):
    client = FakeLiveTestnetClient("cancel", clean_preflight=True)
    monkeypatch.setattr(live, "_new_client", lambda: client)
    fixture = live.live_client.__wrapped__()
    next(fixture)
    with pytest.raises(AssertionError) as exc:
        next(fixture)
    assert "NEVER_LOG" not in str(exc.value)
    assert "manual" in str(exc.value).lower()


@pytest.mark.parametrize("failure", ["cancel", "inspect", "remaining", "invalid"])
def test_each_live_test_refuses_uncertain_cleanup(failure):
    """A failed test must not leave orders for the next test to inherit."""
    client = FakeLiveTestnetClient(failure)
    per_test = live._verify_after_each_live_test.__wrapped__(client)
    assert next(per_test) is None
    with pytest.raises(AssertionError, match="manual reconciliation required"):
        next(per_test)
    assert client.cancel_calls == (1 if failure in ("cancel", "remaining") else 0)
    assert client.open_calls == (2 if failure == "remaining" else 1)


def test_each_live_test_verifies_clean_venue_before_next_test():
    client = FakeLiveTestnetClient()
    per_test = live._verify_after_each_live_test.__wrapped__(client)
    assert next(per_test) is None
    with pytest.raises(StopIteration):
        next(per_test)
    assert client.cancel_calls == 0
    assert client.open_calls == 2


def test_live_cleanup_is_automatic_and_function_scoped():
    marker = live._verify_after_each_live_test._fixture_function_marker
    assert marker.autouse is True
    assert marker.scope == "function"


@pytest.mark.parametrize("failure", ["inspect", "remaining", "invalid"])
def test_live_setup_refuses_unknown_or_existing_orders_without_cancelling(monkeypatch, failure):
    """Pre-existing or unknown venue orders must never be mass-cancelled by setup."""
    client = FakeLiveTestnetClient(failure)
    monkeypatch.setattr(live, "_new_client", lambda: client)
    generator = live.live_client.__wrapped__()
    with pytest.raises(AssertionError, match="preflight.*manual reconciliation required") as error:
        next(generator)
    assert "NEVER_LOG" not in str(error.value)
    assert client.open_calls == 1
    assert client.cancel_calls == 0


def test_live_setup_requires_verified_empty_orders_before_yield(monkeypatch):
    client = FakeLiveTestnetClient()
    monkeypatch.setattr(live, "_new_client", lambda: client)
    generator = live.live_client.__wrapped__()
    assert next(generator) is client
    assert client.open_calls == 1
    assert client.cancel_calls == 0
    with pytest.raises(StopIteration):
        next(generator)
    assert client.cancel_calls == 0
    assert client.open_calls == 3


class TargetedCleanupClient:
    """Only an offline double for an independently simulated signed venue listing."""
    symbol = "BTCUSDT"

    def __init__(self, current, owned=("test-owned",)):
        self.current = tuple(current)
        self._test_owned_order_ids = set(owned)
        self.operations = []

    def startup_open_orders(self, symbol):
        assert symbol == self.symbol
        self.operations.append("signed-list")
        return self.current

    def cancel_order(self, symbol, client_order_id):
        assert symbol == self.symbol
        self.operations.append(("targeted", client_order_id))
        self.current = tuple(
            order for order in self.current if order.client_order_id != client_order_id
        )
        return SimpleNamespace(state=SimpleNamespace(value="canceled"))

    def cancel_all_open_orders(self, symbol):
        self.operations.append("DANGEROUS-BROAD-CANCEL")
        self.current = ()
        return ()


def test_teardown_refuses_foreign_order_without_broad_or_targeted_cancel():
    """An externally introduced order is never subject to test-owned cleanup."""
    client = TargetedCleanupClient([SimpleNamespace(client_order_id="external-order")])
    with pytest.raises(AssertionError, match="manual reconciliation required"):
        live._verify_live_testnet_teardown(client)
    assert "DANGEROUS-BROAD-CANCEL" not in client.operations
    assert not any(isinstance(op, tuple) and op[0] == "targeted" for op in client.operations)


def test_teardown_targets_only_reserved_order_identity():
    client = TargetedCleanupClient([SimpleNamespace(client_order_id="test-owned")])
    live._verify_live_testnet_teardown(client)
    assert client.operations == ["signed-list", ("targeted", "test-owned"), "signed-list"]


def test_teardown_fails_closed_on_mixed_owned_and_foreign_orders():
    client = TargetedCleanupClient([
        SimpleNamespace(client_order_id="test-owned"),
        SimpleNamespace(client_order_id="external-order"),
    ])
    with pytest.raises(AssertionError, match="manual reconciliation required"):
        live._verify_live_testnet_teardown(client)
    assert client.operations == ["signed-list"]


def test_foreign_order_arriving_during_cleanup_is_not_cancelled():
    class ConcurrentClient(TargetedCleanupClient):
        def cancel_order(self, symbol, client_order_id):
            result = super().cancel_order(symbol, client_order_id)
            self.current = (SimpleNamespace(client_order_id="external-late"),)
            return result

    client = ConcurrentClient([SimpleNamespace(client_order_id="test-owned")])
    with pytest.raises(AssertionError, match="manual reconciliation required"):
        live._verify_live_testnet_teardown(client)
    assert client.operations == ["signed-list", ("targeted", "test-owned"), "signed-list"]
    assert client.current[0].client_order_id == "external-late"


def test_missing_ownership_register_fails_closed_before_any_cancel():
    client = TargetedCleanupClient([SimpleNamespace(client_order_id="test-owned")])
    del client._test_owned_order_ids
    with pytest.raises(AssertionError, match="manual reconciliation required"):
        live._verify_live_testnet_teardown(client)
    assert client.operations == ["signed-list"]


def test_test_order_request_reserves_exact_identity_before_submission():
    from decimal import Decimal

    class RulesClient:
        symbol = "BTCUSDT"
        _test_owned_order_ids = set()

        def symbol_rules(self, symbol):
            return SimpleNamespace(
                price_tick=Decimal("0.01"),
                quantity_step=Decimal("0.0001"),
                min_notional=Decimal("5"),
                min_quantity=Decimal("0.0001"),
            )

        def price(self):
            return Decimal("100")

    client = RulesClient()
    request = live._resting_limit_request(client)
    assert request.client_order_id in client._test_owned_order_ids


def test_runtime_intent_reserves_deterministic_test_identity_before_submission():
    from uuid import uuid4
    from decimal import Decimal

    class RulesClient:
        symbol = "BTCUSDT"

        def __init__(self):
            self._test_owned_order_ids = set()

        def symbol_rules(self, symbol):
            return SimpleNamespace(
                price_tick=Decimal("0.01"),
                quantity_step=Decimal("0.0001"),
                min_notional=Decimal("5"),
                min_quantity=Decimal("0.0001"),
            )

        def price(self):
            return Decimal("100")

    client = RulesClient()
    intent_id = uuid4()
    live._execution_request(client, intent_id, uuid4())
    assert f"ct2-{intent_id.hex}" in client._test_owned_order_ids
