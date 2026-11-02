"""B6: creation denial does not claim all HTTP POSTs or safety cancels are blocked."""
import pytest
from crypto_trader.execution.binance_testnet import AttemptJournalRequiredError
from crypto_trader.execution.binance_testnet.transport import RequestsTransport, ScriptedTransport
from crypto_trader.tests import testnet_fixtures as fx

def test_noncreating_validation_and_safety_delete_remain_available_offline():
    reference='ct2-'+'a'*32
    fake=ScriptedTransport([fx.response(200,{}),fx.cancel_response(reference)])
    https=RequestsTransport(); https.send=fake.send
    client=fx.client(https); client.clock.synchronize(fx.SERVER_TIME_MS)
    assert client.test_order(fx.limit_order_request(reference))=={}
    client.cancel_order('BTCUSDT',reference)
    assert [(r.method,r.url.split('/api/v3/')[-1]) for r in fake.requests]==[('POST','order/test'),('DELETE','order')]
    with pytest.raises(AttemptJournalRequiredError,match='independent monotonic'):
        client._require_independent_write_authority()
    assert len(fake.requests)==2
