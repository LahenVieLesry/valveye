from __future__ import annotations

import logging
from typing import Any

import aiohttp

logger = logging.getLogger(__name__)

# 可信游戏新闻/评测域名白名单
_TRUSTED_DOMAINS = {
    "ign.com",
    "gamespot.com",
    "metacritic.com",
    "steamcommunity.com",
    "reddit.com",
    "pcgamer.com",
    "eurogamer.net",
    "kotaku.com",
    "rockpapershotgun.com",
    "polygon.com",
    "gameinformer.com",
}


def _is_trusted_domain(url: str) -> bool:
    from urllib.parse import urlparse
    netloc = urlparse(url).netloc.lower()
    return any(netloc.endswith(d) for d in _TRUSTED_DOMAINS)


async def web_search(query: str, limit: int = 5) -> list[dict[str, Any]]:
    """Search the web using DuckDuckGo (no API key required).

    Returns a list of results with title, href, and body snippet.
    """
    try:
        from ddgs import DDGS
    except ImportError as exc:
        logger.warning("ddgs not installed: %s", exc)
        return []

    try:
        with DDGS() as ddgs:
            results = ddgs.text(query, max_results=limit)
    except Exception as exc:
        logger.warning("DuckDuckGo search failed: %s", exc)
        return []

    out: list[dict[str, Any]] = []
    for r in results:
        href = r.get("href", "")
        out.append({
            "title": r.get("title", ""),
            "url": href,
            "snippet": r.get("body", ""),
            "trusted": _is_trusted_domain(href),
        })
    return out


async def web_fetch(url: str, max_length: int = 4000) -> str:
    """Fetch a web page and extract readable text.

    Uses BeautifulSoup to strip tags and return plain text.
    """
    try:
        from bs4 import BeautifulSoup
    except ImportError as exc:
        logger.warning("beautifulsoup4 not installed: %s", exc)
        return f"[错误] 未安装 beautifulsoup4，无法解析网页: {exc}"

    timeout = aiohttp.ClientTimeout(total=10)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, headers={"User-Agent": "Valveye/1.0"}) as resp:
                if resp.status >= 400:
                    return f"[错误] HTTP {resp.status}"
                html = await resp.text()
    except Exception as exc:
        return f"[错误] 获取页面失败: {exc}"

    soup = BeautifulSoup(html, "html.parser")
    # Remove script/style/nav/footer/header tags
    for tag in soup(["script", "style", "nav", "footer", "header", "aside"]):
        tag.decompose()

    # Try to find main article content first
    main = soup.find("main") or soup.find("article") or soup.find("div", class_="content")
    if main:
        text = main.get_text(separator="\n", strip=True)
    else:
        text = soup.get_text(separator="\n", strip=True)

    # Collapse excessive blank lines
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    text = "\n".join(lines)

    if len(text) > max_length:
        text = text[:max_length] + "…"

    return text
