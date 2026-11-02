"""Path and origin fences must run before credential use or transport."""
from dataclasses import replace

import pytest

from crypto_trader.execution.binance_testnet import ScriptedTransport
from crypto_trader.execution.binance_testnet.errors import TestnetEndpointError as EndpointError
from crypto_trader.mainnet.errors import MainnetEndpointError
from crypto_trader.tests import mainnet_fixtures as mx
from crypto_trader.tests import testnet_fixtures as fx


@pytest.mark.parametrize('path', [
    '@outside.invalid/api/v3/account',
    '/api/v3/account?signature=SYNTHETIC_INJECTED',
    '/api/v3/account#fragment',
    '/api/v3/../v3/account',
    '//outside.invalid/api/v3/account',
    '/sapi/v1/capital/withdraw/apply',
])
def test_testnet_rejects_nonallowlisted_path_before_credentials(path):
    transport = ScriptedTransport(responses=[fx.response(200, {})])
    client = fx.client(transport)
    client.clock.synchronize(fx.SERVER_TIME_MS)
    with pytest.raises(EndpointError):
        client._request(path, {}, weight=1, is_write=False, signed=True)
    assert transport.requests == []
    assert client.request_log() == ()


class FormattingPath(str):
    def __format__(self, spec):
        return '@outside.invalid/api/v3/account'


def test_mainnet_rejects_allowlisted_subclass_with_different_formatting():
    transport = ScriptedTransport(responses=[fx.response(200, {})])
    client = mx.client(transport)
    with pytest.raises(MainnetEndpointError):
        client._get(FormattingPath('/api/v3/account'), {}, signed=True)
    assert transport.requests == []
    assert client.request_log() == ()


def test_mainnet_base_url_subclass_is_rejected_before_parser_hooks():
    class ParserSpoof(str):
        def strip(self, *args):
            return 'https://api.binance.com'

    client = mx.client(ScriptedTransport())
    with pytest.raises(MainnetEndpointError):
        replace(client, base_url=ParserSpoof('https://outside.invalid'))
