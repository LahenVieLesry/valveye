from __future__ import annotations

import asyncio
import ssl

import aiohttp
import certifi

from valveye.config import settings
from valveye.data_sources.base import PriceSource
from valveye.domain import PriceSnapshot


class ITADSource(PriceSource):
    """
    IsThereAnyDeal 适配器。
    说明：公开接口能力会变动，当前实现以可选增强源为主，失败时由聚合层自动降级。
    """

    source_name = "itad"

    async def fetch_price(self, game_query: str, region: str, currency: str) -> PriceSnapshot | None:
        if not settings.itad_api_key:
            return None

        timeout = aiohttp.ClientTimeout(total=12)
        ssl_ctx = ssl.create_default_context(cafile=certifi.where())
        connector = aiohttp.TCPConnector(ssl=ssl_ctx)
        base_url = settings.itad_base_url.rstrip("/")
        country = (region or "CN").upper()
        common_params = {"key": settings.itad_api_key}

        try:
            async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
                async with session.get(
                    f"{base_url}/games/search/v1",
                    params={**common_params, "title": game_query, "results": 1},
                ) as resp:
                    if resp.status >= 400:
                        return None
                    search_payload = await resp.json()

                if not isinstance(search_payload, list) or not search_payload:
                    return None

                first = search_payload[0] if isinstance(search_payload[0], dict) else None
                if not first:
                    return None

                game_id = first.get("id")
                title = first.get("title") or game_query
                if not game_id:
                    return None

                async with session.post(
                    f"{base_url}/games/prices/v3",
                    params={**common_params, "country": country},
                    json=[game_id],
                ) as resp:
                    if resp.status >= 400:
                        return None
                    prices_payload = await resp.json()
        except (aiohttp.ClientError, asyncio.TimeoutError):
            return None

        if not isinstance(prices_payload, list) or not prices_payload:
            return None

        record = prices_payload[0] if isinstance(prices_payload[0], dict) else None
        if not record:
            return None

        deals = record.get("deals") if isinstance(record.get("deals"), list) else []
        best_deal: dict | None = None
        best_price: float | None = None
        for deal in deals:
            if not isinstance(deal, dict):
                continue
            price_obj = deal.get("price")
            if not isinstance(price_obj, dict):
                continue
            amount = price_obj.get("amount")
            try:
                amount_f = float(amount)
            except (TypeError, ValueError):
                continue
            if best_price is None or amount_f < best_price:
                best_price = amount_f
                best_deal = deal

        if best_price is None:
            return None

        history_low_obj = record.get("historyLow") if isinstance(record.get("historyLow"), dict) else {}
        all_low_obj = history_low_obj.get("all") if isinstance(history_low_obj.get("all"), dict) else {}
        low_raw = all_low_obj.get("amount")
        try:
            low = float(low_raw)
        except (TypeError, ValueError):
            low = best_price

        deal_currency = None
        if isinstance(best_deal, dict):
            price_obj = best_deal.get("price")
            if isinstance(price_obj, dict):
                dc = price_obj.get("currency")
                if isinstance(dc, str) and dc:
                    deal_currency = dc

        store = None
        if isinstance(best_deal, dict):
            shop_obj = best_deal.get("shop")
            if isinstance(shop_obj, dict):
                sn = shop_obj.get("name")
                if isinstance(sn, str) and sn:
                    store = sn

        return PriceSnapshot(
            source=self.source_name,
            game_id=str(game_id),
            title=str(title),
            currency=deal_currency or currency,
            current_price=best_price,
            historical_low=low,
            historical_low_at=None,
            history=[],
            store=store,
        )
