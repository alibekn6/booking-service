import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from httpx import AsyncClient, Response
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.security import TokenService
from app.domain.enums import Role

API = "/api/v1"


@dataclass
class Actor:
    """A user with a JWT, plus shortcuts for the API calls tests make all the time."""

    client: AsyncClient
    user_id: uuid.UUID
    token: str

    @classmethod
    def new(cls, client: AsyncClient, tokens: TokenService, role: Role = Role.BUYER) -> "Actor":
        user_id = uuid.uuid4()
        return cls(client, user_id, tokens.issue(user_id, role).token)

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}

    async def get(self, path: str, **kw: Any) -> Response:
        return await self.client.get(API + path, headers=self.headers, **kw)

    async def post(
        self, path: str, json: Any = None, headers: dict[str, str] | None = None
    ) -> Response:
        return await self.client.post(
            API + path, json=json, headers={**self.headers, **(headers or {})}
        )

    async def delete(self, path: str) -> Response:
        return await self.client.delete(API + path, headers=self.headers)

    async def hold(
        self,
        event_id: str,
        seat_ids: list[str] | None = None,
        *,
        category_id: str | None = None,
        quantity: int | None = None,
        idempotency_key: str | None = None,
    ) -> Response:
        body: dict[str, Any] = (
            {"seat_ids": seat_ids}
            if seat_ids is not None
            else {"category_id": category_id, "quantity": quantity}
        )
        headers = {"Idempotency-Key": idempotency_key} if idempotency_key else None
        return await self.post(f"/events/{event_id}/holds", body, headers)

    async def confirm(self, hold_id: str) -> Response:
        return await self.post(f"/holds/{hold_id}/confirm")

    async def release(self, hold_id: str) -> Response:
        return await self.delete(f"/holds/{hold_id}")


@dataclass
class EventInfo:
    id: str
    body: dict[str, Any]
    seats: list[dict[str, Any]] = field(default_factory=list)

    def seat(self, row: str, number: int) -> str:
        return next(s["id"] for s in self.seats if s["row"] == row and s["number"] == number)

    def category(self, code: str) -> dict[str, Any]:
        return next(c for c in self.body["categories"] if c["code"] == code)

    @property
    def seat_ids(self) -> list[str]:
        return [s["id"] for s in self.seats]


async def create_event(
    organizer: Actor,
    *,
    rows: int = 2,
    seats_per_row: int = 10,
    sale_opens_in: timedelta | None = None,
) -> EventInfo:
    """Row 1 is VIP (5000), the other rows STD (2000). Sale is open unless told otherwise."""
    now = datetime.now(UTC)
    sale_starts_at = now - timedelta(days=1) if sale_opens_in is None else now + sale_opens_in
    payload = {
        "name": "Test concert",
        "venue": "Test hall",
        "starts_at": (now + timedelta(days=30)).isoformat(),
        "sale_starts_at": sale_starts_at.isoformat(),
        "categories": [
            {"code": "VIP", "name": "VIP", "price": "5000.00"},
            {"code": "STD", "name": "Standard", "price": "2000.00"},
        ],
        "rows": [
            {
                "row": str(r),
                "seat_from": 1,
                "seat_to": seats_per_row,
                "category": "VIP" if r == 1 else "STD",
            }
            for r in range(1, rows + 1)
        ],
    }
    res = await organizer.post("/events", payload)
    assert res.status_code == 201, res.text
    body = res.json()
    seat_map = await organizer.client.get(f"{API}/events/{body['id']}/seats")
    return EventInfo(id=body["id"], body=body, seats=seat_map.json()["seats"])


async def seat_statuses(client: AsyncClient, event_id: str) -> dict[str, str]:
    res = await client.get(f"{API}/events/{event_id}/seats")
    return {s["id"]: s["status"] for s in res.json()["seats"]}


async def travel_back(engine: AsyncEngine, hold_id: str, seconds: float) -> None:
    """Pretend `seconds` have passed for a hold, by moving its timestamps into the past.

    The DB clock is the only clock the service uses, so this is equivalent to waiting.
    """
    async with engine.begin() as conn:
        params = {"id": hold_id, "s": seconds}
        await conn.execute(
            text(
                "UPDATE holds SET created_at = created_at - make_interval(secs => :s),"
                " expires_at = expires_at - make_interval(secs => :s) WHERE id = :id"
            ),
            params,
        )
        await conn.execute(
            text(
                "UPDATE seats SET held_until = held_until - make_interval(secs => :s)"
                " WHERE hold_id = :id AND status = 'held'"
            ),
            params,
        )


async def scalar(engine: AsyncEngine, sql: str, **params: Any) -> Any:
    async with engine.connect() as conn:
        return (await conn.execute(text(sql), params)).scalar_one()
