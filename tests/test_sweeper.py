from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine

from app.container import Container
from app.invariants import audit
from app.workers.sweeper import Sweeper
from tests.helpers import Actor, create_event, scalar, travel_back


async def test_sweeper_releases_expired_holds_only(
    organizer: Actor, actor: object, engine: AsyncEngine, container: Container
) -> None:
    event = await create_event(organizer)
    expired_buyer, live_buyer = actor(), actor()  # type: ignore[operator]
    expired = (await expired_buyer.hold(event.id, event.seat_ids[:3])).json()
    live = (await live_buyer.hold(event.id, event.seat_ids[3:5])).json()
    await travel_back(engine, expired["id"], 3600)

    async with engine.connect() as conn:  # before: rows still point at the dead hold
        report = await audit(conn, max_seats=4, grace_s=0)
    assert report.violations["expired_hold_not_released"]

    result = await Sweeper(engine, batch_size=1000).sweep_once()

    assert (result.seats_released, result.holds_expired) == (3, 1)
    assert (
        await scalar(engine, "SELECT status::text FROM holds WHERE id = :h", h=expired["id"])
        == "expired"
    )
    assert (
        await scalar(engine, "SELECT status::text FROM holds WHERE id = :h", h=live["id"])
        == "active"
    )
    assert await scalar(engine, "SELECT count(*) FROM seats WHERE status = 'held'") == 2
    async with engine.connect() as conn:  # nothing stuck
        assert (await audit(conn, max_seats=4, grace_s=0)).ok


async def test_sweeper_is_a_noop_when_nothing_expired(
    organizer: Actor, buyer: Actor, engine: AsyncEngine, client: AsyncClient
) -> None:
    event = await create_event(organizer)
    await buyer.hold(event.id, event.seat_ids[:1])

    result = await Sweeper(engine, batch_size=1000).sweep_once()

    assert (result.seats_released, result.holds_expired) == (0, 0)
