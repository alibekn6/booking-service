"""Holding, releasing and listing seats, and the rules around holds."""

from collections.abc import Callable
from datetime import datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.helpers import Actor, create_event, seat_statuses, travel_back


async def test_hold_specific_seats(organizer: Actor, buyer: Actor, client: AsyncClient) -> None:
    event = await create_event(organizer)
    seats = [event.seat("1", 1), event.seat("2", 1)]

    res = await buyer.hold(event.id, seats)

    assert res.status_code == 201, res.text
    hold = res.json()
    assert hold["status"] == "active"
    assert {s["seat_id"] for s in hold["seats"]} == set(seats)
    assert hold["total"] == "7000.00"  # VIP 5000 + STD 2000, fixed at hold time
    ttl = datetime.fromisoformat(hold["expires_at"]) - datetime.fromisoformat(hold["created_at"])
    assert ttl == timedelta(minutes=10)
    statuses = await seat_statuses(client, event.id)
    assert statuses[seats[0]] == statuses[seats[1]] == "held"


async def test_hold_is_all_or_nothing(
    organizer: Actor, actor: Callable[..., Actor], client: AsyncClient
) -> None:
    event = await create_event(organizer)
    taken, free = event.seat("1", 1), event.seat("1", 2)
    await actor().hold(event.id, [taken])

    res = await actor().hold(event.id, [free, taken])

    assert res.status_code == 409
    error = res.json()["error"]
    assert error["code"] == "SEATS_UNAVAILABLE"
    assert error["details"]["seat_ids"] == [taken]  # tells which seat is the problem
    assert (await seat_statuses(client, event.id))[free] == "available"  # all or nothing


async def test_sale_not_started(organizer: Actor, buyer: Actor, engine: AsyncEngine) -> None:
    event = await create_event(organizer, sale_opens_in=timedelta(hours=1))

    res = await buyer.hold(event.id, [event.seat("1", 1)])

    assert res.status_code == 409
    assert res.json()["error"]["code"] == "SALE_NOT_STARTED"
    assert "sale_starts_at" in res.json()["error"]["details"]

    async with engine.begin() as conn:  # the clock reaches the start time
        await conn.execute(
            text("UPDATE events SET sale_starts_at = now() - interval '1 second' WHERE id = :id"),
            {"id": event.id},
        )
    assert (await buyer.hold(event.id, [event.seat("1", 1)])).status_code == 201


async def test_hold_any_seats_in_category(organizer: Actor, buyer: Actor) -> None:
    event = await create_event(organizer, rows=3, seats_per_row=10)
    std = event.category("STD")

    res = await buyer.hold(event.id, category_id=std["id"], quantity=3)

    assert res.status_code == 201
    seats = res.json()["seats"]
    assert {s["category_id"] for s in seats} == {std["id"]}
    # best seats first, in the organizer's order
    assert [(s["row"], s["number"]) for s in seats] == [("2", 1), ("2", 2), ("2", 3)]


async def test_hold_any_seats_not_enough(organizer: Actor, actor: Callable[..., Actor]) -> None:
    event = await create_event(organizer, rows=2, seats_per_row=3)
    vip = event.category("VIP")["id"]
    await actor().hold(event.id, category_id=vip, quantity=2)

    res = await actor().hold(event.id, category_id=vip, quantity=2)

    assert res.status_code == 409
    assert res.json()["error"]["code"] == "NOT_ENOUGH_SEATS"


@pytest.mark.parametrize(
    ("body", "code"),
    [
        ({"seat_ids": []}, "VALIDATION_ERROR"),
        ({"category_id": "00000000-0000-0000-0000-000000000000"}, "VALIDATION_ERROR"),
        (
            {"quantity": 5, "category_id": "00000000-0000-0000-0000-000000000000"},
            "VALIDATION_ERROR",
        ),
        ({"seat_ids": ["00000000-0000-0000-0000-000000000000"]}, "INVALID_SEATS"),
        (
            {"quantity": 1, "category_id": "00000000-0000-0000-0000-000000000000"},
            "INVALID_CATEGORY",
        ),
    ],
)
async def test_hold_validation(
    organizer: Actor, buyer: Actor, body: dict[str, object], code: str
) -> None:
    event = await create_event(organizer)

    res = await buyer.post(f"/events/{event.id}/holds", body)

    assert res.status_code == 422
    assert res.json()["error"]["code"] == code


async def test_hold_rejects_too_many_or_duplicate_seats(organizer: Actor, buyer: Actor) -> None:
    event = await create_event(organizer)
    five = event.seat_ids[:5]
    assert (await buyer.hold(event.id, five)).status_code == 422  # 1..4 seats per request
    assert (await buyer.hold(event.id, [five[0], five[0]])).status_code == 422


async def test_seat_of_another_event_is_invalid(organizer: Actor, buyer: Actor) -> None:
    event, other = await create_event(organizer), await create_event(organizer)

    res = await buyer.hold(event.id, [other.seat_ids[0]])

    assert res.status_code == 422
    assert res.json()["error"]["code"] == "INVALID_SEATS"


