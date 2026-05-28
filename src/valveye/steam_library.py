"""Steam Player Library Service — fetch and cache owned games via Steam Web API."""
from __future__ import annotations

import asyncio
import json
import ssl
from collections import OrderedDict
from datetime import datetime, timedelta
from pathlib import Path

import aiohttp
import certifi

from valveye.domain import OwnedGame, OwnedGamesResult
from valveye.retry import async_retry

_CACHE_TTL = timedelta(hours=24)
_CACHE_DIR = Path(".valveye")


class SteamLibraryService:
    """Fetches and caches a player's Steam game library (owned games).

    Two-layer cache:
      L1: in-memory OrderedDict (fast, per-session)
      L2: JSON file in .valveye/ directory (persists across restarts, 24h TTL)
    """

    def __init__(
        self,
        steam_api_key: str,
        default_steam_id: str,
        timeout_sec: int = 15,
    ) -> None:
        self._api_key = steam_api_key
        self._default_steam_id = default_steam_id
        self._timeout_sec = timeout_sec
        self._session: aiohttp.ClientSession | None = None
        self._cache: OrderedDict[str, OwnedGamesResult] = OrderedDict()

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

    # ── Public API ────────────────────────────────────────────────────────

    async def get_owned_games(
        self,
        steam_id: str | None = None,
        force_refresh: bool = False,
    ) -> OwnedGamesResult:
        """Fetch player's owned games. Returns OwnedGamesResult (never raises)."""
        sid = steam_id or self._default_steam_id
        if not sid:
            return OwnedGamesResult.empty(error="未配置 Steam ID，请在 .env 中设置 STEAM_ID 或在对话中指定")

        if not force_refresh:
            # L1: memory cache
            cached = self._cache.get(sid)
            if cached is not None:
                self._cache.move_to_end(sid)
                return cached

            # L2: file cache
            file_result = self._load_file_cache(sid)
            if file_result is not None:
                self._cache[sid] = file_result
                return file_result

        # Fetch from API
        result = await self._fetch_from_api(sid)
        if result is not None:
            self._put_cache(sid, result)
            return result

        # API failed — try stale file cache as fallback
        if not force_refresh:
            stale = self._load_file_cache(sid, ignore_ttl=True)
            if stale is not None:
                stale.error = stale.error or "API 请求失败，返回本地缓存数据"
                return stale

        return OwnedGamesResult.empty(
            steam_id=sid,
            error="STEAM_API_KEY 未配置" if not self._api_key else "无法获取游戏库数据",
        )

    async def get_owned_app_ids(
        self,
        steam_id: str | None = None,
    ) -> set[int]:
        """Return the set of owned app_ids (convenience wrapper)."""
        result = await self.get_owned_games(steam_id=steam_id)
        return result.app_ids

    async def is_game_owned(
        self,
        app_id: int,
        steam_id: str | None = None,
    ) -> bool:
        """Check whether a specific game is owned."""
        owned = await self.get_owned_app_ids(steam_id=steam_id)
        return app_id in owned

    # ── Internal: API fetch ───────────────────────────────────────────────

    @async_retry(max_attempts=2, base_delay=1.0, exceptions=(aiohttp.ClientError, asyncio.TimeoutError))
    async def _fetch_from_api(self, steam_id: str) -> OwnedGamesResult | None:
        """Call Steam Web API. Returns None on failure (never raises)."""
        if not self._api_key:
            return None

        session = await self._get_session()
        url = "https://api.steampowered.com/IPlayerService/GetOwnedGames/v1/"
        params = {
            "key": self._api_key,
            "steamid": steam_id,
            "format": "json",
            "include_appinfo": 1,
            "include_played_free_games": 1,
        }

        try:
            async with session.get(url, params=params) as resp:
                if resp.status >= 400:
                    return None
                payload = await resp.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError):
            return None
        except ValueError:
            return None

        if not isinstance(payload, dict):
            return None

        response = payload.get("response")
        if not isinstance(response, dict):
            return None

        game_count = response.get("game_count", 0)
        raw_games = response.get("games")

        if not isinstance(raw_games, list):
            # game_count=0 with no games list → likely private profile
            return OwnedGamesResult(
                steam_id=steam_id,
                game_count=0,
                games=[],
                error="该 Steam 档案为私密状态或游戏库为空",
            )

        games: list[OwnedGame] = []
        for item in raw_games:
            if not isinstance(item, dict):
                continue
            app_id = item.get("appid")
            name = item.get("name")
            if not isinstance(app_id, int) or not isinstance(name, str):
                continue
            playtime = item.get("playtime_forever", 0)
            playtime_2w = item.get("playtime_2weeks")
            icon = item.get("img_icon_url")
            games.append(OwnedGame(
                app_id=app_id,
                name=name,
                playtime_forever=int(playtime) if playtime else 0,
                playtime_2weeks=int(playtime_2w) if playtime_2w else None,
                img_icon_url=str(icon) if icon else None,
            ))

        result = OwnedGamesResult(
            steam_id=steam_id,
            game_count=game_count,
            games=games,
        )

        # Write to file cache
        self._save_file_cache(steam_id, result)
        return result

    # ── Internal: File cache ──────────────────────────────────────────────

    def _file_path(self, steam_id: str) -> Path:
        return _CACHE_DIR / f"steam_library_{steam_id}.json"

    def _load_file_cache(self, steam_id: str, ignore_ttl: bool = False) -> OwnedGamesResult | None:
        """Load from JSON file cache. Returns None if missing or expired."""
        path = self._file_path(steam_id)
        if not path.exists():
            return None

        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None

        if not isinstance(data, dict):
            return None

        # Check TTL
        if not ignore_ttl:
            fetched_at = data.get("fetched_at")
            if isinstance(fetched_at, str):
                try:
                    ts = datetime.fromisoformat(fetched_at)
                    if datetime.now() - ts > _CACHE_TTL:
                        return None
                except ValueError:
                    return None

        games: list[OwnedGame] = []
        for item in data.get("games", []):
            if not isinstance(item, dict):
                continue
            games.append(OwnedGame(
                app_id=item.get("app_id", 0),
                name=item.get("name", ""),
                playtime_forever=item.get("playtime_forever", 0),
                playtime_2weeks=item.get("playtime_2weeks"),
                img_icon_url=item.get("img_icon_url"),
            ))

        return OwnedGamesResult(
            steam_id=data.get("steam_id", steam_id),
            game_count=data.get("game_count", len(games)),
            games=games,
        )

    def _save_file_cache(self, steam_id: str, result: OwnedGamesResult) -> None:
        """Persist to JSON file cache."""
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        path = self._file_path(steam_id)
        data = {
            "steam_id": result.steam_id,
            "fetched_at": datetime.now().isoformat(),
            "game_count": result.game_count,
            "games": [
                {
                    "app_id": g.app_id,
                    "name": g.name,
                    "playtime_forever": g.playtime_forever,
                    "playtime_2weeks": g.playtime_2weeks,
                    "img_icon_url": g.img_icon_url,
                }
                for g in result.games
            ],
        }
        try:
            path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            pass  # file cache failure is non-fatal

    # ── Internal: Memory cache ────────────────────────────────────────────

    def _put_cache(self, steam_id: str, result: OwnedGamesResult) -> None:
        self._cache[steam_id] = result
        self._cache.move_to_end(steam_id)
        while len(self._cache) > 4:
            self._cache.popitem(last=False)
