"""Fixed-window rate limiter. Redis in real deployments, in-memory for local/tests."""

import time
from typing import Protocol

from mysite.config import get_settings


class RateLimiter(Protocol):
    async def hit(self, key: str, limit: int, window: int) -> int | None:
        """Count one attempt. Returns seconds to wait if the limit is exceeded, else None."""

    async def reset(self, key: str) -> None: ...


class MemoryRateLimiter:
    def __init__(self) -> None:
        self._data: dict[str, tuple[int, float]] = {}

    async def hit(self, key: str, limit: int, window: int) -> int | None:
        now = time.monotonic()
        count, reset_at = self._data.get(key, (0, now + window))
        if now >= reset_at:
            count, reset_at = 0, now + window
        count += 1
        self._data[key] = (count, reset_at)
        return max(1, int(reset_at - now)) if count > limit else None

    async def reset(self, key: str) -> None:
        self._data.pop(key, None)

    def clear(self) -> None:
        self._data.clear()


class RedisRateLimiter:
    def __init__(self, url: str) -> None:
        from redis.asyncio import Redis

        self._redis = Redis.from_url(url)

    async def hit(self, key: str, limit: int, window: int) -> int | None:
        key = f"rl:{key}"
        count = await self._redis.incr(key)
        if count == 1:
            await self._redis.expire(key, window)
        if count > limit:
            ttl = await self._redis.ttl(key)
            if ttl < 0:  # key lost its expiry (crash between INCR and EXPIRE)
                await self._redis.expire(key, window)
                ttl = window
            return max(1, ttl)
        return None

    async def reset(self, key: str) -> None:
        await self._redis.delete(f"rl:{key}")


_limiter: RateLimiter | None = None


def get_rate_limiter() -> RateLimiter:
    global _limiter
    if _limiter is None:
        url = get_settings().redis_url
        _limiter = RedisRateLimiter(url) if url else MemoryRateLimiter()
    return _limiter
