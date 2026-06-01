from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from typing import Any, TypeVar

T = TypeVar("T")


class AsyncRateLimiter:
    """Async token-bucket rate limiter with per-endpoint granularity.

    Usage:
        limiter = AsyncRateLimiter(qps=5.0)
        await limiter.acquire()
        # ... make HTTP request ...
    """

    def __init__(self, qps: float = 5.0, burst: int = 1):
        self._interval = 1.0 / qps if qps > 0 else 0.0
        self._burst = max(1, burst)
        self._tokens = float(self._burst)
        self._last_update = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        """Wait until a token is available."""
        if self._interval <= 0:
            return
        async with self._lock:
            now = time.monotonic()
            elapsed = now - self._last_update
            self._tokens = min(self._burst, self._tokens + elapsed / self._interval)
            self._last_update = now

            if self._tokens < 1.0:
                need = 1.0 - self._tokens
                wait = need * self._interval
                await asyncio.sleep(wait)
                self._tokens = 0.0
                self._last_update = time.monotonic()
            else:
                self._tokens -= 1.0

    async def __aenter__(self) -> AsyncRateLimiter:
        await self.acquire()
        return self

    async def __aexit__(self, *args: object) -> None:
        pass


def rate_limited(qps: float = 5.0):
    """Decorator that rate-limits an async function.

    Creates a per-function limiter instance.
    """
    limiter = AsyncRateLimiter(qps=qps)

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            await limiter.acquire()
            return await func(*args, **kwargs)
        return wrapper
    return decorator
