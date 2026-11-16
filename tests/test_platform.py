"""Auth, Redis degradation, caching, rate limiting, request ids and JSON logs."""

import asyncio
import json
import time
import uuid
from collections.abc import Callable

import pytest
from httpx import AsyncClient

from app.core.security import TokenService
from app.domain.enums import Role
from tests.conftest import AppFactory
from tests.helpers import API, Actor, create_event


async def test_register_login_and_use_the_token(client: AsyncClient, organizer: Actor) -> None:
    creds = {"email": "Fan@Example.com", "password": "correct horse battery"}
    registered = await client.post(f"{API}/auth/register", json=creds)
    assert registered.status_code == 201
    assert registered.json()["email"] == "fan@example.com"
    assert (await client.post(f"{API}/auth/register", json=creds)).status_code == 409

    bad = await client.post(f"{API}/auth/login", json={**creds, "password": "nope"})
    assert bad.status_code == 401
    assert bad.json()["error"]["code"] == "INVALID_CREDENTIALS"
    login = await client.post(f"{API}/auth/login", json=creds)
    assert login.status_code == 200
    token = login.json()["access_token"]

    event = await create_event(organizer)
    res = await client.post(
        f"{API}/events/{event.id}/holds",
        json={"seat_ids": event.seat_ids[:1]},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert res.status_code == 201


@pytest.mark.parametrize(
    "authorization",
    [None, "Bearer not-a-jwt", "Basic Zm9vOmJhcg=="],
)
async def test_buyer_endpoints_require_a_valid_jwt(
    client: AsyncClient, organizer: Actor, authorization: str | None
) -> None:
    event = await create_event(organizer)
    headers = {"Authorization": authorization} if authorization else {}

    res = await client.post(
        f"{API}/events/{event.id}/holds", json={"seat_ids": event.seat_ids[:1]}, headers=headers
    )

    assert res.status_code == 401
    assert res.json()["error"]["code"] == "UNAUTHENTICATED"


async def test_expired_or_foreign_tokens_are_rejected(client: AsyncClient) -> None:
    expired = TokenService(secret=_secret(), algorithm="HS256", ttl_s=1, leeway_s=0)
    token = expired.issue(uuid.uuid4(), Role.BUYER, ttl_s=-10).token
    forged = TokenService(secret="another-secret-0123456789abcdef0123", algorithm="HS256", ttl_s=60)
    for t in (token, forged.issue(uuid.uuid4(), Role.ORGANIZER).token):
        res = await client.get(f"{API}/me/holds", headers={"Authorization": f"Bearer {t}"})
        assert res.status_code == 401


def _secret() -> str:
    from app.core.config import Settings  # noqa: PLC0415

    return Settings(_env_file=None).jwt_secret.get_secret_value()  # type: ignore[call-arg]


async def test_works_without_redis(make_app: AppFactory) -> None:
    """Redis is an optimisation: with it gone, reads and holds still work (fail-open)."""
    async with (
        make_app(
            redis_url="redis://127.0.0.1:1/0", read_cache_ttl_ms=1000, hold_rate_limit_per_minute=5
        ) as running,
        running.client() as client,
    ):
        organizer = Actor.new(client, running.container.tokens, Role.ORGANIZER)
        buyer = Actor.new(client, running.container.tokens)
        event = await create_event(organizer)

        assert (await client.get(f"{API}/events/{event.id}/seats")).status_code == 200
        assert (await buyer.hold(event.id, event.seat_ids[:1])).status_code == 201
        ready = (await client.get("/health/ready")).json()
        assert ready["postgres"] == "ok"
        assert ready["redis"].startswith("degraded")


async def test_seat_map_cache_is_shared_and_bounded_in_staleness(make_app: AppFactory) -> None:
    async with make_app(read_cache_ttl_ms=300) as running, running.client() as client:
        organizer = Actor.new(client, running.container.tokens, Role.ORGANIZER)
        buyer = Actor.new(client, running.container.tokens)
        event = await create_event(organizer)
        url = f"{API}/events/{event.id}/seats"
        first = (await client.get(url)).json()

        hold = (await buyer.hold(event.id, event.seat_ids[:1])).json()
        assert (await client.get(url)).json() == first  # served from the shared snapshot

        started = time.monotonic()
        while (await client.get(url)).json()["seats"][0]["status"] != "held":
            assert time.monotonic() - started < 2, "availability lag must stay < 2 s"
            await asyncio.sleep(0.05)
        assert hold["seats"][0]["seat_id"] == event.seat_ids[0]


async def test_hold_rate_limit(make_app: AppFactory) -> None:
    async with make_app(hold_rate_limit_per_minute=2) as running, running.client() as client:
        organizer = Actor.new(client, running.container.tokens, Role.ORGANIZER)
        buyer = Actor.new(client, running.container.tokens)
        event = await create_event(organizer)

        codes = [(await buyer.hold(event.id, [s])).status_code for s in event.seat_ids[:3]]

        assert codes == [201, 201, 429]
        limited = await buyer.hold(event.id, event.seat_ids[3:4])
        assert limited.json()["error"]["code"] == "RATE_LIMITED"
        assert int(limited.headers["Retry-After"]) >= 1


async def test_request_id_is_propagated(client: AsyncClient, buyer: Actor) -> None:
    generated = await client.get("/health")
    assert len(generated.headers["X-Request-ID"]) == 32

    res = await client.get(
        f"{API}/holds/00000000-0000-0000-0000-000000000000",
        headers={**buyer.headers, "X-Request-ID": "trace-abc-123"},
    )
    assert res.headers["X-Request-ID"] == "trace-abc-123"
    assert res.json()["error"]["request_id"] == "trace-abc-123"


async def test_every_log_line_is_json_with_request_id(
    make_app: AppFactory, capsys: pytest.CaptureFixture[str], actor: Callable[..., Actor]
) -> None:
    async with (
        make_app(log_json=True, access_log=True, log_level="INFO") as running,
        running.client() as client,
    ):
        buyer = Actor.new(client, running.container.tokens)
        capsys.readouterr()
        await client.get(f"{API}/me/holds", headers={**buyer.headers, "X-Request-ID": "rid-42"})

    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]
    access = [entry for entry in lines if entry.get("logger") == "app.access"]
    assert access, lines
    assert access[0]["request_id"] == "rid-42"
    assert access[0]["user_id"] == str(buyer.user_id)
    assert access[0]["status"] == 200
    assert {"timestamp", "level", "event"} <= set(access[0])
