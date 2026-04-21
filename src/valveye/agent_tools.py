from __future__ import annotations

import json

from langchain_core.tools import tool

from valveye.pricing import PriceService
from valveye.recommendation import Recommender
from valveye.subscriptions import SubscriptionRepository


def build_tools(price_service: PriceService, recommender: Recommender, repo: SubscriptionRepository):
    @tool
    async def query_low_price(game: str, region: str = "US", currency: str = "USD", window: str = "all") -> str:
        """查询某游戏当前价与史低信息，window 支持 all/12m/3m。"""
        snapshot = await price_service.fetch_first_available(game_query=game, region=region, currency=currency)
        decision = price_service.evaluate_low(snapshot=snapshot, window=window)
        return (
            f"{snapshot.title} | 当前价 {snapshot.current_price:.2f} {snapshot.currency} | "
            f"史低 {snapshot.historical_low:.2f} {snapshot.currency} | "
            f"来源 {snapshot.source} | 在史低: {decision.is_at_low}"
        )

    @tool
    async def recommend_similar_games(game: str, top_n: int = 5) -> str:
        """推荐同类游戏（标签+相似产品+差评摘要），返回结构化 JSON。"""
        result = await recommender.recommend(game_query=game, top_n=top_n)
        return json.dumps(result, ensure_ascii=False)

    @tool
    def subscribe_game(
        user_id: str,
        game: str,
        channels_json: str,
        window: str = "all",
        region: str = "US",
        currency: str = "USD",
    ) -> str:
        """订阅游戏价格提醒，channels_json 是通知渠道 JSON 数组。"""
        channels = json.loads(channels_json)
        sub_id = repo.add(
            user_id=user_id,
            game_query=game,
            window=window,
            region=region,
            currency=currency,
            channels=channels,
        )
        return f"订阅成功，ID={sub_id}"

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

    return [query_low_price, recommend_similar_games, subscribe_game, list_subscriptions]
