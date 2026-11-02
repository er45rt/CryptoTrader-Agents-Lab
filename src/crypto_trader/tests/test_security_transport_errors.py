"""Signed transport failures must stay safe even when logged with a traceback."""
import logging
import traceback

import pytest
import requests

from crypto_trader.execution.binance_testnet import (
    AmbiguousOrderError, HttpRequest, PreSendTransportError, RequestsTransport,
    ScriptedTransport, TransportError, TransportTimeout,
)
from crypto_trader.tests import mainnet_fixtures as mx
from crypto_trader.tests import testnet_fixtures as fx

SENTINEL = 'SYNTHETIC_SIGNATURE_DO_NOT_LOG'
LEAK = 'https://testnet.binance.vision/api/v3/order?signature=' + SENTINEL


def assert_safe_error(error, caplog):
    logging.getLogger('security-test').error('safe failure', exc_info=(type(error), error, error.__traceback__))
    rendered = ''.join(traceback.format_exception(error)) + caplog.text
    assert SENTINEL not in rendered
    assert fx.FAKE_API_KEY not in rendered
    assert fx.FAKE_API_SECRET not in rendered
    seen = set()
    pending = [error]
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        assert SENTINEL not in str(current)
        pending.extend(e for e in (current.__cause__, current.__context__) if e is not None)


@pytest.mark.parametrize('kind, expected', [
    ('ConnectTimeout', PreSendTransportError),
    ('ReadTimeout', TransportTimeout),
    ('ConnectionError', TransportError),
    ('RequestException', TransportError),
    ('dns', PreSendTransportError),
])
def test_requests_transport_removes_unsafe_exception_chains(monkeypatch, caplog, kind, expected):
    def fail(session, method, url, **kwargs):
        if kind == 'dns':
            from urllib3 import HTTPSConnectionPool
            from urllib3.exceptions import MaxRetryError, NewConnectionError
            error = requests.exceptions.ConnectionError(MaxRetryError(
                HTTPSConnectionPool('testnet.binance.vision'), '/api/v3/order',
                NewConnectionError(None, LEAK),
            ))
        else:
            error = getattr(requests.exceptions, kind)(LEAK + fx.FAKE_API_KEY + fx.FAKE_API_SECRET)
        raise error

    monkeypatch.setattr(requests.sessions.Session, 'request', fail)
    request = HttpRequest('POST', 'https://testnet.binance.vision/api/v3/order', query='signature=' + SENTINEL)
    with pytest.raises(expected) as caught:
        RequestsTransport().send(request)
    assert type(caught.value) is expected
    assert_safe_error(caught.value, caplog)


@pytest.mark.parametrize('operation, expected', [
    ('testnet-read', TransportTimeout),
    ('mainnet-read', TransportTimeout),
    ('prepared-write', AmbiguousOrderError),
    ('safety-cancel', PreSendTransportError),
])
def test_clients_sanitize_custom_transport_failures(caplog, operation, expected):
    error = PreSendTransportError(LEAK) if operation == 'safety-cancel' else TransportTimeout(LEAK)
    responses = [error] * 3
    if operation == 'prepared-write':
        responses.insert(0, fx.exchange_info_response())
    transport = ScriptedTransport(responses)
    client = mx.client(transport) if operation == 'mainnet-read' else fx.client(transport, retries=1)
    client.clock.synchronize(fx.SERVER_TIME_MS)
    with pytest.raises(expected) as caught:
        if operation == 'mainnet-read':
            client.account_snapshot()
        elif operation == 'prepared-write':
            fx.place_order(client, fx.limit_order_request('ct-security-safe-errors'))
        elif operation == 'safety-cancel':
            client._request('/api/v3/order', {}, method='DELETE', weight=1, signed=True, is_write=True)
        else:
            client.account_snapshot()
    assert_safe_error(caught.value, caplog)
    assert SENTINEL not in '\n'.join(client.request_log())
    if operation in ('prepared-write', 'safety-cancel'):
        assert len([r for r in transport.requests if r.is_write]) == 1


@pytest.mark.parametrize('venue', ['testnet', 'mainnet'])
@pytest.mark.parametrize('message', [
    LEAK, 'https%3A%2F%2Fexample.invalid%2F%3Fsignature%3D' + SENTINEL,
])
def test_signed_url_echoed_by_venue_is_not_exposed_in_errors_or_logs(caplog, venue, message):
    transport = ScriptedTransport([fx.response(400, {'code': -2014, 'msg': message})])
    client = mx.client(transport) if venue == 'mainnet' else fx.client(transport)
    client.clock.synchronize(fx.SERVER_TIME_MS)
    with pytest.raises(Exception) as caught:
        client.account_snapshot()
    assert_safe_error(caught.value, caplog)


