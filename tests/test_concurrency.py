"""Races beyond the acceptance list, each followed by a full invariant audit."""

import asyncio
import random
from collections import Counter
from collections.abc import Callable

from sqlalchemy import text

from app.container import Container
from tests.helpers import Actor, create_event
from tests.test_acceptance import assert_invariants


async def test_any_seats_mode_hands_out_each_seat_once(
    organizer: Actor, actor: Callable[..., Actor], container: Container
) -> None:
    event = await create_event(organizer, rows=5, seats_per_row=10)  # 40 STD seats
    std = event.category("STD")["id"]

    responses = await asyncio.gather(
        *(actor().hold(event.id, category_id=std, quantity=2) for _ in range(30))
    )

    ok = [r.json() for r in responses if r.status_code == 201]
    assert len(ok) == 20
    seats = [s["seat_id"] for hold in ok for s in hold["seats"]]
    assert len(seats) == len(set(seats)) == 40
    assert {r.json()["error"]["code"] for r in responses if r.status_code != 201} == {
        "NOT_ENOUGH_SEATS"
    }
    await assert_invariants(container)


async def test_mixed_rush_keeps_every_invariant(
    organizer: Actor, actor: Callable[..., Actor], container: Container
) -> None:
    """120 buyers on 30 seats: random picks, then confirm / release / abandon."""
    rng = random.Random(42)
    event = await create_event(organizer, rows=3, seats_per_row=10)

    async def buyer_journey() -> str:
        buyer = actor()
        if rng.random() < 0.3:
            category = event.category(rng.choice(["VIP", "STD"]))["id"]
            res = await buyer.hold(event.id, category_id=category, quantity=rng.randint(1, 4))
        else:
            res = await buyer.hold(event.id, rng.sample(event.seat_ids, rng.randint(1, 4)))
        if res.status_code != 201:
            return res.json()["error"]["code"]
        hold_id = res.json()["id"]
        roll = rng.random()
        if roll < 0.6:
            results = await asyncio.gather(*(buyer.confirm(hold_id) for _ in range(2)))
            assert [r.status_code for r in results] == [200, 200]
            assert results[0].json() == results[1].json()
            return "bought"
        if roll < 0.8:
            assert (await buyer.release(hold_id)).status_code == 204
            return "released"
        return "abandoned"

    outcomes = Counter(await asyncio.gather(*(buyer_journey() for _ in range(120))))

    assert outcomes["bought"] > 0
    assert set(outcomes) <= {
        "bought",
        "released",
        "abandoned",
        "SEATS_UNAVAILABLE",
        "NOT_ENOUGH_SEATS",
    }
    async with container.engine.connect() as conn:
        sold = (
            await conn.execute(text("SELECT count(*) FROM seats WHERE status = 'sold'"))
        ).scalar_one()
        tickets = (await conn.execute(text("SELECT count(*) FROM tickets"))).scalar_one()
    assert sold == tickets <= 30
    await assert_invariants(container)


async def test_confirm_racing_a_takeover_at_the_deadline(
    organizer: Actor, actor: Callable[..., Actor], container: Container
) -> None:
    """The hold expires *while* its owner confirms and another buyer grabs the seat.
    Whatever the interleaving, at most one of them may end up with the seat."""
    rounds = 25
    event = await create_event(organizer, rows=rounds, seats_per_row=1)
    for r in range(rounds):
        seat = event.seats[r]["id"]
        owner, rival = actor(), actor()
        hold = (await owner.hold(event.id, [seat])).json()
        async with container.engine.begin() as conn:
            await conn.execute(
                text(
                    "UPDATE holds SET expires_at = clock_timestamp() + interval '15 ms'"
                    " WHERE id = :id;"
                ),
                {"id": hold["id"]},
            )
            await conn.execute(
                text(
                    "UPDATE seats SET held_until = (SELECT expires_at FROM holds WHERE id = :id)"
                    " WHERE hold_id = :id"
                ),
                {"id": hold["id"]},
            )

        async def rival_attempts(rival: Actor = rival, seat: str = seat) -> bool:
            for _ in range(20):
                if (await rival.hold(event.id, [seat])).status_code == 201:
                    return True
                await asyncio.sleep(0.002)
            return False

        await asyncio.sleep(rng_delay := (r % 5) * 0.004)
        confirm, rival_won = await asyncio.gather(owner.confirm(hold["id"]), rival_attempts())
        assert confirm.status_code in (200, 410), (confirm.status_code, rng_delay)
        assert not (confirm.status_code == 200 and rival_won), "seat given to two buyers"
    await assert_invariants(container)
