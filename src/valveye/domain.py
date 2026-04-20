from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(slots=True)
class PricePoint:
    price: float
    timestamp: datetime


@dataclass(slots=True)
class PriceSnapshot:
    source: str
    game_id: str
    title: str
    currency: str
    current_price: float
    historical_low: float
    historical_low_at: datetime | None = None
    history: list[PricePoint] = field(default_factory=list)
    store: str | None = None


@dataclass(slots=True)
class Subscription:
    id: int
    user_id: str
    game_query: str
    window: str
    region: str
    currency: str
    channels: list[dict]
    active: bool
    last_notified_low: float | None
    last_notified_at: datetime | None
