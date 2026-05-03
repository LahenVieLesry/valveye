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


@dataclass(slots=True)
class GameProfile:
    app_id: int
    title: str
    app_type: str
    tags: list[str] = field(default_factory=list)
    relevance_tags: list[str] = field(default_factory=list)
    tags_weighted: dict[str, int] = field(default_factory=dict)
    description: str = ""
    detailed_description: str = ""
    developer: str = ""
    publisher: str = ""
    release_date: str = ""
    platforms: dict[str, bool] = field(default_factory=dict)
    website: str = ""
    metacritic_score: int | None = None
    thumb: str | None = None
    negative_ratio: float | None = None
    positive_count: int = 0
    negative_count: int = 0


@dataclass(slots=True)
class RecommendationReason:
    tag_overlap: list[str] = field(default_factory=list)
    matched_signals: list[str] = field(default_factory=list)
    downside: str = ""


@dataclass(slots=True)
class RecommendationItem:
    title: str
    app_id: int
    score: float
    tags: list[str] = field(default_factory=list)
    similarity_breakdown: dict[str, float] = field(default_factory=dict)
    reason: RecommendationReason = field(default_factory=RecommendationReason)
    source_signals: list[str] = field(default_factory=list)
    thumb: str | None = None

    def to_dict(self) -> dict:
        return {
            "title": self.title,
            "app_id": self.app_id,
            "score": round(self.score, 4),
            "tags": self.tags,
            "similarity_breakdown": {
                key: round(float(value), 4) for key, value in self.similarity_breakdown.items()
            },
            "reason": {
                "tag_overlap": self.reason.tag_overlap,
                "matched_signals": self.reason.matched_signals,
                "downside": self.reason.downside,
            },
            "source_signals": self.source_signals,
            "thumb": self.thumb,
        }
