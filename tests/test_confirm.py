"""Confirming a hold."""

from collections.abc import Callable

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.helpers import Actor, create_event, scalar, seat_statuses, travel_back


async def test_confirm_sells_the_seats(
    organizer: Actor, buyer: Actor, client: AsyncClient, engine: AsyncEngine
) -> None:
    event = await create_event(organizer)
    seats = [event.seat("1", 1), event.seat("2", 1)]
    hold = (await buyer.hold(event.id, seats)).json()

    res = await buyer.confirm(hold["id"])

    assert res.status_code == 200, res.text
    order = res.json()
    assert order["status"] == "paid"
    assert order["hold_id"] == hold["id"]
    assert order["total"] == "7000.00"
    assert {t["seat_id"] for t in order["tickets"]} == set(seats)
    statuses = await seat_statuses(client, event.id)
    assert {statuses[s] for s in seats} == {"sold"}
    assert (await buyer.get(f"/holds/{hold['id']}")).json()["status"] == "confirmed"
    assert await scalar(engine, "SELECT count(*) FROM tickets") == 2


async def test_expired_hold_whose_seats_were_taken_is_rejected(
    organizer: Actor, buyer: Actor, actor: Callable[..., Actor], engine: AsyncEngine
) -> None:
    event = await create_event(organizer)
    hold = (await buyer.hold(event.id, event.seat_ids[:2])).json()
    await travel_back(engine, hold["id"], 601)
    other = actor()
    assert (await other.hold(event.id, event.seat_ids[:1])).status_code == 201

    res = await buyer.confirm(hold["id"])

    assert res.status_code == 410
    assert res.json()["error"]["code"] == "HOLD_EXPIRED"
    assert await scalar(engine, "SELECT count(*) FROM orders") == 0


async def test_unknown_hold(buyer: Actor) -> None:
    res = await buyer.confirm("00000000-0000-0000-0000-000000000000")
    assert res.status_code == 404
