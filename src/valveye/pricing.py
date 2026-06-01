from __future__ import annotations

import asyncio
import os
import ssl
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import aiohttp
import certifi

from valveye.config import settings
from valveye.data_sources.base import PriceSource
from valveye.domain import PriceSnapshot
from valveye.rate_limiter import AsyncRateLimiter
from valveye.retry import async_retry


@dataclass(slots=True)
class ResolvedGame:
    english_name: str
    app_id: int | None = None


# Steam 主要区域：(region_code, currency_code, 中文标签)
STEAM_REGIONS: list[tuple[str, str, str]] = [
    ("US", "USD", "美区"),
    ("GB", "GBP", "英区"),
    ("EU", "EUR", "欧区"),
    ("CN", "CNY", "国区"),
    ("JP", "JPY", "日区"),
    ("KR", "KRW", "韩区"),
    ("TW", "TWD", "台区"),
    ("HK", "HKD", "港区"),
    ("SG", "SGD", "新加坡区"),
    ("AU", "AUD", "澳区"),
    ("CA", "CAD", "加区"),
    ("MX", "MXN", "墨区"),
    ("BR", "BRL", "巴区"),
    ("AR", "ARS", "阿区"),
    ("CL", "CLP", "智利区"),
    ("CO", "COP", "哥伦比亚区"),
    ("IN", "INR", "印区"),
    ("RU", "RUB", "俄区"),
    ("TR", "TRY", "土区"),
    ("UA", "UAH", "乌区"),
    ("KZ", "KZT", "哈区"),
    ("ZA", "ZAR", "南非区"),
    ("AE", "AED", "阿联酋区"),
]

# 时区名称/前缀 → (region, currency) 映射
_TZ_TO_REGION: dict[str, tuple[str, str]] = {
    "Asia/Shanghai": ("CN", "CNY"), "Asia/Chongqing": ("CN", "CNY"),
    "Asia/Harbin": ("CN", "CNY"), "Asia/Urumqi": ("CN", "CNY"),
    "Asia/Tokyo": ("JP", "JPY"),
    "Asia/Seoul": ("KR", "KRW"),
    "Asia/Taipei": ("TW", "TWD"),
    "Asia/Hong_Kong": ("HK", "HKD"),
    "Asia/Singapore": ("SG", "SGD"),
    "Asia/Kolkata": ("IN", "INR"), "Asia/Calcutta": ("IN", "INR"),
    "Asia/Dubai": ("AE", "AED"),
    "Asia/Almaty": ("KZ", "KZT"), "Asia/Qyzylorda": ("KZ", "KZT"),
    "Europe/Moscow": ("RU", "RUB"),
    "Europe/Istanbul": ("TR", "TRY"),
    "Europe/Kiev": ("UA", "UAH"), "Europe/Kyiv": ("UA", "UAH"),
    "Europe/London": ("GB", "GBP"),
    "Europe/Berlin": ("EU", "EUR"), "Europe/Paris": ("EU", "EUR"),
    "Europe/Amsterdam": ("EU", "EUR"), "Europe/Madrid": ("EU", "EUR"),
    "Europe/Rome": ("EU", "EUR"), "Europe/Lisbon": ("EU", "EUR"),
    "Europe/Brussels": ("EU", "EUR"), "Europe/Vienna": ("EU", "EUR"),
    "Europe/Zurich": ("EU", "EUR"), "Europe/Warsaw": ("EU", "EUR"),
    "Europe/Prague": ("EU", "EUR"), "Europe/Stockholm": ("EU", "EUR"),
    "America/New_York": ("US", "USD"), "America/Chicago": ("US", "USD"),
    "America/Denver": ("US", "USD"), "America/Los_Angeles": ("US", "USD"),
    "America/Toronto": ("CA", "CAD"), "America/Vancouver": ("CA", "CAD"),
    "America/Mexico_City": ("MX", "MXN"),
    "America/Sao_Paulo": ("BR", "BRL"),
    "America/Argentina/Buenos_Aires": ("AR", "ARS"),
    "America/Santiago": ("CL", "CLP"),
    "America/Bogota": ("CO", "COP"),
    "Australia/Sydney": ("AU", "AUD"), "Australia/Melbourne": ("AU", "AUD"),
    "Australia/Perth": ("AU", "AUD"),
    "Africa/Johannesburg": ("ZA", "ZAR"),
}

