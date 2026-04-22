from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import dataclass

from valveye.config import settings
from valveye.data_sources.cheapshark import CheapSharkSource
from valveye.data_sources.itad import ITADSource
from valveye.data_sources.steamdb import SteamDBSource
from valveye.notifications import Notifier
from valveye.pricing import PriceService
from valveye.subscriptions import SubscriptionRepository


def _normalize_channel(channel: dict | str) -> dict:
    if isinstance(channel, dict):
        return channel
    if isinstance(channel, str):
        parsed = json.loads(channel)
        if isinstance(parsed, dict):
            return parsed
    raise ValueError("channel 必须为字典或可解析为字典的 JSON 字符串")


@dataclass(slots=True)
class SelectionSpec:
    top_n: int | None = None
    ids: list[int] | None = None
    all_active: bool = False


def build_services() -> tuple[SubscriptionRepository, PriceService, Notifier]:
    repo = SubscriptionRepository(db_path=settings.sqlite_path)
    sources = [ITADSource(), SteamDBSource(), CheapSharkSource()]
    price_service = PriceService(sources=sources)
    notifier = Notifier()
    return repo, price_service, notifier


def parse_id_list(raw_ids: str) -> list[int]:
    ids: list[int] = []
    for chunk in raw_ids.split(","):
        item = chunk.strip()
        if not item:
            continue
        try:
            ids.append(int(item))
        except ValueError as exc:
            raise ValueError(f"无效编号: {item!r}") from exc
    return ids


def select_subscriptions(subscriptions, spec: SelectionSpec):
    if spec.ids:
        wanted = set(spec.ids)
        selected = [sub for sub in subscriptions if sub.id in wanted]
        selected.sort(key=lambda sub: spec.ids.index(sub.id))
        return selected

    if spec.top_n is not None:
        return list(subscriptions[: max(spec.top_n, 0)])

    if spec.all_active or (spec.top_n is None and not spec.ids):
        return list(subscriptions)

    return []


def build_message(snapshot, window: str, tag: str) -> str:
    return (
        f"【{tag}】{snapshot.title}\n"
        f"当前价: {snapshot.current_price:.2f} {snapshot.currency}\n"
        f"史低价: {snapshot.historical_low:.2f} {snapshot.currency}\n"
        f"来源: {snapshot.source}\n"
        f"口径: {window}"
    )


async def notify_selected_subscriptions(spec: SelectionSpec) -> int:
    repo, price_service, notifier = build_services()

    active_subscriptions = repo.list_active()
    selected = select_subscriptions(active_subscriptions, spec)
    if not selected:
        print("没有找到符合条件的活跃订阅。")
        return 0

    print(f"将处理 {len(selected)} 条订阅。")

    sent_count = 0
    for sub in selected:
        try:
            snapshot = await price_service.fetch_first_available(
                game_query=sub.game_query,
                region=sub.region,
                currency=sub.currency,
            )
            decision = price_service.evaluate_low(
                snapshot=snapshot,
                window=sub.window,
                known_notified_low=sub.last_notified_low,
            )
        except Exception as exc:  # noqa: BLE001
            print(f"订阅 #{sub.id} 价格查询失败：{exc}")
            continue

        should_notify = decision.is_at_low or decision.is_new_low
        if not should_notify:
            print(f"跳过订阅 #{sub.id}：当前未触发史低条件。")
            continue

        tag = "新史低" if decision.is_new_low else "触及史低"
        msg = build_message(snapshot=snapshot, window=sub.window, tag=tag)

        success_channels = 0
        for channel in sub.channels:
            try:
                normalized_channel = _normalize_channel(channel)
                await notifier.send(channel=normalized_channel, message=msg)
                success_channels += 1
            except Exception as exc:  # noqa: BLE001
                print(f"订阅 #{sub.id} 的通道发送失败：{exc}")

        if success_channels == 0:
            print(f"订阅 #{sub.id} 所有通道发送失败，未标记已通知。")
            continue

        repo.mark_notified(sub.id, snapshot.historical_low)
        sent_count += 1
        print(f"已发送订阅 #{sub.id}：{sub.user_id} / {sub.game_query}（成功通道 {success_channels}/{len(sub.channels)}）")

    return sent_count


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="批量触发史低通知")
    parser.add_argument(
        "--top-n",
        type=int,
        default=None,
        help="只处理前 N 条活跃订阅（按数据库默认顺序）",
    )
    parser.add_argument(
        "--ids",
        default=None,
        help="只处理指定订阅编号，逗号分隔，例如: 1,3,4",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="处理全部活跃订阅",
    )
    return parser


def _build_selection_spec(args: argparse.Namespace) -> SelectionSpec:
    ids = parse_id_list(args.ids) if args.ids else None
    if args.top_n is not None and ids is not None:
        raise ValueError("--top-n 和 --ids 不能同时使用")
    if args.all and (args.top_n is not None or ids is not None):
        raise ValueError("--all 不能与 --top-n 或 --ids 同时使用")
    return SelectionSpec(top_n=args.top_n, ids=ids, all_active=bool(args.all or (args.top_n is None and ids is None)))


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        spec = _build_selection_spec(args)
    except ValueError as exc:
        parser.error(str(exc))
        return 2

    sent = asyncio.run(notify_selected_subscriptions(spec))
    print(f"完成，实际发送通知的订阅数: {sent}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())