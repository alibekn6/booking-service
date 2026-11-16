from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import Settings


def create_engine(settings: Settings, *, application_name: str = "booking-api") -> AsyncEngine:
    return create_async_engine(
        str(settings.database_url),
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_timeout=settings.db_pool_timeout_s,
        pool_recycle=1800,
        connect_args={
            "server_settings": {
                "application_name": application_name,
                "timezone": "UTC",
                # Bounded waits: under a rush a fast, retryable 503 beats a pile-up.
                "statement_timeout": str(settings.db_statement_timeout_ms),
                "lock_timeout": str(settings.db_lock_timeout_ms),
                "idle_in_transaction_session_timeout": "15000",
            },
        },
    )


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
