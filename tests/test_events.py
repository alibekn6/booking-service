"""Event creation and the seat map."""

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from httpx import AsyncClient

from tests.helpers import API, Actor, create_event


def event_payload(**overrides: Any) -> dict[str, Any]:
    now = datetime.now(UTC)
    payload: dict[str, Any] = {
        "name": "Concert",
        "starts_at": (now + timedelta(days=10)).isoformat(),
        "sale_starts_at": (now + timedelta(days=1)).isoformat(),
        "seats": [
            {"row": "A", "number": 1, "category": "VIP"},
            {"row": "A", "number": 2, "category": "VIP"},
            {"row": "B", "number": 1, "category": "Balcony"},
        ],
    }
    return payload | overrides


async def test_create_event_with_explicit_seat_list(organizer: Actor, client: AsyncClient) -> None:
    res = await organizer.post("/events", event_payload())

    assert res.status_code == 201, res.text
    body = res.json()
    # categories not declared up front are derived from the seats (price 0)
    assert {c["code"]: c["total_seats"] for c in body["categories"]} == {"VIP": 2, "Balcony": 1}
    seat_map = (await client.get(f"{API}/events/{body['id']}/seats")).json()
    assert [(s["row"], s["number"], s["category"]) for s in seat_map["seats"]] == [
        ("A", 1, "VIP"),
        ("A", 2, "VIP"),
        ("B", 1, "Balcony"),
    ]


async def test_create_event_with_row_runs_and_prices(organizer: Actor) -> None:
    event = await create_event(organizer, rows=3, seats_per_row=25)

    assert len(event.seats) == 75
    assert event.category("VIP")["price"] == "5000.00"
    assert event.category("STD")["total_seats"] == 50


@pytest.mark.parametrize(
    ("overrides", "fragment"),
    [
        ({"seats": [{"row": "A", "number": 1, "category": "X"}] * 2}, "duplicate seat"),
        (
            {"sale_starts_at": (datetime.now(UTC) + timedelta(days=99)).isoformat()},
            "sale_starts_at",
        ),
        ({"categories": [{"code": "VIP", "price": "1"}]}, "undeclared categories"),
        ({"seats": []}, "provide seats"),
        ({"starts_at": "2026-12-01T20:00:00"}, "timezone"),
    ],
)
async def test_create_event_validation(
    organizer: Actor, overrides: dict[str, Any], fragment: str
) -> None:
    res = await organizer.post("/events", event_payload(**overrides))

    assert res.status_code == 422
    assert res.json()["error"]["code"] == "VALIDATION_ERROR"
    assert fragment in res.text


async def test_only_organizers_create_events(buyer: Actor, client: AsyncClient) -> None:
    assert (await client.post(f"{API}/events", json=event_payload())).status_code == 401
    res = await buyer.post("/events", event_payload())
    assert res.status_code == 403
    assert res.json()["error"]["code"] == "FORBIDDEN"


async def test_unknown_event_is_404(client: AsyncClient) -> None:
    missing = "00000000-0000-0000-0000-000000000000"
    for path in (f"/events/{missing}", f"/events/{missing}/seats"):
        res = await client.get(API + path)
        assert res.status_code == 404
        assert res.json()["error"]["code"] == "EVENT_NOT_FOUND"


async def test_seat_map_is_served_gzipped_when_accepted(
    organizer: Actor, client: AsyncClient
) -> None:
    event = await create_event(organizer, rows=10, seats_per_row=40)
    url = f"{API}/events/{event.id}/seats"

    gzipped = await client.get(url, headers={"Accept-Encoding": "gzip"})
    plain = await client.get(url, headers={"Accept-Encoding": "identity"})

    assert gzipped.headers["Content-Encoding"] == "gzip"
    assert "Content-Encoding" not in plain.headers
    # httpx decompresses transparently; the cache is off in tests, so each call is a fresh
    # snapshot with its own generated_at.
    assert gzipped.json()["seats"] == plain.json()["seats"]
    # random UUIDs dominate the payload and compress poorly
    assert int(gzipped.headers["Content-Length"]) * 3 < int(plain.headers["Content-Length"])
