import uuid
from typing import Annotated

from fastapi import APIRouter, Header, Response

from app.api.deps import EventServiceDep, Organizer
from app.api.schemas import ErrorOut, EventCreateIn, EventOut, SeatMapOut
from app.domain.views import EventView
from app.infra.cache import Snapshot

router = APIRouter(prefix="/events", tags=["events"])

# Lets a CDN/browser reuse a snapshot for the same window the server does.
_SNAPSHOT_CACHE_CONTROL = "public, max-age=1"


AcceptEncoding = Annotated[str, Header(include_in_schema=False)]


def _snapshot(snapshot: Snapshot, accept_encoding: str) -> Response:
    headers = {"Cache-Control": _SNAPSHOT_CACHE_CONTROL, "Vary": "Accept-Encoding"}
    if "gzip" in accept_encoding.lower():
        # Pre-compressed once per cache rebuild, not per request.
        headers["Content-Encoding"] = "gzip"
        return Response(snapshot.gzipped, media_type="application/json", headers=headers)
    return Response(snapshot.raw, media_type="application/json", headers=headers)


@router.post(
    "",
    status_code=201,
    response_model=EventOut,
    responses={401: {"model": ErrorOut}, 403: {"model": ErrorOut}},
    summary="Create an event with its seats (organizer)",
)
async def create_event(
    payload: EventCreateIn, organizer: Organizer, events: EventServiceDep
) -> EventView:
    return await events.create_event(organizer.user_id, payload.to_command())


@router.get(
    "/{event_id}",
    response_model=EventOut,
    responses={404: {"model": ErrorOut}},
    summary="Event card with free seats per category",
)
async def get_event(
    event_id: uuid.UUID, events: EventServiceDep, accept_encoding: AcceptEncoding = ""
) -> Response:
    return _snapshot(await events.event_snapshot(event_id), accept_encoding)


@router.get(
    "/{event_id}/seats",
    response_model=SeatMapOut,
    responses={404: {"model": ErrorOut}},
    summary="Every seat with its status + free seats per category",
    description="A snapshot shared by all viewers; lags reality by at most the cache TTL "
    "(1 s by default). Holds are always re-checked in the database.",
)
async def get_seat_map(
    event_id: uuid.UUID, events: EventServiceDep, accept_encoding: AcceptEncoding = ""
) -> Response:
    return _snapshot(await events.seat_map_snapshot(event_id), accept_encoding)
