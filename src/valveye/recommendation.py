from __future__ import annotations

import difflib
import ssl

import aiohttp
import certifi

from valveye.config import settings


class Recommender:
    async def recommend(self, game_query: str, top_n: int = 10) -> list[dict]:
        timeout = aiohttp.ClientTimeout(total=15)
        ssl_ctx = ssl.create_default_context(cafile=certifi.where())
        connector = aiohttp.TCPConnector(ssl=ssl_ctx)
        async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
            async with session.get(
                f"{settings.cheapshark_base_url}/games",
                params={"title": game_query, "limit": 20, "exact": 0},
            ) as resp:
                resp.raise_for_status()
                games = await resp.json()

        scored: list[tuple[float, dict]] = []
        for g in games:
            title = str(g.get("external", ""))
            if not title:
                continue
            score = difflib.SequenceMatcher(a=game_query.lower(), b=title.lower()).ratio()
            if title.lower() == game_query.lower():
                continue
            scored.append(
                (
                    score,
                    {
                        "title": title,
                        "game_id": g.get("gameID"),
                        "cheapest": g.get("cheapest"),
                        "thumb": g.get("thumb"),
                    },
                )
            )

        scored.sort(key=lambda x: x[0], reverse=True)
        return [item for _, item in scored[:top_n]]
