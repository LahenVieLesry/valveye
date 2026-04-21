from __future__ import annotations

import json
from datetime import UTC, datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from valveye.config import settings
from valveye.notifications import Notifier
from valveye.pricing import PriceService
from valveye.subscriptions import SubscriptionRepository
from valveye.time_utils import local_hhmm


def _normalize_channel(channel: dict | str) -> dict:
    if isinstance(channel, dict):
        return channel
    if isinstance(channel, str):
        parsed = json.loads(channel)
        if isinstance(parsed, dict):
            return parsed
    raise ValueError("channel 必须为字典或可解析为字典的 JSON 字符串")


class PriceCheckScheduler:
    def __init__(
        self,
        repo: SubscriptionRepository,
        price_service: PriceService,
        notifier: Notifier,
    ):
        self.repo = repo
        self.price_service = price_service
        self.notifier = notifier
        self.scheduler = AsyncIOScheduler(timezone="UTC")
        self._last_run_local_date: str | None = None

    def start(self) -> None:
        self.scheduler.add_job(self.run_if_due, "interval", minutes=15, id="price-check")
        self.scheduler.start()

    async def run_if_due(self) -> None:
        now = datetime.now(tz=UTC)
        hour, minute, local_date = local_hhmm(now)
        if local_date == self._last_run_local_date:
            return

        target_h = settings.check_local_hour
        target_m = settings.check_local_minute
        if hour != target_h or abs(minute - target_m) > 7:
            return

        await self.run_once()
        self._last_run_local_date = local_date

    async def run_once(self) -> None:
        subscriptions = self.repo.list_active()
        for sub in subscriptions:
            snapshot = await self.price_service.fetch_first_available(
                game_query=sub.game_query,
                region=sub.region,
                currency=sub.currency,
            )
            decision = self.price_service.evaluate_low(
                snapshot=snapshot,
                window=sub.window,
                known_notified_low=sub.last_notified_low,
            )
            should_notify = decision.is_at_low or decision.is_new_low
            if not should_notify:
                continue

            tag = "新史低" if decision.is_new_low else "触及史低"
            msg = (
                f"【{tag}】{snapshot.title}\n"
                f"当前价: {snapshot.current_price:.2f} {snapshot.currency}\n"
                f"史低价: {snapshot.historical_low:.2f} {snapshot.currency}\n"
                f"来源: {snapshot.source}\n"
                f"口径: {sub.window}"
            )

            for channel in sub.channels:
                try:
                    normalized_channel = _normalize_channel(channel)
                    await self.notifier.send(channel=normalized_channel, message=msg)
                except Exception:
                    # 通道失败不影响其他通道
                    continue

            self.repo.mark_notified(sub.id, snapshot.historical_low)
