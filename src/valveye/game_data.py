from __future__ import annotations

import asyncio
import re
import ssl
from collections import OrderedDict

import aiohttp
import certifi

from valveye.config import settings
from valveye.domain import GameProfile, TrendingGame
from valveye.pricing import resolve_game
from valveye.retry import async_retry

_MORE_LIKE_APP_RE = re.compile(r"/app/(\d+)")
_SPACE_RE = re.compile(r"\s+")


def _clip(text: str, limit: int = 110) -> str:
    cleaned = _SPACE_RE.sub(" ", text.replace("\n", " ")).strip()
    if len(cleaned) <= limit:
        return cleaned
    return f"{cleaned[: limit - 1].rstrip()}…"


class GameDataService:
    """Encapsulates all Steam/SteamSpy API interactions for game data retrieval."""

    def __init__(self, cache_size: int = 128):
        self._timeout_sec = max(8, settings.steam_recommend_timeout_sec)
        self._session: aiohttp.ClientSession | None = None
        self._cache: OrderedDict[int, GameProfile] = OrderedDict()
        self._cache_size = cache_size

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=self._timeout_sec)
            ssl_ctx = ssl.create_default_context(cafile=certifi.where())
            connector = aiohttp.TCPConnector(ssl=ssl_ctx)
            self._session = aiohttp.ClientSession(timeout=timeout, connector=connector, trust_env=True)
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    def _cache_put(self, profile: GameProfile) -> None:
        self._cache[profile.app_id] = profile
        self._cache.move_to_end(profile.app_id)
        while len(self._cache) > self._cache_size:
            self._cache.popitem(last=False)

    def get_cached_profile(self, app_id: int) -> GameProfile | None:
        return self._cache.get(app_id)

    async def resolve_game(self, game_query: str) -> tuple[str, int | None]:
        """Resolve any-language game name to English name and app_id."""
        resolved = await resolve_game(game_query)
        en_name = resolved.english_name if resolved else game_query
        app_id = resolved.app_id if resolved else None
        return en_name, app_id

    async def search(self, term: str, limit: int = 10) -> list[dict]:
        """Search Steam Store by term."""
        if not term.strip():
            return []
        session = await self._get_session()
        url = f"{settings.steam_store_base_url.rstrip('/')}/api/storesearch"
        try:
            async with session.get(url, params={"term": term, "l": "english", "cc": "us"}) as resp:
                if resp.status >= 400:
                    return []
                payload = await resp.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
            return []

        items = payload.get("items") if isinstance(payload, dict) else None
        if not isinstance(items, list):
            return []
        return [it for it in items[:limit] if isinstance(it, dict)]

    async def fetch_appdetails(self, app_id: int) -> dict | None:
        """Fetch raw appdetails from Steam Store."""
        session = await self._get_session()
        url = f"{settings.steam_store_base_url.rstrip('/')}/api/appdetails"
        try:
            async with session.get(url, params={"appids": app_id, "l": "english", "cc": "us"}) as resp:
                if resp.status >= 400:
                    return None
                payload = await resp.json()
        except (aiohttp.ClientError, asyncio.TimeoutError):
            return None

        root = payload.get(str(app_id)) if isinstance(payload, dict) else None
        if not isinstance(root, dict):
            return None
        if root.get("success") is not True:
            return None
        data = root.get("data")
        return data if isinstance(data, dict) else None

    async def fetch_steamspy(self, app_id: int) -> dict | None:
        """Fetch SteamSpy details for an app."""
        session = await self._get_session()
        url = f"{settings.steamspy_api_base_url.rstrip('/')}/api.php"
        try:
            async with session.get(url, params={"request": "appdetails", "appid": app_id}) as resp:
                if resp.status >= 400:
                    return None
                payload = await resp.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError):
            return None
        return payload if isinstance(payload, dict) else None

    async def fetch_more_like_this_ids(self, app_id: int) -> list[int]:
        """Fetch Steam's 'More Like This' recommended app IDs."""
        session = await self._get_session()
        url = f"{settings.steam_store_base_url.rstrip('/')}/recommended/morelike/app/{app_id}"
        try:
            async with session.get(url, params={"l": "english"}) as resp:
                if resp.status >= 400:
                    return []
                body = await resp.text()
        except (aiohttp.ClientError, asyncio.TimeoutError):
            return []

        found = _MORE_LIKE_APP_RE.findall(body)
        uniq: dict[int, None] = {}
        for raw in found:
            try:
                sid = int(raw)
            except (TypeError, ValueError):
                continue
            if sid != app_id:
                uniq[sid] = None
        return list(uniq.keys())

    async def fetch_reviews(
        self, app_id: int, review_type: str = "negative", count: int = 3
    ) -> list[str]:
        """Fetch review text snippets. review_type: 'negative' or 'positive'."""
        session = await self._get_session()
        url = f"{settings.steam_store_base_url.rstrip('/')}/appreviews/{app_id}"
        try:
            async with session.get(
                url,
                params={
                    "json": 1,
                    "language": "all",
                    "filter": "recent",
                    "review_type": review_type,
                    "purchase_type": "all",
                    "num_per_page": count,
                },
            ) as resp:
                if resp.status >= 400:
                    return []
                payload = await resp.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError):
            return []

        rows = payload.get("reviews") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            return []

        snippets: list[str] = []
        for r in rows:
            if not isinstance(r, dict):
                continue
            text = r.get("review")
            if not isinstance(text, str) or not text.strip():
                continue
            snippets.append(_clip(text))
            if len(snippets) >= count:
                break
        return snippets

    # ── Trending / Featured ──────────────────────────────────────────────

    # Steam Store category keys → human-readable labels
    _STEAM_CATEGORY_MAP: dict[str, str] = {
        "top_sellers": "热销商品",
        "new_releases": "新品推荐",
        "specials": "特惠精选",
        "coming_soon": "即将推出",
    }

    async def fetch_trending(
        self,
        category: str = "top_sellers",
        limit: int = 10,
        cc: str = "cn",
        lang: str = "schinese",
    ) -> list[TrendingGame]:
        """Fetch trending / featured games.

        Primary: Steam Store ``/api/featuredcategories``.
        Fallback: SteamSpy ``/api.php?request=top100in2weeks``.

        Parameters
        ----------
        category:
            One of ``top_sellers``, ``new_releases``, ``specials``, ``coming_soon``.
            Falls back to SteamSpy with ``top100in2weeks`` / ``top100forever`` /
            ``top100owned``.
        limit:
            Max items to return (default 10).
        cc / lang:
            Country code and language for Steam Store API.
        """
        # --- try Steam Store API first ---
        games = await self._fetch_steam_featured(category, limit, cc=cc, lang=lang)
        if games:
            return games

        # --- fallback to SteamSpy ---
        spy_category = {
            "top_sellers": "top100in2weeks",
            "new_releases": "top100in2weeks",
            "specials": "top100in2weeks",
            "coming_soon": "top100forever",
        }.get(category, "top100in2weeks")
        return await self._fetch_steamspy_trending(spy_category, limit)

    async def _fetch_steam_featured(
        self, category: str, limit: int, cc: str = "cn", lang: str = "schinese",
    ) -> list[TrendingGame]:
        """Fetch from Steam Store ``/api/featuredcategories``."""
        session = await self._get_session()
        url = f"{settings.steam_store_base_url.rstrip('/')}/api/featuredcategories"
        try:
            async with session.get(url, params={"l": lang, "cc": cc}) as resp:
                if resp.status >= 400:
                    return []
                payload = await resp.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
            return []

        if not isinstance(payload, dict):
            return []

        # Each category is a top-level key with an "items" array
        cat_data = payload.get(category)
        if not isinstance(cat_data, dict):
            return []
        items = cat_data.get("items")
        if not isinstance(items, list):
            return []

        currency = ""
        result: list[TrendingGame] = []
        for it in items[:limit]:
            if not isinstance(it, dict):
                continue
            app_id = it.get("id")
            name = it.get("name")
            if not isinstance(app_id, (int, float)) or not isinstance(name, str):
                continue
            if not currency:
                currency = str(it.get("currency") or "")

            # Prices come in hundredths (e.g. 7700 = ¥77.00)
            raw_orig = it.get("original_price")
            raw_final = it.get("final_price")
            original_price: float | None = None
            final_price: float | None = None
            try:
                if isinstance(raw_orig, (int, float)) and raw_orig > 0:
                    original_price = raw_orig / 100
            except (TypeError, ValueError):
                pass
            try:
                if isinstance(raw_final, (int, float)):
                    final_price = raw_final / 100
            except (TypeError, ValueError):
                pass

            discount = 0
            try:
                discount = int(it.get("discount_percent") or 0)
            except (TypeError, ValueError):
                pass

            result.append(TrendingGame(
                app_id=int(app_id),
                name=name,
                discount_percent=discount,
                original_price=original_price,
                final_price=final_price,
                currency=currency,
                source="steam_featured",
            ))
        return result

    async def _fetch_steamspy_trending(
        self, category: str, limit: int,
    ) -> list[TrendingGame]:
        """Fetch from SteamSpy ``/api.php``."""
        session = await self._get_session()
        url = f"{settings.steamspy_api_base_url.rstrip('/')}/api.php"
        try:
            async with session.get(url, params={"request": category}) as resp:
                if resp.status >= 400:
                    return []
                payload = await resp.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
            return []

        if not isinstance(payload, dict):
            return []

        result: list[TrendingGame] = []
        for app_id_str, info in payload.items():
            if len(result) >= limit:
                break
            if not isinstance(info, dict):
                continue
            try:
                app_id = int(app_id_str)
            except (TypeError, ValueError):
                continue
            name = info.get("name")
            if not isinstance(name, str) or not name.strip():
                continue
            result.append(TrendingGame(
                app_id=app_id,
                name=name.strip(),
                source="steamspy",
            ))
        return result

    @async_retry(max_attempts=2, base_delay=1.0, exceptions=(aiohttp.ClientError, asyncio.TimeoutError))
    async def _fetch_appdetails_with_retry(self, app_id: int) -> dict | None:
        """Fetch appdetails with retry. Lets network errors propagate for the retry decorator."""
        session = await self._get_session()
        url = f"{settings.steam_store_base_url.rstrip('/')}/api/appdetails"
        async with session.get(url, params={"appids": app_id, "l": "english", "cc": "us"}) as resp:
            if resp.status >= 400:
                return None
            payload = await resp.json()

        root = payload.get(str(app_id)) if isinstance(payload, dict) else None
        if not isinstance(root, dict):
            return None
        if root.get("success") is not True:
            return None
        data = root.get("data")
        return data if isinstance(data, dict) else None

    async def fetch_profile(self, app_id: int) -> GameProfile | None:
        """Fetch full game profile, with caching."""
        cached = self._cache.get(app_id)
        if cached is not None:
            self._cache.move_to_end(app_id)
            return cached

        try:
            details = await self._fetch_appdetails_with_retry(app_id)
        except (aiohttp.ClientError, asyncio.TimeoutError):
            return None
        if not details:
            return None

        tags = _extract_display_tags(details)
        spy = await self.fetch_steamspy(app_id)
        tags_weighted: dict[str, int] = {}
        if spy:
            tags = _merge_tags(tags, _extract_tags_from_steamspy(spy))
            tags_weighted = _extract_weighted_tags(spy)

        negative_ratio = None
        positive_count = 0
        negative_count = 0
        if spy:
            try:
                positive_count = int(spy.get("positive", 0))
                negative_count = int(spy.get("negative", 0))
                total = positive_count + negative_count
                if total > 0:
                    negative_ratio = negative_count / total
            except (TypeError, ValueError):
                pass

        # Extract developer/publisher (lists in Steam API)
        developers = details.get("developers")
        developer = ", ".join(developers) if isinstance(developers, list) and developers else ""
        publishers = details.get("publishers")
        publisher = ", ".join(publishers) if isinstance(publishers, list) and publishers else ""

        # Extract release date
        release_info = details.get("release_date")
        release_date = ""
        if isinstance(release_info, dict):
            release_date = str(release_info.get("date") or "")

        # Extract platforms
        raw_platforms = details.get("platforms")
        platforms: dict[str, bool] = {}
        if isinstance(raw_platforms, dict):
            for k in ("windows", "mac", "linux"):
                platforms[k] = bool(raw_platforms.get(k))

        # Metacritic
        metacritic_raw = details.get("metacritic")
        metacritic_score = None
        if isinstance(metacritic_raw, dict):
            try:
                metacritic_score = int(metacritic_raw.get("score", 0)) or None
            except (TypeError, ValueError):
                pass

        profile = GameProfile(
            app_id=app_id,
            title=str(details.get("name") or ""),
            app_type=str(details.get("type") or ""),
            tags=_extract_display_tags(details, tags),
            relevance_tags=_extract_relevance_tags(details),
            tags_weighted=tags_weighted,
            description=str(details.get("short_description") or ""),
            detailed_description=str(details.get("detailed_description") or ""),
            developer=developer,
            publisher=publisher,
            release_date=release_date,
            platforms=platforms,
            website=str(details.get("website") or ""),
            metacritic_score=metacritic_score,
            thumb=str(details.get("header_image") or "") or None,
            negative_ratio=negative_ratio,
            positive_count=positive_count,
            negative_count=negative_count,
        )
        self._cache_put(profile)
        return profile