# 语言环境 → (region, currency) 映射（从 LANG/LC_ALL 环境变量解析）
_LOCALE_TO_REGION: dict[str, tuple[str, str]] = {
    "zh": ("CN", "CNY"), "ja": ("JP", "JPY"), "ko": ("KR", "KRW"),
    "ru": ("RU", "RUB"), "tr": ("TR", "TRY"), "uk": ("UA", "UAH"),
    "pt": ("BR", "BRL"), "es": ("MX", "MXN"), "de": ("EU", "EUR"),
    "fr": ("EU", "EUR"), "it": ("EU", "EUR"), "nl": ("EU", "EUR"),
    "pl": ("EU", "EUR"), "sv": ("EU", "EUR"), "da": ("EU", "EUR"),
    "fi": ("EU", "EUR"), "cs": ("EU", "EUR"), "ro": ("EU", "EUR"),
    "hu": ("EU", "EUR"), "el": ("EU", "EUR"), "th": ("SG", "SGD"),
    "vi": ("SG", "SGD"), "id": ("SG", "SGD"), "ms": ("SG", "SGD"),
    "hi": ("IN", "INR"), "ar": ("AE", "AED"),
}


def _detect_system_region() -> tuple[str, str] | None:
    """从系统语言环境和时区推断用户所在区域，无法判断时返回 None。"""
    # 优先从 LANG / LC_ALL 环境变量检测
    for var in ("LC_ALL", "LC_MESSAGES", "LANG"):
        lang_env = os.environ.get(var, "")
        if lang_env and lang_env != "C":
            lang_code = lang_env.split("_")[0].split(".")[0].lower()
            if lang_code in _LOCALE_TO_REGION:
                return _LOCALE_TO_REGION[lang_code]

    # 回退：从系统时区推断
    try:
        # 尝试 TZ 环境变量获取完整时区名
        tz_val = os.environ.get("TZ", "")
        if tz_val and "/" in tz_val:
            result = _TZ_TO_REGION.get(tz_val)
            if result:
                return result

        # 尝试 zoneinfo 获取当前时区
        try:
            from datetime import datetime as _dt
            local_tz = _dt.now().astimezone().tzinfo
            if local_tz:
                zone_key = getattr(local_tz, "key", None)
                if zone_key:
                    result = _TZ_TO_REGION.get(zone_key)
                    if result:
                        return result
        except Exception:
            pass
    except Exception:
        pass

    return None


def _detect_region(query: str) -> tuple[str, str]:
    """根据输入文本的字符特征和系统环境，返回默认的 (region, currency)。

    优先级：专属文字（假名/韩文） > 系统语言/时区检测 > CJK 汉字（需系统环境辅助） > 美区兜底
    注意：CJK 统一表意文字（汉字）同时被中日韩使用，不能直接判定为国区。
    """
    has_kana = False
    has_hangul = False
    has_cjk = False

    for ch in query:
        cp = ord(ch)
        # 假名 → 日区（专属字符，无歧义）
        if (0x3040 <= cp <= 0x30FF) or (0x31F0 <= cp <= 0x31FF):
            has_kana = True
        # 韩文 → 韩区（专属字符，无歧义）
        elif 0xAC00 <= cp <= 0xD7AF or 0x1100 <= cp <= 0x11FF:
            has_hangul = True
        # CJK 汉字 → 中日韩共用，不能直接判定
        elif (0x4E00 <= cp <= 0x9FFF) or (0x3400 <= cp <= 0x4DBF) or (0xF900 <= cp <= 0xFAFF):
            has_cjk = True
        elif 0x0400 <= cp <= 0x04FF:
            return "RU", "RUB"
        elif 0x0600 <= cp <= 0x06FF:
            return "AE", "AED"
        elif 0x0E00 <= cp <= 0x0E7F:
            return "SG", "SGD"
        elif 0x0900 <= cp <= 0x097F:
            return "IN", "INR"

    # 专属文字判定（无歧义）
    if has_kana:
        return "JP", "JPY"
    if has_hangul:
        return "KR", "KRW"

    # 系统环境检测（对拉丁文字和纯汉字均有效）
    system_region = _detect_system_region()
    if system_region:
        return system_region

    # 纯汉字且无法从系统环境判断 → 兜底国区
    if has_cjk:
        return "CN", "CNY"

    # 兜底
    return "US", "USD"


