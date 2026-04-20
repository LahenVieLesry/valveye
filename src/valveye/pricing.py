from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from valveye.data_sources.base import PriceSource
from valveye.domain import PriceSnapshot


@dataclass(slots=True)
class LowPriceDecision:
    snapshot: PriceSnapshot
    is_at_low: bool
    is_new_low: bool
    window: str


class PriceService:
    def __init__(self, sources: list[PriceSource]):
        self.sources = sources

    async def fetch_first_available(self, game_query: str, region: str, currency: str) -> PriceSnapshot:
        last_exc: Exception | None = None
        for source in self.sources:
            try:
                result = await source.fetch_price(game_query=game_query, region=region, currency=currency)
                if result is not None:
                    return result
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                continue
        if last_exc is not None:
            exc_name = type(last_exc).__name__
            raise RuntimeError(f"all price sources failed: {exc_name}: {last_exc!r}") from last_exc
        raise RuntimeError("all price sources returned no result")

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
        return LowPriceDecision(snapshot=snapshot, is_at_low=is_at_low, is_new_low=is_new_low, window=window)
