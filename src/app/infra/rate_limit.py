"""Per-buyer fixed-window rate limit in Redis (anti-bot for hold creation).

Fails open: with Redis down requests pass, since correctness is guaranteed by Postgres.
"""

import time

from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.core.errors import RateLimitedError
from app.core.logging import get_logger

log = get_logger(__name__)

WINDOW_S = 60


class RateLimiter:
    def __init__(self, redis: Redis, limit_per_minute: int) -> None:
        self._redis = redis
        self._limit = limit_per_minute

    async def hit(self, scope: str, ident: str) -> None:
        if self._limit <= 0:
            return
        now = time.time()
        key = f"rl:{scope}:{ident}:{int(now // WINDOW_S)}"
        try:
            async with self._redis.pipeline(transaction=False) as pipe:
                pipe.incr(key)
                pipe.expire(key, WINDOW_S + 1)
                count, _ = await pipe.execute()
        except (RedisError, OSError) as exc:
            log.warning("rate_limiter_unavailable", error=repr(exc))
            return
        if count > self._limit:
            raise RateLimitedError(retry_after_s=max(1, int(WINDOW_S - now % WINDOW_S)))
