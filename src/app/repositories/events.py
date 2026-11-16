import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import func, insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Event, Seat, SeatCategory
from app.db.sql import db_now, seat_is_free
from app.domain.enums import SeatStatus
from app.domain.views import CategoryAvailability


@dataclass(frozen=True, slots=True)
class CategoryRow:
    id: uuid.UUID
    code: str
    name: str
    price: Decimal


@dataclass(frozen=True, slots=True)
class SeatRow:
    id: uuid.UUID
    row: str
    number: int
    category_id: uuid.UUID
    status: SeatStatus  # effective status: an expired hold already reads as AVAILABLE


class EventRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(
        self, event: Event, categories: Sequence[SeatCategory], seats: Sequence[dict[str, Any]]
    ) -> None:
        self._session.add(event)
        await self._session.flush()
        self._session.add_all(categories)
        await self._session.flush()
        await self._session.execute(insert(Seat), list(seats))

    async def get(self, event_id: uuid.UUID) -> Event | None:
        return await self._session.get(Event, event_id)

    async def category_availability(self, event_id: uuid.UUID) -> list[CategoryAvailability]:
        stmt = (
            select(
                SeatCategory.id,
                SeatCategory.code,
                SeatCategory.name,
                SeatCategory.price,
                func.count(Seat.id).label("total"),
                func.count(Seat.id).filter(seat_is_free()).label("available"),
            )
            .outerjoin(Seat, Seat.category_id == SeatCategory.id)
            .where(SeatCategory.event_id == event_id)
            .group_by(SeatCategory.id)
            .order_by(SeatCategory.price.desc(), SeatCategory.code)
        )
        return [
            CategoryAvailability(
                id=r.id,
                code=r.code,
                name=r.name,
                price=r.price,
                total_seats=r.total,
                available_seats=r.available,
            )
            for r in (await self._session.execute(stmt)).all()
        ]

    async def categories(self, event_id: uuid.UUID) -> list[CategoryRow]:
        stmt = (
            select(SeatCategory.id, SeatCategory.code, SeatCategory.name, SeatCategory.price)
            .where(SeatCategory.event_id == event_id)
            .order_by(SeatCategory.price.desc(), SeatCategory.code)
        )
        return [
            CategoryRow(id=r.id, code=r.code, name=r.name, price=r.price)
            for r in (await self._session.execute(stmt)).all()
        ]

    async def seat_states(self, event_id: uuid.UUID) -> tuple[datetime | None, list[SeatRow]]:
        """Every seat with its effective status, all judged at one statement's "now"."""
        stmt = (
            select(
                Seat.id,
                Seat.row_label,
                Seat.seat_number,
                Seat.category_id,
                Seat.status,
                Seat.held_until,
                db_now().label("now"),
            )
            .where(Seat.event_id == event_id)
            .order_by(Seat.sort_key)
        )
        rows = (await self._session.execute(stmt)).all()
        if not rows:
            return None, []
        now: datetime = rows[0].now
        return now, [
            SeatRow(
                id=r.id,
                row=r.row_label,
                number=r.seat_number,
                category_id=r.category_id,
                status=(
                    SeatStatus.AVAILABLE
                    if r.status == SeatStatus.HELD and r.held_until <= now
                    else r.status
                ),
            )
            for r in rows
        ]
