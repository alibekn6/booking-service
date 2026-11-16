"""Test infrastructure: real Postgres + Redis from docker compose. Never SQLite.

Row locks, READ COMMITTED re-checks, SKIP LOCKED and advisory locks are exactly what is
under test here; an in-memory substitute would make the race tests meaningless.

    docker compose up -d postgres redis && uv run pytest
"""

import asyncio
import contextlib
import os
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass

import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from starlette.types import ASGIApp

from app.container import Container
from app.core.config import Settings
from app.db.migrate import upgrade_head
from app.domain.enums import Role
from app.main import create_app
from tests.helpers import Actor

TEST_DATABASE_URL = os.getenv(
    "TEST_DATABASE_URL", "postgresql+asyncpg://booking:booking@localhost:5433/booking_test"
)
TEST_REDIS_URL = os.getenv("TEST_REDIS_URL", "redis://localhost:6380/15")
TABLES = "tickets, orders, hold_seats, seats, holds, seat_categories, events, users"


async def _recreate_database(url: str) -> None:
    base, name = url.rsplit("/", 1)
    engine = create_async_engine(f"{base}/postgres", isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as conn:
            await conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
            await conn.execute(text(f'CREATE DATABASE "{name}"'))
    finally:
        await engine.dispose()


@pytest.fixture(scope="session")
def database_url() -> str:
    asyncio.run(_recreate_database(TEST_DATABASE_URL))
    upgrade_head(TEST_DATABASE_URL)  # the real Alembic migrations: covered by every test
    return TEST_DATABASE_URL


@pytest.fixture
def settings(database_url: str) -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        env="test",
        database_url=database_url,  # type: ignore[arg-type]
        redis_url=TEST_REDIS_URL,  # type: ignore[arg-type]
        log_json=False,
        access_log=False,
        log_level="WARNING",
        read_cache_ttl_ms=0,  # tests read the truth; cache behaviour has its own tests
        hold_rate_limit_per_minute=0,
        db_pool_size=60,  # enough for the 50-way races
    )


@dataclass(frozen=True)
class RunningApp:
    asgi: ASGIApp
    container: Container

    def client(self) -> AsyncClient:
        return AsyncClient(transport=ASGITransport(app=self.asgi), base_url="http://test")


AppFactory = Callable[..., contextlib.AbstractAsyncContextManager[RunningApp]]


@pytest.fixture
def make_app(settings: Settings) -> AppFactory:
    """Factory for a started app (optional settings overrides) on a clean database."""

    @contextlib.asynccontextmanager
    async def factory(**overrides: object) -> AsyncIterator[RunningApp]:
        application = create_app(settings.model_copy(update=overrides))
        async with LifespanManager(application) as manager:
            container: Container = application.state.container
            async with container.engine.begin() as conn:
                await conn.execute(text(f"TRUNCATE {TABLES} RESTART IDENTITY CASCADE"))
            with contextlib.suppress(Exception):
                await container.redis.flushdb()
            yield RunningApp(asgi=manager.app, container=container)

    return factory


@pytest.fixture
async def app(make_app: AppFactory) -> AsyncIterator[RunningApp]:
    async with make_app() as running:
        yield running


@pytest.fixture
def container(app: RunningApp) -> Container:
    return app.container


@pytest.fixture
def engine(container: Container) -> AsyncEngine:
    return container.engine


@pytest.fixture
async def client(app: RunningApp) -> AsyncIterator[AsyncClient]:
    async with app.client() as c:
        yield c


@pytest.fixture
def actor(client: AsyncClient, container: Container) -> Callable[..., Actor]:
    def make(role: Role = Role.BUYER) -> Actor:
        return Actor.new(client, container.tokens, role)

    return make


@pytest.fixture
def organizer(actor: Callable[..., Actor]) -> Actor:
    return actor(Role.ORGANIZER)


@pytest.fixture
def buyer(actor: Callable[..., Actor]) -> Actor:
    return actor()
