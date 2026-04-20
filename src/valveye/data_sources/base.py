from __future__ import annotations

from abc import ABC, abstractmethod

from valveye.domain import PriceSnapshot


class PriceSourceError(RuntimeError):
    pass


class PriceSource(ABC):
    source_name: str

    @abstractmethod
    async def fetch_price(self, game_query: str, region: str, currency: str) -> PriceSnapshot | None:
        raise NotImplementedError
