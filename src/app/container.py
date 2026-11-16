"""Composition root: builds long-lived infrastructure once per process.

Request-scoped objects (session, repositories, services) are assembled from this in
`app.api.deps`; nothing else reaches into `app.state`.
"""

from dataclasses import dataclass
from datetime import timedelta
from typing import Self

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.core.security import PasswordHasher, TokenService
from app.db.session import create_engine, create_session_factory
from app.infra.cache import ReadCache
from app.infra.rate_limit import RateLimiter
from app.infra.redis import create_redis
from app.services.booking import BookingPolicy


@dataclass(frozen=True, slots=True)
class Container:
    settings: Settings
    engine: AsyncEngine
    session_factory: async_sessionmaker[AsyncSession]
    redis: Redis
    cache: ReadCache
    rate_limiter: RateLimiter
    tokens: TokenService
    hasher: PasswordHasher
    booking_policy: BookingPolicy

    @classmethod
    def create(cls, settings: Settings) -> Self:
        engine = create_engine(settings)
        redis = create_redis(settings)
        return cls(
            settings=settings,
            engine=engine,
            session_factory=create_session_factory(engine),
            redis=redis,
            cache=ReadCache(redis, settings.read_cache_ttl_ms, settings.read_cache_local_ttl_ms),
            rate_limiter=RateLimiter(redis, settings.hold_rate_limit_per_minute),
            tokens=TokenService(
                secret=settings.jwt_secret.get_secret_value(),
                algorithm=settings.jwt_algorithm,
                ttl_s=settings.jwt_ttl_s,
                issuer=settings.jwt_issuer,
                audience=settings.jwt_audience,
            ),
            hasher=PasswordHasher(),
            booking_policy=BookingPolicy(
                hold_ttl=timedelta(seconds=settings.hold_ttl_s),
                max_seats_per_buyer=settings.max_seats_per_buyer,
            ),
        )

    async def aclose(self) -> None:
        await self.redis.aclose()
        await self.engine.dispose()