@pytest.mark.parametrize('venue', ['testnet', 'mainnet'])
@pytest.mark.parametrize('encoded', [
    'signature%253D' + SENTINEL,
    'signature%25253D' + SENTINEL,
])
def test_multiply_encoded_signature_is_redacted(caplog, venue, encoded):
    transport = ScriptedTransport([fx.response(400, {'code': -2014, 'msg': encoded})])
    client = mx.client(transport) if venue == 'mainnet' else fx.client(transport)
    client.clock.synchronize(fx.SERVER_TIME_MS)
    with pytest.raises(Exception) as caught:
        client.account_snapshot()
    assert_safe_error(caught.value, caplog)
    assert len(transport.requests) == 1


@pytest.mark.parametrize('venue', ['testnet', 'mainnet'])
def test_percent_encoded_key_without_signature_is_redacted(caplog, venue):
    actual_key = mx.FAKE_API_KEY if venue == 'mainnet' else fx.FAKE_API_KEY
    encoded_key = ''.join(f'%{ord(c):02X}' for c in actual_key)
    transport = ScriptedTransport([fx.response(400, {'code': -2014, 'msg': encoded_key})])
    client = mx.client(transport) if venue == 'mainnet' else fx.client(transport)
    client.clock.synchronize(fx.SERVER_TIME_MS)
    with pytest.raises(Exception) as caught:
        client.account_snapshot()
    assert encoded_key not in str(caught.value)
    assert_safe_error(caught.value, caplog)


def test_httprequest_repr_excludes_all_user_controlled_url_and_credentials():
    request = HttpRequest(
        'GET', 'https://testnet.binance.vision/api/v3/account?signature=' + SENTINEL,
        query='signature=' + SENTINEL,
        headers={'X-MBX-APIKEY': fx.FAKE_API_KEY},
    )
    displayed = repr(request)
    assert SENTINEL not in displayed
    assert fx.FAKE_API_KEY not in displayed
    assert 'signature=' not in displayed
    assert 'HttpRequest' in displayed


@pytest.mark.parametrize('scenario', ['forged_class_name', 'stale_explicit_cause', 'stale_context', 'different_host'], ids=['fake-type', 'old-cause', 'old-context', 'wrong-host'])
def test_pre_send_classifier_requires_request_bound_urllib3_proof(monkeypatch, scenario):
    """P0-3: unrelated/fake exceptions must remain UNKNOWN after a write."""
    from urllib3 import HTTPSConnectionPool
    from urllib3.exceptions import MaxRetryError, NewConnectionError

    if scenario == 'different_host':
        underlying = MaxRetryError(
            HTTPSConnectionPool('unrelated.invalid'),
            '/api/v3/order', NewConnectionError(None, 'connection refused'),
        )
        error = requests.exceptions.ConnectionError(underlying)
    else:
        error = requests.exceptions.ConnectionError('request transmission may have occurred')
        if scenario == 'forged_class_name':
            fake_type = type('NewConnectionError', (Exception,), {})
            error.__cause__ = fake_type('untrusted name match')
        elif scenario == 'stale_explicit_cause':
            error.__cause__ = NewConnectionError(None, 'a previous unrelated connection')
        else:
            error.__context__ = NewConnectionError(None, 'an old implicit context')

    def injected(session, method, url, **kwargs):
        raise error

    monkeypatch.setattr(requests.sessions.Session, 'request', injected)
    sent = HttpRequest('POST', 'https://testnet.binance.vision/api/v3/order', query='signature=' + SENTINEL, is_write=True)
    with pytest.raises(TransportError) as caught:
        RequestsTransport().send(sent)
    assert type(caught.value) is TransportError, 'untrusted pre-send evidence was incorrectly accepted'
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


@pytest.mark.parametrize('kind', ['dns', 'refused'])
def test_pre_send_classifier_accepts_request_bound_urllib3_failure(monkeypatch, kind):
    """Real requests wrapping MaxRetryError.reason is admissible only for this endpoint."""
    from urllib3 import HTTPSConnectionPool
    from urllib3.exceptions import MaxRetryError, NameResolutionError, NewConnectionError

    inner = (
        NameResolutionError('testnet.binance.vision', None, OSError('dns failure'))
        if kind == 'dns' else NewConnectionError(None, 'connection refused')
    )
    error = requests.exceptions.ConnectionError(
        MaxRetryError(HTTPSConnectionPool('testnet.binance.vision'), '/api/v3/order', inner)
    )

    def injected(session, method, url, **kwargs):
        raise error

    monkeypatch.setattr(requests.sessions.Session, 'request', injected)
    with pytest.raises(PreSendTransportError) as caught:
        RequestsTransport().send(HttpRequest('POST', 'https://testnet.binance.vision/api/v3/order', is_write=True))
    assert type(caught.value) is PreSendTransportError
    assert caught.value.__cause__ is None
