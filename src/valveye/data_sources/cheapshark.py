from __future__ import annotations

import ssl
from datetime import datetime, timezone

import aiohttp
import certifi

from valveye.config import settings
from valveye.data_sources.base import PriceSource
from valveye.domain import PriceSnapshot


class CheapSharkSource(PriceSource):
    source_name = "cheapshark"

    async def fetch_price(self, game_query: str, region: str, currency: str) -> PriceSnapshot | None:
        timeout = aiohttp.ClientTimeout(total=15)
        ssl_ctx = ssl.create_default_context(cafile=certifi.where())
        connector = aiohttp.TCPConnector(ssl=ssl_ctx)
        async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
            async with session.get(
                f"{settings.cheapshark_base_url}/games",
                params={"title": game_query, "limit": 1, "exact": 0},
            ) as resp:
                resp.raise_for_status()
                games = await resp.json()

            if not games:
                return None

            game_id = str(games[0].get("gameID"))
            title = games[0].get("external") or game_query

            async with session.get(f"{settings.cheapshark_base_url}/games", params={"id": game_id}) as resp:
                resp.raise_for_status()
                detail = await resp.json()

        cheapest_ever = detail.get("cheapestPriceEver", {}) if isinstance(detail, dict) else {}
        deals = detail.get("deals", []) if isinstance(detail, dict) else []
        current = float(deals[0].get("price", 0.0)) if deals else float(cheapest_ever.get("price", 0.0))
        low = float(cheapest_ever.get("price", current))
        low_ts_raw = cheapest_ever.get("date")
        low_ts = None
        if low_ts_raw:
            try:
                low_ts = datetime.fromtimestamp(int(low_ts_raw), tz=timezone.utc)
            except (TypeError, ValueError):
                low_ts = None

        return PriceSnapshot(
            source=self.source_name,
            game_id=game_id,
            title=title,
            currency=currency,
            current_price=current,
            historical_low=low,
            historical_low_at=low_ts,
            history=[],
            store=(deals[0].get("storeID") if deals else None),
        )