# --- Tag extraction helpers (shared between GameDataService and Recommender) ---


_FEATURE_TAGS = {
    "single-player",
    "single player",
    "multiplayer",
    "multi-player",
    "co-op",
    "co op",
    "online co-op",
    "online co op",
    "shared/split screen co-op",
    "shared/split screen co op",
    "shared/split screen",
    "steam achievements",
    "full controller support",
    "steam cloud",
    "hdr available",
    "family sharing",
    "remote play on phone",
    "remote play on tablet",
    "remote play on tv",
    "remote play together",
    "commentary available",
    "steam trading cards",
    "steam workshop",
    "stats",
    "save anytime",
    "includes level editor",
    "captions available",
    "playable without timed input",
    "camera comfort",
    "custom volume controls",
    "stereo sound",
    "surround sound",
}


def is_feature_tag(tag: str) -> bool:
    normalized = (tag or "").strip().lower()
    return normalized in _FEATURE_TAGS or normalized.startswith("remote play")


def _extract_display_tags(details: dict, base_tags: list[str] | None = None) -> list[str]:
    tags: list[str] = []
    genres = details.get("genres")
    if isinstance(genres, list):
        for item in genres:
            if isinstance(item, dict):
                desc = item.get("description")
                if isinstance(desc, str) and desc.strip():
                    tags.append(desc.strip())

    categories = details.get("categories")
    if isinstance(categories, list):
        for item in categories:
            if isinstance(item, dict):
                desc = item.get("description")
                if isinstance(desc, str) and desc.strip():
                    tags.append(desc.strip())

    if base_tags:
        tags = [*base_tags, *tags]

    return _merge_tags([], tags)