def _search_locales(query: str) -> list[tuple[str, str]]:
    """根据输入文本的字符特征，返回按优先级排列的 Steam (l, cc) 候选列表。"""
    has_kana = False
    has_hangul = False
    has_cjk = False
    has_cyrillic = False
    has_arabic = False
    has_thai = False
    has_devanagari = False
    has_latin_ext = False  # Latin Extended (Vietnamese, Turkish, etc.)

    for ch in query:
        cp = ord(ch)
        if 0x3040 <= cp <= 0x30FF:
            has_kana = True
        elif 0xAC00 <= cp <= 0xD7AF or 0x1100 <= cp <= 0x11FF:
            has_hangul = True
        elif (0x4E00 <= cp <= 0x9FFF) or (0x3400 <= cp <= 0x4DBF) or (0xF900 <= cp <= 0xFAFF):
            has_cjk = True
        elif 0x0400 <= cp <= 0x04FF:
            has_cyrillic = True
        elif 0x0600 <= cp <= 0x06FF:
            has_arabic = True
        elif 0x0E00 <= cp <= 0x0E7F:
            has_thai = True
        elif 0x0900 <= cp <= 0x097F:
            has_devanagari = True
        elif 0x0100 <= cp <= 0x024F:
            has_latin_ext = True

    locales: list[tuple[str, str]] = []

    if has_kana:
        # 含假名 → 日文优先，再试中文（汉字也可能出现在日文标题中）
        locales.extend([("japanese", "jp"), ("schinese", "cn"), ("korean", "kr")])
    elif has_hangul:
        # 含韩文 → 韩文优先
        locales.extend([("korean", "kr"), ("japanese", "jp"), ("schinese", "cn")])
    elif has_cjk:
        # 纯汉字无假名/韩文 → 简中优先，再试繁中、日文
        locales.extend([("schinese", "cn"), ("tchinese", "tw"), ("japanese", "jp")])

    if has_cyrillic:
        locales.extend([("russian", "ru"), ("ukrainian", "ua")])

    if has_arabic:
        locales.append(("arabic", "sa"))

    if has_thai:
        locales.append(("thai", "th"))

    if has_devanagari:
        locales.append(("indian", "in"))

    # Vietnamese / Turkish / Central European (diacritics)
    if has_latin_ext and not has_cjk and not has_kana and not has_hangul:
        locales.extend([("vietnamese", "vn"), ("turkish", "tr")])

    # 英语作为最终兜底（如无特殊字符或所有候选均失败时）
    locales.append(("english", "us"))

    # 去重，保持顺序
    seen: set[tuple[str, str]] = set()
    unique: list[tuple[str, str]] = []
    for item in locales:
        if item not in seen:
            seen.add(item)
            unique.append(item)
    return unique


def _make_session(timeout_sec: float = 12) -> aiohttp.ClientSession:
    ssl_ctx = ssl.create_default_context(cafile=certifi.where())
    connector = aiohttp.TCPConnector(ssl=ssl_ctx)
    return aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout_sec), connector=connector, trust_env=True)


