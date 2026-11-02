"""Adversarial request-boundary tests; synthetic credentials and no network."""
import pytest

from crypto_trader.execution.binance_testnet import ScriptedTransport
from crypto_trader.execution.binance_testnet.errors import AttemptJournalRequiredError
from crypto_trader.tests import testnet_fixtures as fx


class SpoofedMethod(str):
    def __eq__(self, other):
        return other == 'GET'

    def upper(self):
        return 'POST'


class SpoofedPath(str):
    def __eq__(self, other):
        return other == '/api/v3/order/test'


@pytest.mark.parametrize('overrides', [
    {'method': SpoofedMethod('POST')},
    {'method': type('PlainSubclass', (str,), {})('GET')},
    {'path': SpoofedPath('/api/v3/order'), 'method': 'POST', 'is_write': True, 'signed': True},
    {'path': type('PlainPath', (str,), {})('/api/v3/time')},
    {'is_write': 0},
    {'signed': 1},
    {'wait': 1},
])
def test_request_refuses_nonbuiltin_security_inputs_before_send(overrides):
    transport = ScriptedTransport(responses=[fx.response(200, {})])
    client = fx.client(transport)
    client.clock.synchronize(fx.SERVER_TIME_MS)
    options = dict(path='/api/v3/time', method='GET', is_write=False, signed=False, wait=True)
    options.update(overrides)
    with pytest.raises(AttemptJournalRequiredError):
        client._request(params={}, weight=1, **options)
    assert transport.requests == []
    assert client.request_log() == ()
