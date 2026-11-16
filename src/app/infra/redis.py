from redis.asyncio import Redis

from app.core.config import Settings


def create_redis(settings: Settings) -> Redis:
    # Short timeouts: a sick Redis must not stall requests; callers fall back to Postgres.
    return Redis.from_url(
        str(settings.redis_url),
        socket_timeout=0.25,
        socket_connect_timeout=0.25,
        health_check_interval=30,
    )