async def resolve_game(
    game_query: str, *, session: aiohttp.ClientSession | None = None,
) -> ResolvedGame | None:
    """将任意语言的游戏名解析为英文标准名。

    流程：检测语言 → 按优先级尝试多个 locale 搜索 Steam → 用 appdetails(l=english) 取英文名。
    """
    candidates = _search_locales(game_query)
    base = settings.steam_store_base_url.rstrip("/")
    owns_session = session is None

    try:
        if owns_session:
            session = _make_session(12)
        assert session is not None

        # Step 1: 按候选 locale 依次搜索，找到第一个有结果的
        first: dict | None = None
        app_id: int | None = None
        for lang, cc in candidates:
            async with session.get(
                f"{base}/api/storesearch",
                params={"term": game_query, "l": lang, "cc": cc},
            ) as resp:
                if resp.status >= 400:
                    continue
                payload = await resp.json(content_type=None)

            items = payload.get("items") if isinstance(payload, dict) else None
            if not isinstance(items, list) or not items:
                continue

            first = items[0]
            raw_id = first.get("id")
            if raw_id:
                app_id = int(raw_id)
                break

        if app_id is None:
            return None

        # Step 2: 用 app_id + l=english 获取英文标准名
        async with session.get(
            f"{base}/api/appdetails",
            params={"appids": app_id, "l": "english", "cc": "us"},
        ) as resp:
            if resp.status >= 400:
                name = first.get("name") if first else None
                return ResolvedGame(english_name=str(name), app_id=app_id) if name else None
            detail_payload = await resp.json(content_type=None)

    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
        return None
    finally:
        if owns_session and session is not None:
            await session.close()

    app_data = detail_payload.get(str(app_id)) if isinstance(detail_payload, dict) else None
    if isinstance(app_data, dict) and app_data.get("success"):
        data = app_data.get("data", {})
        en_name = data.get("name") if isinstance(data, dict) else None
        if en_name:
            return ResolvedGame(english_name=str(en_name), app_id=app_id)

    # 兜底：用搜索结果中的名称
    name = first.get("name") if first else None
    return ResolvedGame(english_name=str(name), app_id=app_id) if name else None


@async_retry(max_attempts=2, base_delay=1.0, exceptions=(aiohttp.ClientError, asyncio.TimeoutError))
async def _fetch_exchange_rates(
    base_currency: str, *, session: aiohttp.ClientSession | None = None,
) -> dict[str, float]:
    """获取以 base_currency 为基准的汇率。失败时返回空字典。"""
    url = f"https://open.er-api.com/v6/latest/{base_currency}"
    owns_session = session is None
    try:
        if owns_session:
            session = _make_session(8)
        assert session is not None
        async with session.get(url) as resp:
            if resp.status >= 400:
                return {}
            data = await resp.json(content_type=None)
    finally:
        if owns_session and session is not None:
            await session.close()

    rates = data.get("rates") if isinstance(data, dict) else None
    if not isinstance(rates, dict):
        return {}
    result: dict[str, float] = {}
    for code, rate in rates.items():
        try:
            result[str(code)] = float(rate)
        except (TypeError, ValueError):
            continue
    return result


async def fetch_all_regions(game_query: str, target_currency: str = "", user_query: str = "") -> list[dict]:
    """查询所有 Steam 区域的价格，按汇率转换为 target_currency 后排序返回。"""
    from valveye.data_sources.itad import ITADSource  # 避免循环导入

    session = _make_session(15)
    rate_limiter = AsyncRateLimiter(qps=5.0, burst=3)
    try:
        resolved = await resolve_game(game_query, session=session)
        en_name = resolved.english_name if resolved else game_query

        if not target_currency:
            _, target_currency = _detect_region(user_query or game_query)

        itad = ITADSource()
        sem = asyncio.Semaphore(5)

        async def _fetch_one(region: str, currency: str, label: str) -> dict | None:
            async with sem:
                await rate_limiter.acquire()
                snapshot = await itad.fetch_price(game_query=en_name, region=region, currency=currency)
            if snapshot is None:
                return None
            return {
                "region": region,
                "label": label,
                "currency": snapshot.currency,
                "price": snapshot.current_price,
                "low": snapshot.historical_low,
                "store": snapshot.store,
            }

        tasks = [_fetch_one(r, c, l) for r, c, l in STEAM_REGIONS]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        valid: list[dict] = []
        for r in results:
            if isinstance(r, dict):
                valid.append(r)

        if not valid:
            return []

        # 汇率转换
        try:
            rates = await _fetch_exchange_rates(target_currency, session=session)
        except (aiohttp.ClientError, asyncio.TimeoutError):
            rates = {}
    finally:
        await session.close()

    for item in valid:
        src_currency = item["currency"]
        if src_currency == target_currency:
            item["converted_price"] = item["price"]
            item["converted_low"] = item["low"]
        elif src_currency in rates and rates[src_currency] > 0:
            rate = rates[src_currency]
            item["converted_price"] = round(item["price"] / rate, 2)
            item["converted_low"] = round(item["low"] / rate, 2)
        else:
            item["converted_price"] = item["price"]
            item["converted_low"] = item["low"]
        item["target_currency"] = target_currency

    valid.sort(key=lambda x: x.get("converted_price", x["price"]))
    return valid


