import asyncio

from valveye.config import settings
from valveye.data_sources.itad import ITADSource


class _FakeResponse:
    def __init__(self, status: int, payload):
        self.status = status
        self._payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def json(self):
        return self._payload


class _FakeSession:
    def __init__(self):
        self.calls = []
        self._responses = []

    def queue_response(self, status: int, payload):
        self._responses.append(_FakeResponse(status=status, payload=payload))

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def get(self, url, params=None):
        self.calls.append(("get", url, params, None))
        return self._responses.pop(0)

    def post(self, url, params=None, json=None):
        self.calls.append(("post", url, params, json))
        return self._responses.pop(0)


def test_itad_source_fetch_price_uses_key_and_v3_prices(monkeypatch):
    old_key = settings.itad_api_key
    old_base = settings.itad_base_url

    fake_session = _FakeSession()
    fake_session.queue_response(
        200,
        [
            {
                "id": "018d937f-012f-73b8-ab2c-898516969e6a",
                "title": "Half-Life 2",
            }
        ],
    )
    fake_session.queue_response(
        200,
        [
            {
                "id": "018d937f-012f-73b8-ab2c-898516969e6a",
                "historyLow": {"all": {"amount": 4.99}},
                "deals": [
                    {
                        "shop": {"name": "Steam"},
                        "price": {"amount": 5.50, "currency": "USD"},
                    },
                    {
                        "shop": {"name": "GOG"},
                        "price": {"amount": 3.99, "currency": "USD"},
                    },
                ],
            }
        ],
    )

    monkeypatch.setattr("valveye.data_sources.itad.aiohttp.ClientSession", lambda *args, **kwargs: fake_session)

    settings.itad_api_key = "test-key"
    settings.itad_base_url = "https://api.isthereanydeal.com/"

    source = ITADSource()
    snapshot = asyncio.run(source.fetch_price(game_query="Half-Life 2", region="cn", currency="CNY"))

    settings.itad_api_key = old_key
    settings.itad_base_url = old_base

    assert snapshot is not None
    assert snapshot.game_id == "018d937f-012f-73b8-ab2c-898516969e6a"
    assert snapshot.title == "Half-Life 2"
    assert snapshot.current_price == 3.99
    assert snapshot.historical_low == 4.99
    assert snapshot.currency == "USD"
    assert snapshot.store == "GOG"

    assert len(fake_session.calls) == 2
    method0, url0, params0, _ = fake_session.calls[0]
    assert method0 == "get"
    assert url0.endswith("/games/search/v1")
    assert params0["key"] == "test-key"
    assert params0["title"] == "Half-Life 2"
    assert params0["results"] == 1

    method1, url1, params1, body1 = fake_session.calls[1]
    assert method1 == "post"
    assert url1.endswith("/games/prices/v3")
    assert params1["key"] == "test-key"
    assert params1["country"] == "CN"
    assert body1 == ["018d937f-012f-73b8-ab2c-898516969e6a"]


def test_itad_source_fetch_price_returns_none_without_api_key():
    old_key = settings.itad_api_key
    settings.itad_api_key = ""

    source = ITADSource()
    snapshot = asyncio.run(source.fetch_price(game_query="Portal", region="US", currency="USD"))

    settings.itad_api_key = old_key
    assert snapshot is None
