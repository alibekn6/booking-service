import uuid
from collections import Counter
from decimal import Decimal
from typing import Any

import orjson
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import EventNotFoundError
from app.db.models import Event, SeatCategory
from app.domain.commands import NewEvent
from app.domain.enums import SeatStatus
from app.domain.views import CategoryAvailability, EventView, SeatMap, SeatState
from app.infra.cache import ReadCache, Snapshot
from app.repositories.events import EventRepository


def _json_default(obj: Any) -> str:
    # Decimal is not native to orjson; asyncpg returns its own uuid.UUID subclass,
    # which orjson (exact-type match) does not recognise either.
    if isinstance(obj, Decimal | uuid.UUID):
        return str(obj)
    raise TypeError(f"not JSON serializable: {type(obj).__name__}")


def render_json(view: object) -> bytes:
    """orjson renders dataclasses/UUID/datetime natively; same shape as the API schemas."""
    return orjson.dumps(view, default=_json_default, option=orjson.OPT_UTC_Z)


class EventService:
    def __init__(self, session: AsyncSession, events: EventRepository, cache: ReadCache) -> None:
        self._session = session
        self._events = events
        self._cache = cache

    # --- organizer --------------------------------------------------------------------------

    async def create_event(self, organizer_id: uuid.UUID, cmd: NewEvent) -> EventView:
        event_id = uuid.uuid4()
        category_ids = {c.code: uuid.uuid4() for c in cmd.categories}
        event = Event(
            id=event_id,
            organizer_id=organizer_id,
            name=cmd.name,
            venue=cmd.venue,
            starts_at=cmd.starts_at,
            sale_starts_at=cmd.sale_starts_at,
        )
        categories = [
            SeatCategory(
                id=category_ids[c.code], event_id=event_id, code=c.code, name=c.name, price=c.price
            )
            for c in cmd.categories
        ]
        seats = [
            {
                "id": uuid.uuid4(),
                "event_id": event_id,
                "category_id": category_ids[s.category],
                "row_label": s.row,
                "seat_number": s.number,
                "sort_key": i,
            }
            for i, s in enumerate(cmd.seats)
        ]
        async with self._session.begin():
            await self._events.add(event, categories, seats)
            return await self._event_view(event_id)

    # --- buyers: cached snapshots, at most read_cache_ttl stale ------------------------------

    async def event_snapshot(self, event_id: uuid.UUID) -> Snapshot:
        async def compute() -> bytes:
            return render_json(await self._event_view(event_id))

        return await self._cache.get_or_compute(f"event:{event_id}", compute)

    async def seat_map_snapshot(self, event_id: uuid.UUID) -> Snapshot:
        async def compute() -> bytes:
            return render_json(await self.seat_map(event_id))

        return await self._cache.get_or_compute(f"seatmap:{event_id}", compute)

    async def seat_map(self, event_id: uuid.UUID) -> SeatMap:
        """Seat statuses + per-category summary. The summary is computed from the very same
        rows, so both halves of the response always agree."""
        generated_at, seats = await self._events.seat_states(event_id)
        if generated_at is None:
            raise EventNotFoundError()
        categories = await self._events.categories(event_id)
        code_by_id = {c.id: c.code for c in categories}
        total = Counter(s.category_id for s in seats)
        free = Counter(s.category_id for s in seats if s.status == SeatStatus.AVAILABLE)
        return SeatMap(
            event_id=event_id,
            generated_at=generated_at,
            categories=[
                CategoryAvailability(
                    id=c.id,
                    code=c.code,
                    name=c.name,
                    price=c.price,
                    total_seats=total[c.id],
                    available_seats=free[c.id],
                )
                for c in categories
            ],
            seats=[
                SeatState(
                    id=s.id,
                    row=s.row,
                    number=s.number,
                    category=code_by_id[s.category_id],
                    status=s.status,
                )
                for s in seats
            ],
        )

    async def _event_view(self, event_id: uuid.UUID) -> EventView:
        event = await self._events.get(event_id)
        if event is None:
            raise EventNotFoundError()
        return EventView(
            id=event.id,
            name=event.name,
            venue=event.venue,
            starts_at=event.starts_at,
            sale_starts_at=event.sale_starts_at,
            categories=await self._events.category_availability(event_id),
        )