def _extract_relevance_tags(details: dict) -> list[str]:
    genres: list[str] = []
    genre_rows = details.get("genres")
    if isinstance(genre_rows, list):
        for item in genre_rows:
            if not isinstance(item, dict):
                continue
            desc = item.get("description")
            if not isinstance(desc, str):
                continue
            cleaned = desc.strip()
            if cleaned and not is_feature_tag(cleaned):
                genres.append(cleaned)

    if genres:
        return _merge_tags([], genres)

    fallback: list[str] = []
    category_rows = details.get("categories")
    if isinstance(category_rows, list):
        for item in category_rows:
            if not isinstance(item, dict):
                continue
            desc = item.get("description")
            if not isinstance(desc, str):
                continue
            cleaned = desc.strip()
            if cleaned and not is_feature_tag(cleaned):
                fallback.append(cleaned)

    return _merge_tags([], fallback)


def _extract_tags_from_steamspy(payload: dict) -> list[str]:
    tag_map = payload.get("tags")
    if not isinstance(tag_map, dict):
        return []

    weighted: list[tuple[int, str]] = []
    for key, votes in tag_map.items():
        if not isinstance(key, str) or not key.strip():
            continue
        try:
            weight = int(votes)
        except (TypeError, ValueError):
            weight = 0
        weighted.append((weight, key.strip()))

    weighted.sort(key=lambda x: x[0], reverse=True)
    return [name for _, name in weighted[:10]]


def _extract_weighted_tags(payload: dict) -> dict[str, int]:
    tag_map = payload.get("tags")
    if not isinstance(tag_map, dict):
        return {}

    result: dict[str, int] = {}
    for key, votes in tag_map.items():
        if not isinstance(key, str) or not key.strip():
            continue
        try:
            result[key.strip()] = int(votes)
        except (TypeError, ValueError):
            pass
    return dict(sorted(result.items(), key=lambda x: x[1], reverse=True))


def _merge_tags(base: list[str], extra: list[str]) -> list[str]:
    seen: dict[str, str] = {}
    for raw in [*base, *extra]:
        if not isinstance(raw, str):
            continue
        cleaned = raw.strip()
        if not cleaned:
            continue
        key = cleaned.lower()
        if key not in seen:
            seen[key] = cleaned
    return list(seen.values())