@dataclass(slots=True)
class LowPriceDecision:
    snapshot: PriceSnapshot
    is_at_low: bool
    is_new_low: bool
    window: str
    window_low: float


class PriceService:
    def __init__(self, sources: list[PriceSource]):
        self.sources = sources
        self._session: aiohttp.ClientSession | None = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = _make_session(12)
        return self._session

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
            self._session = None

    async def fetch_first_available(self, game_query: str, region: str, currency: str) -> PriceSnapshot:
        from valveye.schemas import ToolError, ToolErrorCode

        session = await self._get_session()
        resolved = await resolve_game(game_query, session=session)
        query = resolved.english_name if resolved else game_query
        app_id = resolved.app_id if resolved else None

        fallback_chain: list[dict] = []
        last_exc: Exception | None = None
        for source in self.sources:
            try:
                result = await source.fetch_price(game_query=query, region=region, currency=currency)
                if result is not None:
                    result.app_id = app_id
                    fallback_chain.append({
                        "source": source.source_name,
                        "status": "success",
                        "reason": "",
                    })
                    result.fallback_chain = fallback_chain
                    return result
                fallback_chain.append({
                    "source": source.source_name,
                    "status": "failed",
                    "reason": "返回空结果",
                })
            except Exception as exc:
                last_exc = exc
                fallback_chain.append({
                    "source": source.source_name,
                    "status": "failed",
                    "reason": f"{type(exc).__name__}: {exc!s}",
                })
                continue

        if last_exc is not None:
            exc_name = type(last_exc).__name__
            raise ToolError(
                code=ToolErrorCode.PRICE_SOURCE_UNAVAILABLE,
                message=f"所有价格数据源均不可用，最后错误: {exc_name}: {last_exc!s}",
                suggestion="请稍后重试，或检查网络连接。",
            ) from last_exc
        raise ToolError(
            code=ToolErrorCode.PRICE_SOURCE_UNAVAILABLE,
            message="所有价格数据源均未返回结果",
            suggestion="请检查游戏名称是否为 Steam 官方英文名。",
        )

    @staticmethod
    def evaluate_low(snapshot: PriceSnapshot, window: str, known_notified_low: float | None = None) -> LowPriceDecision:
        # 目前第三方接口普遍给的是全历史最低价；当 history 有数据时才按窗口重算。
        low = snapshot.historical_low
        if snapshot.history and window in {"3m", "12m"}:
            now = datetime.now(tz=timezone.utc)
            days = 90 if window == "3m" else 365
            since = now - timedelta(days=days)
            candidates = [p.price for p in snapshot.history if p.timestamp >= since]
            if candidates:
                low = min(candidates)

        is_at_low = snapshot.current_price <= low + 1e-6
        is_new_low = known_notified_low is None or low < known_notified_low - 1e-6
        return LowPriceDecision(snapshot=snapshot, is_at_low=is_at_low, is_new_low=is_new_low, window=window, window_low=low)
