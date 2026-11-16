"""Auth, Redis degradation, caching, rate limiting, request ids and JSON logs."""

import uuid

import pytest
from httpx import AsyncClient

from app.core.security import TokenService
from app.domain.enums import Role
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
