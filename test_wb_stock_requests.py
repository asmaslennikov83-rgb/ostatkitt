import asyncio

import pytest

from app.config import CabinetConfig
from app.wb_api import WBClient, WBApiError


class Response:
    def __init__(self, status, payload=None):
        self.status = status
        self.payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def text(self):
        return 'failure'

    async def json(self, **kwargs):
        return self.payload


class Session:
    def __init__(self, status=204, payload=None):
        self.calls = []
        self.status = status
        self.payload = payload

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return Response(self.status, self.payload)


def test_write_payload_uses_size_id():
    session = Session()
    client = WBClient(CabinetConfig('one', 'One', 'fake'), session)
    asyncio.run(client.put_fbs_stocks(11, {101: 5}))
    assert len(session.calls) == 1
    assert session.calls[0][0] == 'PUT'
    assert session.calls[0][2]['json'] == {'stocks': [{'chrtId': 101, 'amount': 5}]}


@pytest.mark.parametrize('status', [429, 500, 502, 503, 504])
def test_write_not_retried_on_uncertain_response(status):
    session = Session(status)
    client = WBClient(CabinetConfig('one', 'One', 'fake'), session)
    with pytest.raises(WBApiError):
        asyncio.run(client.put_fbs_stocks(11, {101: 5}))
    assert len(session.calls) == 1


def test_write_timeout_not_retried():
    class TimeoutSession(Session):
        def request(self, method, url, **kwargs):
            self.calls.append(method)
            raise asyncio.TimeoutError()
    session = TimeoutSession()
    client = WBClient(CabinetConfig('one', 'One', 'fake'), session)
    with pytest.raises(WBApiError):
        asyncio.run(client.put_fbs_stocks(11, {101: 5}))
    assert len(session.calls) == 1


def test_read_missing_rows_and_invalid_response():
    session = Session(200, {'stocks': [{'chrtId': 101, 'amount': 5}]})
    client = WBClient(CabinetConfig('one', 'One', 'fake'), session)
    assert asyncio.run(client.get_fbs_stocks(11, [101, 102])) == {101: 5, 102: 0}
    assert session.calls[0][2]['json'] == {'chrtIds': [101, 102]}
    session.payload = {}
    with pytest.raises(WBApiError):
        asyncio.run(client.get_fbs_stocks(11, [101]))
