from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Awaitable, Callable
from typing import TypeVar

T = TypeVar("T")


class AsyncTokenBucket:
    """Async token bucket with bounded capacity; share one per broker/API key."""

    def __init__(self, rate: float, capacity: int):
        if rate <= 0 or capacity < 1:
            raise ValueError("rate and capacity must be positive")
        self.rate, self.capacity = rate, float(capacity)
        self._tokens = float(capacity)
        self._updated = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self, tokens: float = 1.0) -> None:
        if tokens <= 0 or tokens > self.capacity:
            raise ValueError("requested tokens must be in (0, capacity]")
        while True:
            async with self._lock:
                now = time.monotonic()
                self._tokens = min(self.capacity, self._tokens + (now - self._updated) * self.rate)
                self._updated = now
                if self._tokens >= tokens:
                    self._tokens -= tokens
                    return
                delay = (tokens - self._tokens) / self.rate
            await asyncio.sleep(delay)


async def with_backoff(operation: Callable[[], Awaitable[T]], *, attempts: int = 5,
                       base_delay: float = 0.5, max_delay: float = 20.0,
                       retry_if: Callable[[Exception], bool] | None = None) -> T:
    """Retry transient failures only; use jitter and bounded exponential backoff."""
    for attempt in range(attempts):
        try:
            return await operation()
        except Exception as exc:
            if attempt + 1 >= attempts or (retry_if is not None and not retry_if(exc)):
                raise
            await asyncio.sleep(min(max_delay, base_delay * 2**attempt) * random.uniform(0.75, 1.25))
    raise RuntimeError("unreachable")