async def test_limit_counts_holds_and_purchases_per_event(organizer: Actor, buyer: Actor) -> None:
    event, other_event = await create_event(organizer), await create_event(organizer)
    bought = (await buyer.hold(event.id, event.seat_ids[:2])).json()
    await buyer.confirm(bought["id"])
    held = (await buyer.hold(event.id, event.seat_ids[2:4])).json()

    over = await buyer.hold(event.id, [event.seat_ids[4]])
    assert over.status_code == 409
    assert over.json()["error"]["code"] == "HOLD_LIMIT_EXCEEDED"
    assert over.json()["error"]["details"] == {"limit": 4, "owned": 4, "requested": 1}

    assert (await buyer.hold(other_event.id, other_event.seat_ids[:4])).status_code == 201
    await buyer.release(held["id"])  # releasing gives the quota back
    assert (await buyer.hold(event.id, event.seat_ids[4:6])).status_code == 201


async def test_release_hold(organizer: Actor, buyer: Actor, client: AsyncClient) -> None:
    event = await create_event(organizer)
    seats = event.seat_ids[:2]
    hold = (await buyer.hold(event.id, seats)).json()

    assert (await buyer.release(hold["id"])).status_code == 204
    assert (await buyer.release(hold["id"])).status_code == 204  # idempotent

    statuses = await seat_statuses(client, event.id)
    assert {statuses[s] for s in seats} == {"available"}
    assert (await buyer.get(f"/holds/{hold['id']}")).json()["status"] == "released"
    confirm = await buyer.confirm(hold["id"])
    assert confirm.status_code == 409
    assert confirm.json()["error"]["code"] == "HOLD_RELEASED"


async def test_cannot_release_a_purchase(organizer: Actor, buyer: Actor) -> None:
    event = await create_event(organizer)
    hold = (await buyer.hold(event.id, event.seat_ids[:1])).json()
    await buyer.confirm(hold["id"])

    res = await buyer.release(hold["id"])

    assert res.status_code == 409
    assert res.json()["error"]["code"] == "HOLD_ALREADY_CONFIRMED"


async def test_holds_are_private(
    organizer: Actor, buyer: Actor, actor: Callable[..., Actor]
) -> None:
    event = await create_event(organizer)
    hold = (await buyer.hold(event.id, event.seat_ids[:1])).json()
    stranger = actor()

    for res in (
        await stranger.get(f"/holds/{hold['id']}"),
        await stranger.confirm(hold["id"]),
        await stranger.release(hold["id"]),
    ):
        assert res.status_code == 404
        assert res.json()["error"]["code"] == "HOLD_NOT_FOUND"


async def test_my_holds_and_orders(
    organizer: Actor, buyer: Actor, actor: Callable[..., Actor]
) -> None:
    event = await create_event(organizer)
    first = (await buyer.hold(event.id, event.seat_ids[:1])).json()
    second = (await buyer.hold(event.id, event.seat_ids[1:3])).json()
    order = (await buyer.confirm(first["id"])).json()
    await actor().hold(event.id, event.seat_ids[5:6])  # someone else's

    holds = (await buyer.get("/me/holds")).json()
    orders = (await buyer.get("/me/orders", params={"event_id": event.id})).json()

    assert [(h["id"], h["status"]) for h in holds] == [
        (second["id"], "active"),
        (first["id"], "confirmed"),
    ]
    assert [o["id"] for o in orders] == [order["id"]]
    assert orders[0]["tickets"][0]["seat_id"] == event.seat_ids[0]
    assert (await buyer.get(f"/orders/{order['id']}")).json() == order


async def test_expired_hold_frees_seats_without_sweeper(
    organizer: Actor, buyer: Actor, actor: Callable[..., Actor], engine: AsyncEngine
) -> None:
    event = await create_event(organizer)
    hold = (await buyer.hold(event.id, event.seat_ids[:4])).json()
    await travel_back(engine, hold["id"], 601)

    assert (await buyer.get(f"/holds/{hold['id']}")).json()["status"] == "expired"
    assert (await actor().hold(event.id, event.seat_ids[:2])).status_code == 201
    # the expired hold no longer counts toward the buyer's limit either
    assert (await buyer.hold(event.id, event.seat_ids[4:8])).status_code == 201


async def test_idempotency_key_replays_the_same_hold(organizer: Actor, buyer: Actor) -> None:
    event = await create_event(organizer)
    seats = event.seat_ids[:2]

    first = await buyer.hold(event.id, seats, idempotency_key="k-1")
    again = await buyer.hold(event.id, seats, idempotency_key="k-1")
    misuse = await buyer.hold(event.id, event.seat_ids[2:3], idempotency_key="k-1")

    assert first.status_code == again.status_code == 201
    assert again.json() == first.json()
    assert again.headers["Idempotent-Replayed"] == "true"
    assert misuse.status_code == 422
    assert misuse.json()["error"]["code"] == "IDEMPOTENCY_KEY_REUSED"
