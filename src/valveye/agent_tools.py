from __future__ import annotations

from collections.abc import Awaitable, Callable

import json

from langchain_core.tools import tool

from valveye.game_data import GameDataService
from valveye.pricing import PriceService, _detect_region, fetch_all_regions
from valveye.recommendation import Recommender
from valveye.schemas import ToolError
from valveye.steam_library import SteamLibraryService
from valveye.subscriptions import SubscriptionRepository
from valveye.user_profile import UserProfileStore


# 需要用户确认的工具名集合
SENSITIVE_TOOLS = {"subscribe_game"}


def _format_fallback_chain(chain: list[dict]) -> str:
    """将 fallback_chain 渲染为易读字符串。"""
    if not chain:
        return ""
    parts = []
    for item in chain:
        status = "✅" if item.get("status") == "success" else "❌"
        source = item.get("source", "?")
        reason = item.get("reason", "")
        if reason:
            parts.append(f"{source} {status} ({reason})")
        else:
            parts.append(f"{source} {status}")
    return " | ".join(parts)


PermissionCallback = Callable[[str, dict], Awaitable[tuple[str, str]]] | None


def build_tools(
    price_service: PriceService,
    recommender: Recommender,
    game_data: GameDataService,
    repo: SubscriptionRepository,
    steam_library: SteamLibraryService | None = None,
    user_profile_store: UserProfileStore | None = None,
    permission_callback: PermissionCallback = None,
):
    @tool
    async def query_low_price(game: str, user_query: str = "", region: str = "", currency: str = "", window: str = "all") -> str:
        """查询某游戏当前价与史低信息，window 支持 all/12m/3m。game 参数必须为 Steam 官方英文名。
        user_query 为玩家的原始输入（用于自动检测区域），region/currency 留空时自动检测。"""
        if not region or not currency:
            region, currency = _detect_region(user_query or game)
        try:
            snapshot = await price_service.fetch_first_available(game_query=game, region=region, currency=currency)
        except ToolError as e:
            return str(e)
        decision = price_service.evaluate_low(snapshot=snapshot, window=window)
        chain_str = _format_fallback_chain(snapshot.fallback_chain)
        lines = [
            f"{snapshot.title} | 当前价 {snapshot.current_price:.2f} {snapshot.currency} | "
            f"史低 {snapshot.historical_low:.2f} {snapshot.currency} | "
            f"来源 {snapshot.source} | 在史低: {decision.is_at_low}",
        ]
        if chain_str:
            lines.append(f"数据源尝试: {chain_str}")
        return "\n".join(lines)

    @tool
    async def compare_prices(game: str, user_query: str = "", target_currency: str = "") -> str:
        """对比某游戏在所有 Steam 区域的价格。当用户询问「哪里最便宜」「各区域价格」「跨区对比」时使用。
        game 参数必须为 Steam 官方英文名。user_query 为玩家的原始输入（用于自动检测目标货币）。"""
        results = await fetch_all_regions(game_query=game, target_currency=target_currency, user_query=user_query)
        if not results:
            return "未找到任何区域的价格数据，请检查游戏名称是否正确。"

        cur = results[0].get("target_currency", "")
        lines = [f"📊 {game} 各区域价格对比（{cur}）：", ""]
        for i, r in enumerate(results, 1):
            label = r["label"]
            region = r["region"]
            orig = f"{r['price']:.2f} {r['currency']}"
            conv = f"{r['converted_price']:.2f} {cur}" if r.get("converted_price") != r["price"] else ""
            low = f"{r['converted_low']:.2f} {cur}" if r.get("converted_low") != r["low"] else f"{r['low']:.2f} {r['currency']}"
            store = r.get("store") or ""
            line = f"  {i}. {label}({region}): {orig}"
            if conv:
                line += f" ≈ {conv}"
            line += f" | 史低: {low}"
            if store:
                line += f" | 来源: {store}"
            lines.append(line)

        lines.append("")
        lines.append(f"💰 最低价: {results[0]['label']}({results[0]['region']}) — {results[0]['converted_price']:.2f} {cur}")
        return "\n".join(lines)

    @tool
    async def recommend_similar_games(game: str, top_n: int = 5) -> str:
        """推荐同类游戏（标签+相似产品+差评摘要），返回结构化 JSON。
        自动排除玩家已拥有的游戏。game 参数必须为 Steam 官方英文名。"""
        owned_ids: set[int] = set()
        if steam_library is not None:
            try:
                owned_ids = await steam_library.get_owned_app_ids()
            except Exception:
                pass
        result = await recommender.recommend(game_query=game, top_n=top_n, owned_app_ids=owned_ids)
        text = json.dumps(result, ensure_ascii=False)
        text += (
            "\n\n以上推荐中，有没有你想排除的游戏，或者希望我多推荐某个类型的？"
            "回复 `+游戏名` 表示偏好该类，` -游戏名` 表示排除。"
        )
        return text

    @tool
    async def search_similar_candidates(game: str, top_n: int = 15) -> str:
        """搜索与指定游戏相似的候选游戏列表（轻量级，不含详细描述）。
        返回标题、标签、差评率和来源信号。自动排除玩家已拥有的游戏。
        用于先发现候选，再用 get_game_details 深入调查。
        game 参数必须为 Steam 官方英文名。"""
        owned_ids: set[int] = set()
        if steam_library is not None:
            try:
                owned_ids = await steam_library.get_owned_app_ids()
            except Exception:
                pass
        result = await recommender.search_candidates(game_query=game, top_n=top_n, owned_app_ids=owned_ids)
        if not result:
            return "未找到相似候选，请检查游戏名称。"
        return json.dumps(result, ensure_ascii=False)

    @tool
    async def get_game_details(game: str) -> str:
        """获取单个游戏的详细信息：描述、加权标签（社区投票权重）、类型、评价统计。
        用于深入分析某个候选游戏是否真正适合推荐。game 参数必须为 Steam 官方英文名。"""
        en_name, app_id = await game_data.resolve_game(game)
        if not app_id:
            # Try search fallback
            rows = await game_data.search(term=en_name, limit=1)
            if rows:
                try:
                    app_id = int(rows[0].get("id"))
                except (TypeError, ValueError):
                    return f"未找到游戏：{game}"
            else:
                return f"未找到游戏：{game}"

        profile = await game_data.fetch_profile(app_id=app_id)
        if not profile:
            return f"无法获取游戏详情：{game}"

        return json.dumps({
            "title": profile.title,
            "app_id": profile.app_id,
            "developer": profile.developer,
            "publisher": profile.publisher,
            "release_date": profile.release_date,
            "platforms": profile.platforms,
            "genres": profile.relevance_tags,
            "description": profile.description,
            "detailed_description": profile.detailed_description[:2000] if profile.detailed_description else "",
            "website": profile.website,
            "metacritic_score": profile.metacritic_score,
            "tags_weighted": dict(list(profile.tags_weighted.items())[:15]),
            "positive_count": profile.positive_count,
            "negative_count": profile.negative_count,
            "negative_ratio": round(profile.negative_ratio, 4) if profile.negative_ratio is not None else None,
            "thumb": profile.thumb,
        }, ensure_ascii=False)

    @tool
    async def get_game_reviews(game: str, review_type: str = "negative", count: int = 3) -> str:
        """获取游戏玩家评论片段。review_type 可选 'negative'（差评）或 'positive'（好评）。
        用于了解玩家真实体验，辅助推荐决策。game 参数必须为 Steam 官方英文名。"""
        if review_type not in ("negative", "positive"):
            review_type = "negative"

        en_name, app_id = await game_data.resolve_game(game)
        if not app_id:
            rows = await game_data.search(term=en_name, limit=1)
            if rows:
                try:
                    app_id = int(rows[0].get("id"))
                except (TypeError, ValueError):
                    return f"未找到游戏：{game}"
            else:
                return f"未找到游戏：{game}"

        reviews = await game_data.fetch_reviews(app_id=app_id, review_type=review_type, count=count)
        label = "差评" if review_type == "negative" else "好评"
        if not reviews:
            return f"暂无{label}样本：{game}"

        return json.dumps({
            "game": en_name,
            "review_type": review_type,
            "label": label,
            "reviews": reviews,
        }, ensure_ascii=False)

    @tool
    async def subscribe_game(
        user_id: str,
        game: str,
        channels_json: str,
        user_query: str = "",
        window: str = "all",
        region: str = "",
        currency: str = "",
    ) -> str:
        """订阅游戏价格提醒，channels_json 是通知渠道 JSON 数组。
        user_query 为玩家的原始输入（用于自动检测区域），region/currency 留空时自动检测。"""
        if permission_callback is not None:
            decision, note = await permission_callback("subscribe_game", {
                "user_id": user_id, "game": game, "channels_json": channels_json,
                "window": window, "region": region, "currency": currency,
            })
            if decision == "deny":
                return "❌ 用户已拒绝执行订阅操作。"
            if decision == "other" and note:
                return f"⏸ 用户暂停了订阅操作并备注: {note}"
            # decision == "allow" → continue
        if not region or not currency:
            region, currency = _detect_region(user_query or game)
        try:
            channels = json.loads(channels_json)
        except json.JSONDecodeError as e:
            return f"通知渠道格式错误: {e}。正确格式示例: [{{\"type\":\"email\",\"to\":\"user@example.com\"}}]"
        sub_id, created = repo.add(
            user_id=user_id,
            game_query=game,
            window=window,
            region=region,
            currency=currency,
            channels=channels,
        )
        if created:
            return f"订阅成功，ID={sub_id}"
        return f"订阅已存在，ID={sub_id}"

    @tool
    def list_subscriptions() -> str:
        """查看有效订阅列表。"""
        rows = repo.list_active()
        payload = [
            {
                "id": s.id,
                "user_id": s.user_id,
                "game": s.game_query,
                "window": s.window,
                "region": s.region,
                "currency": s.currency,
                "channels": s.channels,
            }
            for s in rows
        ]
        return json.dumps(payload, ensure_ascii=False)

    @tool
    def request_game_details(games: str) -> str:
        """搜索候选后，请求详情专家获取游戏详细信息。
        games: 逗号分隔的英文游戏名，如 "Hades, Dead Cells, Slay the Spire"。"""
        return f"正在为您获取以下游戏的详细信息: {games}，请稍候…"

    @tool
    async def get_player_library(steam_id: str = "", include_playtime: bool = True) -> str:
        """查询玩家的 Steam 游戏库。返回已拥有游戏列表、游戏数量和游戏时长。
        steam_id 留空时使用默认配置的 Steam ID。
        当玩家询问「我有哪些游戏」「我的游戏库」「我有没有 XX 游戏」时使用。"""
        if steam_library is None:
            return "游戏库查询功能未启用（STEAM_API_KEY 未配置）"
        result = await steam_library.get_owned_games(steam_id=steam_id or None)
        if result.error:
            return f"查询失败：{result.error}"
        if not result.games:
            return "未找到已拥有游戏，可能 Steam 档案为私密状态。"
        games_out = []
        for g in result.games[:50]:  # cap at 50 for token budget
            entry: dict = {"app_id": g.app_id, "name": g.name}
            if include_playtime:
                entry["playtime_hours"] = round(g.playtime_forever / 60, 1)
            games_out.append(entry)
        return json.dumps({
            "steam_id": result.steam_id,
            "game_count": result.game_count,
            "showing": len(games_out),
            "games": games_out,
        }, ensure_ascii=False)

    @tool
    async def get_trending_games(category: str = "top_sellers", limit: int = 10, cc: str = "cn") -> str:
        """获取 Steam 热门游戏列表。

        category 可选值：
        - top_sellers — 热销商品（默认）
        - new_releases — 新品推荐
        - specials — 特惠精选
        - coming_soon — 即将推出

        返回游戏名称、App ID、折扣信息和价格。数据优先来自 Steam 官方 API，失败时回退到 SteamSpy。
        当玩家询问「最近有什么热门游戏」「推荐新游戏」「有什么打折」时使用。"""
        games = await game_data.fetch_trending(category=category, limit=limit, cc=cc)
        if not games:
            return "暂时无法获取热门游戏列表，请稍后再试。"

        label = game_data._STEAM_CATEGORY_MAP.get(category, category)
        lines = [f"🎮 Steam {label}（共 {len(games)} 款）：", ""]
        for i, g in enumerate(games, 1):
            line = f"  {i}. {g.name} (AppID: {g.app_id})"
            if g.discount_percent > 0:
                orig = f"{g.original_price:.2f}" if g.original_price is not None else "?"
                final = f"{g.final_price:.2f}" if g.final_price is not None else "免费"
                line += f" | 💰 {g.currency} {final}（原价 {orig}，-{g.discount_percent}%）"
            elif g.final_price is not None:
                line += f" | 💰 {g.currency} {g.final_price:.2f}" if g.final_price > 0 else " | 免费"
            lines.append(line)
        return "\n".join(lines)

    @tool
    async def search_by_description(description: str, top_n: int = 10) -> str:
        """根据自然语言描述搜索游戏。当用户描述想要的游戏类型但没有指定具体游戏时使用。
        自动排除玩家已拥有的游戏。
        例如："类似黑魂但不那么难的游戏"、"像素风种田游戏"、"有合作模式的肉鸽卡牌"。"""
        owned_ids: set[int] = set()
        if steam_library is not None:
            try:
                owned_ids = await steam_library.get_owned_app_ids()
            except Exception:
                pass
        result = await recommender.recommend(game_query=description, top_n=top_n, owned_app_ids=owned_ids)
        if not result:
            return "未找到匹配的游戏，请尝试更具体的描述。"
        return json.dumps(result, ensure_ascii=False)

    @tool
    async def web_search(query: str, limit: int = 5) -> str:
        """搜索网络获取游戏新闻、评测等信息。当用户询问游戏评测、新闻、攻略时使用。"""
        from valveye.web_tools import web_search as _web_search
        results = await _web_search(query=query, limit=limit)
        if not results:
            return "未找到相关网络内容。"
        lines = [f"🔍 网络搜索结果（{len(results)} 条）：", ""]
        for i, r in enumerate(results, 1):
            trusted = "✓ 可信" if r.get("trusted") else ""
            lines.append(f"{i}. {r.get('title', '')} {trusted}")
            lines.append(f"   {r.get('url', '')}")
            lines.append(f"   {r.get('snippet', '')[:200]}")
            lines.append("")
        return "\n".join(lines)

    @tool
    async def web_fetch(url: str, max_length: int = 4000) -> str:
        """获取指定网页的内容并提取文本。用于深入阅读某篇文章。"""
        from valveye.web_tools import web_fetch as _web_fetch
        text = await _web_fetch(url=url, max_length=max_length)
        return text

    all_tools = [query_low_price, compare_prices, search_similar_candidates, get_game_details, get_game_reviews, recommend_similar_games, subscribe_game, list_subscriptions, request_game_details, search_by_description, get_player_library, get_trending_games, web_search, web_fetch]

    # 按 Agent 分组的工具列表
    tool_map = {t.name: t for t in all_tools}
    tool_groups = {
        "price": [tool_map["query_low_price"], tool_map["compare_prices"]],
        "info": [tool_map["get_game_details"], tool_map["get_game_reviews"], tool_map["get_player_library"], tool_map["get_trending_games"], tool_map["web_search"], tool_map["web_fetch"]],
        "recommend": [tool_map["search_similar_candidates"], tool_map["recommend_similar_games"], tool_map["request_game_details"], tool_map["search_by_description"], tool_map["web_search"]],
        "subs": [tool_map["subscribe_game"], tool_map["list_subscriptions"], tool_map["get_player_library"]],
    }

    return all_tools, tool_groups
