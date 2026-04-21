from __future__ import annotations

import argparse
import asyncio
import json

from valveye.agent_tools import build_tools
from valveye.config import settings
from valveye.data_sources.cheapshark import CheapSharkSource
from valveye.data_sources.itad import ITADSource
from valveye.data_sources.steamdb import SteamDBSource
from valveye.notifications import Notifier
from valveye.pricing import PriceService
from valveye.recommendation import Recommender
from valveye.scheduler import PriceCheckScheduler
from valveye.subscriptions import SubscriptionRepository


def parse_channels_arg(raw_channels: str) -> list[dict]:
    try:
        parsed = json.loads(raw_channels)
    except json.JSONDecodeError as exc:
        raise ValueError(f"--channels 必须是合法 JSON：{exc}") from exc

    if isinstance(parsed, dict):
        parsed = [parsed]
    if not isinstance(parsed, list):
        raise ValueError("--channels 必须是 JSON 对象或 JSON 数组")

    normalized: list[dict] = []
    for i, item in enumerate(parsed):
        if isinstance(item, str):
            try:
                item = json.loads(item)
            except json.JSONDecodeError as exc:
                raise ValueError(f"--channels 第 {i + 1} 项不是合法 JSON 对象字符串：{exc}") from exc
        if not isinstance(item, dict):
            raise ValueError(f"--channels 第 {i + 1} 项必须是 JSON 对象")
        normalized.append(item)

    return normalized


def build_services():
    repo = SubscriptionRepository(db_path=settings.sqlite_path)
    sources = [ITADSource(), SteamDBSource(), CheapSharkSource()]
    # sources = [SteamDBSource()]
    price_service = PriceService(sources=sources)
    recommender = Recommender()
    notifier = Notifier()
    scheduler = PriceCheckScheduler(repo=repo, price_service=price_service, notifier=notifier)
    tools = build_tools(price_service=price_service, recommender=recommender, repo=repo)
    return repo, price_service, recommender, scheduler, tools


async def _run(args: argparse.Namespace) -> int:
    repo, price_service, recommender, scheduler, _tools = build_services()

    if args.command == "query":
        snapshot = await price_service.fetch_first_available(args.game, args.region, args.currency)
        decision = price_service.evaluate_low(snapshot, args.window)
        print(
            json.dumps(
                {
                    "title": snapshot.title,
                    "source": snapshot.source,
                    "current_price": snapshot.current_price,
                    "historical_low": snapshot.historical_low,
                    "currency": snapshot.currency,
                    "is_at_low": decision.is_at_low,
                    "is_new_low": decision.is_new_low,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    if args.command == "recommend":
        rows = await recommender.recommend(args.game, args.top)
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0

    if args.command == "subscribe":
        try:
            channels = parse_channels_arg(args.channels)
        except ValueError as exc:
            print(f"参数错误: {exc}")
            return 2
        sub_id, created = repo.add(
            user_id=args.user,
            game_query=args.game,
            window=args.window,
            region=args.region,
            currency=args.currency,
            channels=channels,
        )
        if created:
            print(f"订阅创建成功: {sub_id}")
        else:
            print(f"订阅已存在: {sub_id}")
        return 0

    if args.command == "list":
        rows = repo.list_active()
        print(
            json.dumps(
                [
                    {
                        "id": r.id,
                        "user_id": r.user_id,
                        "game": r.game_query,
                        "window": r.window,
                        "region": r.region,
                        "currency": r.currency,
                        "channels": r.channels,
                    }
                    for r in rows
                ],
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    if args.command == "check-once":
        await scheduler.run_once()
        print("已执行一次订阅价格检测")
        return 0

    if args.command == "scheduler":
        scheduler.start()
        print("调度器已启动，按 Ctrl+C 退出")
        while True:
            await asyncio.sleep(3600)

    return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Valveye Steam Agent CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    q = sub.add_parser("query", help="查询游戏史低")
    q.add_argument("--game", required=True)
    q.add_argument("--region", default="CN")
    q.add_argument("--currency", default="CNY")
    q.add_argument("--window", default="all", choices=["all", "12m", "3m"])

    r = sub.add_parser("recommend", help="推荐同类游戏")
    r.add_argument("--game", required=True)
    r.add_argument("--top", type=int, default=15)

    s = sub.add_parser("subscribe", help="订阅游戏史低提醒")
    s.add_argument("--user", required=True)
    s.add_argument("--game", required=True)
    s.add_argument("--region", default="CN")
    s.add_argument("--currency", default="CNY")
    s.add_argument("--window", default="all", choices=["all", "12m", "3m"])
    s.add_argument(
        "--channels",
        required=True,
        help='JSON 数组，如: [{"type":"email","to":"you@example.com"}]',
    )

    sub.add_parser("list", help="查看订阅")
    sub.add_parser("check-once", help="立即执行一次检测")
    sub.add_parser("scheduler", help="启动定时检测")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return asyncio.run(_run(args))
