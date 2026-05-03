from __future__ import annotations

import json

from langchain_core.tools import tool

from valveye.game_data import GameDataService
from valveye.pricing import PriceService, _detect_region, fetch_all_regions
from valveye.recommendation import Recommender
from valveye.subscriptions import SubscriptionRepository


def build_tools(price_service: PriceService, recommender: Recommender, game_data: GameDataService, repo: SubscriptionRepository):
    @tool
    async def query_low_price(game: str, user_query: str = "", region: str = "", currency: str = "", window: str = "all") -> str:
        """查询某游戏当前价与史低信息，window 支持 all/12m/3m。game 参数必须为 Steam 官方英文名。
        user_query 为玩家的原始输入（用于自动检测区域），region/currency 留空时自动检测。"""
        if not region or not currency:
            region, currency = _detect_region(user_query or game)
        snapshot = await price_service.fetch_first_available(game_query=game, region=region, currency=currency)
        decision = price_service.evaluate_low(snapshot=snapshot, window=window)
        return (
            f"{snapshot.title} | 当前价 {snapshot.current_price:.2f} {snapshot.currency} | "
            f"史低 {snapshot.historical_low:.2f} {snapshot.currency} | "
            f"来源 {snapshot.source} | 在史低: {decision.is_at_low}"
        )

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
        """推荐同类游戏（标签+相似产品+差评摘要），返回结构化 JSON。game 参数必须为 Steam 官方英文名。"""
        result = await recommender.recommend(game_query=game, top_n=top_n)
        return json.dumps(result, ensure_ascii=False)

    @tool
    async def search_similar_candidates(game: str, top_n: int = 15) -> str:
        """搜索与指定游戏相似的候选游戏列表（轻量级，不含详细描述）。
        返回标题、标签、差评率和来源信号。用于先发现候选，再用 get_game_details 深入调查。
        game 参数必须为 Steam 官方英文名。"""
        result = await recommender.search_candidates(game_query=game, top_n=top_n)
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
    def subscribe_game(
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
        if not region or not currency:
            region, currency = _detect_region(user_query or game)
        channels = json.loads(channels_json)
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

    return [query_low_price, compare_prices, search_similar_candidates, get_game_details, get_game_reviews, recommend_similar_games, subscribe_game, list_subscriptions]
