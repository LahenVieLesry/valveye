from __future__ import annotations

import ssl

import aiohttp
import certifi

from valveye.config import settings
from valveye.data_sources.base import PriceSource
from valveye.domain import PriceSnapshot


class SteamDBSource(PriceSource):
    """
    SteamDB 适配器。
    说明：SteamDB 无稳定官方公开 API；这里预留可配置网关地址。
    """

    source_name = "steamdb"

    async def fetch_price(self, game_query: str, region: str, currency: str) -> PriceSnapshot | None:
        if not settings.steamdb_api_base:
            return None

        timeout = aiohttp.ClientTimeout(total=12)
        ssl_ctx = ssl.create_default_context(cafile=certifi.where())
        connector = aiohttp.TCPConnector(ssl=ssl_ctx)
        async with aiohttp.ClientSession(timeout=timeout, connector=connector, trust_env=True) as session, session.get(
            f"{settings.steamdb_api_base.rstrip('/')}/price",
            params={"q": game_query, "cc": region.lower(), "currency": currency.upper()},
        ) as resp:
            if resp.status >= 400:
                return None
            payload = await resp.json()

        if not isinstance(payload, dict):
            return None

        title = payload.get("title")
        game_id = payload.get("app_id")
        current = payload.get("current_price")
        low = payload.get("historical_low")
        if title is None or game_id is None or current is None or low is None:
            return None

        return PriceSnapshot(
            source=self.source_name,
            game_id=str(game_id),
            title=str(title),
            currency=currency,
            current_price=float(current),
            historical_low=float(low),
            historical_low_at=None,
            history=[],
            store="steam",
        )
