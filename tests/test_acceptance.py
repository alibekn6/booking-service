"""Section 7 acceptance criteria, one test each (plus a control case where useful)."""

import asyncio
from collections import Counter
from collections.abc import Callable

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine

from app.container import Container
from app.invariants import audit
from tests.helpers import Actor, create_event, scalar, seat_statuses, travel_back


async def assert_invariants(container: Container) -> None:
    async with container.engine.connect() as conn:
        report = await audit(conn, max_seats=container.settings.max_seats_per_buyer)
    assert report.ok, report.violations


async def test_50_parallel_holds_on_one_seat_exactly_one_succeeds(
    organizer: Actor, actor: Callable[..., Actor], container: Container
) -> None:
    event = await create_event(organizer)
    seat = event.seat("1", 5)
    buyers = [actor() for _ in range(50)]

    responses = await asyncio.gather(*(b.hold(event.id, [seat]) for b in buyers))

    statuses = Counter(r.status_code for r in responses)
    assert statuses == {201: 1, 409: 49}, statuses
    assert {r.json()["error"]["code"] for r in responses if r.status_code == 409} == {
        "SEATS_UNAVAILABLE"
    }
    assert await scalar(container.engine, "SELECT count(*) FROM hold_seats") == 1
    await assert_invariants(container)


async def test_overlapping_seat_sets_at_most_one_succeeds_and_nothing_hangs(
    organizer: Actor, actor: Callable[..., Actor], container: Container, client: AsyncClient
) -> None:
    """[1,2] vs [2,3] in parallel, many rounds: never both, never partial, never stuck."""
    rounds = 30
    event = await create_event(organizer, rows=rounds, seats_per_row=3)
    for r in range(1, rounds + 1):
        s1, s2, s3 = (event.seat(str(r), n) for n in (1, 2, 3))
        a, b = actor(), actor()

        res_a, res_b = await asyncio.wait_for(
            asyncio.gather(a.hold(event.id, [s1, s2]), b.hold(event.id, [s2, s3])),
            timeout=10,  # "the system does not hang"
        )

        codes = sorted((res_a.status_code, res_b.status_code))
        assert codes == [201, 409], codes  # at most one; with all-or-nothing exactly one
        statuses = await seat_statuses(client, event.id)
        loser_only_seat = s3 if res_a.status_code == 201 else s1
        assert statuses[loser_only_seat] == "available"  # no partial hold
    await assert_invariants(container)


async def test_confirm_after_10_minutes_and_1_second_is_rejected(
    organizer: Actor, buyer: Actor, actor: Callable[..., Actor], engine: AsyncEngine
) -> None:
    event = await create_event(organizer)
    seats = [event.seat("2", 1), event.seat("2", 2)]
    hold = (await buyer.hold(event.id, seats)).json()

    await travel_back(engine, hold["id"], 10 * 60 + 1)
    res = await buyer.confirm(hold["id"])

    assert res.status_code == 410
    assert res.json()["error"]["code"] == "HOLD_EXPIRED"
    assert await scalar(engine, "SELECT count(*) FROM orders") == 0
    # Rejected even though nobody took the seats, and the seats are free again:
    assert (await buyer.get(f"/holds/{hold['id']}")).json()["status"] == "expired"
    assert (await actor().hold(event.id, seats)).status_code == 201


async def test_confirm_just_before_10_minutes_succeeds(
    organizer: Actor, buyer: Actor, engine: AsyncEngine
) -> None:
    event = await create_event(organizer)
    hold = (await buyer.hold(event.id, [event.seat("2", 1)])).json()

    await travel_back(engine, hold["id"], 10 * 60 - 2)

    assert (await buyer.confirm(hold["id"])).status_code == 200


async def test_five_identical_confirms_give_one_purchase_and_the_same_answer(
    organizer: Actor, actor: Callable[..., Actor], engine: AsyncEngine
) -> None:
    event = await create_event(organizer)
    sequential_buyer, parallel_buyer = actor(), actor()
    seq_hold = (await sequential_buyer.hold(event.id, [event.seat("1", 1)])).json()
    par_hold = (
        await parallel_buyer.hold(event.id, [event.seat("1", 2), event.seat("1", 3)])
    ).json()

    sequential = [await sequential_buyer.confirm(seq_hold["id"]) for _ in range(5)]
    parallel = await asyncio.gather(*(parallel_buyer.confirm(par_hold["id"]) for _ in range(5)))

    for responses, hold in ((sequential, seq_hold), (parallel, par_hold)):
        assert [r.status_code for r in responses] == [200] * 5
        bodies = [r.json() for r in responses]
        assert all(b == bodies[0] for b in bodies), "every repeat must return the same order"
        orders = await scalar(
            engine, "SELECT count(*) FROM orders WHERE hold_id = :h", h=hold["id"]
        )
        assert orders == 1
        assert len(bodies[0]["tickets"]) == len(hold["seats"])


async def test_four_seat_limit_is_not_bypassed_by_parallel_requests(
    organizer: Actor, buyer: Actor, container: Container
) -> None:
    event = await create_event(organizer, rows=4, seats_per_row=10)
    pairs = [[event.seats[i]["id"], event.seats[i + 1]["id"]] for i in range(0, 20, 2)]

    responses = await asyncio.gather(*(buyer.hold(event.id, pair) for pair in pairs))

    codes = Counter(r.status_code for r in responses)
    assert codes[201] == 2, codes  # 2 holds x 2 seats = the 4-seat limit
    assert {r.json()["error"]["code"] for r in responses if r.status_code == 409} == {
        "HOLD_LIMIT_EXCEEDED"
    }
    owned = await scalar(
        container.engine,
        "SELECT count(*) FROM seats WHERE buyer_id = :b AND status IN ('held', 'sold')",
        b=buyer.user_id,
    )
    assert owned == 4
    await assert_invariants(container)


async def test_four_seat_limit_counts_purchases_under_parallel_requests(
    organizer: Actor, buyer: Actor, container: Container
) -> None:
    event = await create_event(organizer, rows=3, seats_per_row=10)
    bought = (await buyer.hold(event.id, event.seat_ids[:3])).json()
    assert (await buyer.confirm(bought["id"])).status_code == 200

    std = event.category("STD")["id"]
    responses = await asyncio.gather(
        *(buyer.hold(event.id, category_id=std, quantity=1) for _ in range(10))
    )

    assert Counter(r.status_code for r in responses)[201] == 1  # 3 bought + 1 = 4
    await assert_invariants(container)
