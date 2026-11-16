"""Data access for holds and purchases: the code that must never sell a seat twice.

Locking protocol (READ COMMITTED everywhere):
  * create hold:      advisory lock (buyer, event) -> seat rows FOR UPDATE, ordered by seats.id
  * confirm/release:  hold row FOR NO KEY UPDATE   -> its seat rows FOR UPDATE, ordered by seats.id
  * "any N seats", sweeper: FOR UPDATE SKIP LOCKED - never wait, so never part of a deadlock
Every path that *waits* takes seat locks in the same global order, hence no deadlock cycles.

When a statement waits for a row lock, Postgres re-evaluates its WHERE against the newest
committed row version (EvalPlanQual). A seat grabbed by the lock holder therefore just
drops out of the waiter's result: the waiter sees "not free" and refuses, it can't overwrite.
"""

import uuid
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy import Result, Select, and_, func, insert, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Event, Hold, HoldSeat, Order, Seat, SeatCategory, Ticket
from app.db.sql import db_now, seat_is_free
from app.domain.enums import HoldStatus, SeatStatus
from app.domain.views import HoldView, OrderView, SeatLine, TicketView


@dataclass(frozen=True, slots=True)
class SaleWindow:
    starts_at: datetime
    is_open: bool


@dataclass(frozen=True, slots=True)
class LockedHold:
    id: uuid.UUID
    event_id: uuid.UUID
    buyer_id: uuid.UUID
    status: HoldStatus


@dataclass(frozen=True, slots=True)
class Deadline:
    now: datetime
    alive: bool


class BookingRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # --- hold creation ------------------------------------------------------------------

    async def sale_window_locking_buyer(
        self, event_id: uuid.UUID, buyer_id: uuid.UUID
    ) -> SaleWindow | None:
        """Read the sale window and serialize this buyer's hold attempts on this event.

        The advisory lock makes "count owned seats, then hold more" atomic per buyer
        (the seat limit holds under parallel requests) and makes Idempotency-Key replay race-free.
        One round trip; the lock is released at commit/rollback.
        """
        lock = func.pg_advisory_xact_lock(func.hashtextextended(f"hold:{event_id}:{buyer_id}", 0))
        stmt = select(
            Event.sale_starts_at,
            (Event.sale_starts_at <= db_now()).label("is_open"),
            lock.label("locked"),
        ).where(Event.id == event_id)
        row = (await self._session.execute(stmt)).one_or_none()
        return None if row is None else SaleWindow(row.sale_starts_at, row.is_open)

    async def find_idempotent_hold(
        self, buyer_id: uuid.UUID, key: str
    ) -> tuple[uuid.UUID, str | None] | None:
        stmt = select(Hold.id, Hold.request_hash).where(
            Hold.buyer_id == buyer_id, Hold.idempotency_key == key
        )
        row = (await self._session.execute(stmt)).one_or_none()
        return None if row is None else (row.id, row.request_hash)

    async def count_owned_seats(self, event_id: uuid.UUID, buyer_id: uuid.UUID) -> int:
        """Seats the buyer owns on the event right now = bought + still held."""
        stmt = (
            select(func.count())
            .select_from(Seat)
            .where(
                Seat.event_id == event_id,
                Seat.buyer_id == buyer_id,
                or_(
                    Seat.status == SeatStatus.SOLD,
                    and_(Seat.status == SeatStatus.HELD, Seat.held_until > db_now()),
                ),
            )
        )
        return (await self._session.execute(stmt)).scalar_one()

    async def lock_free_seats(
        self, event_id: uuid.UUID, seat_ids: Sequence[uuid.UUID]
    ) -> list[SeatLine]:
        """Lock those of `seat_ids` that are free, in id order. Waits for in-flight holders."""
        stmt = (
            self._seat_lines()
            .where(Seat.event_id == event_id, Seat.id.in_(seat_ids), seat_is_free())
            .order_by(Seat.id)
            .with_for_update(of=Seat)
        )
        return self._to_lines(await self._session.execute(stmt))

    async def lock_any_free_seats(
        self, event_id: uuid.UUID, category_id: uuid.UUID, quantity: int
    ) -> list[SeatLine]:
        """Best free seats of a category; seats someone is grabbing right now are skipped."""
        stmt = (
            self._seat_lines()
            .where(Seat.event_id == event_id, Seat.category_id == category_id, seat_is_free())
            .order_by(Seat.sort_key)
            .limit(quantity)
            .with_for_update(of=Seat, skip_locked=True)
        )
        return self._to_lines(await self._session.execute(stmt))

    async def existing_seat_ids(
        self, event_id: uuid.UUID, seat_ids: Sequence[uuid.UUID]
    ) -> set[uuid.UUID]:
        stmt = select(Seat.id).where(Seat.event_id == event_id, Seat.id.in_(seat_ids))
        return set((await self._session.execute(stmt)).scalars())

    async def category_exists(self, event_id: uuid.UUID, category_id: uuid.UUID) -> bool:
        stmt = select(SeatCategory.id).where(
            SeatCategory.id == category_id, SeatCategory.event_id == event_id
        )
        return (await self._session.execute(stmt)).first() is not None

    async def create_hold(
        self,
        *,
        hold_id: uuid.UUID,
        event_id: uuid.UUID,
        buyer_id: uuid.UUID,
        ttl: timedelta,
        seats: Sequence[SeatLine],
        idempotency_key: str | None,
        request_hash: str,
    ) -> tuple[datetime, datetime]:
        """Persist a hold over seats that the caller has already locked."""
        now = db_now()
        created_at, expires_at = (
            await self._session.execute(
                insert(Hold)
                .values(
                    id=hold_id,
                    event_id=event_id,
                    buyer_id=buyer_id,
                    status=HoldStatus.ACTIVE,
                    created_at=now,
                    expires_at=now + ttl,
                    idempotency_key=idempotency_key,
                    request_hash=request_hash,
                )
                .returning(Hold.created_at, Hold.expires_at)
            )
        ).one()
        await self._session.execute(
            update(Seat)
            .where(Seat.id.in_([s.seat_id for s in seats]))
            .values(
                status=SeatStatus.HELD,
                hold_id=hold_id,
                buyer_id=buyer_id,
                held_until=expires_at,
            )
            .execution_options(synchronize_session=False)
        )
        await self._session.execute(
            insert(HoldSeat),
            [{"hold_id": hold_id, "seat_id": s.seat_id, "price": s.price} for s in seats],
        )
        return created_at, expires_at

    # --- confirm / release ----------------------------------------------------------------

    async def lock_hold(self, hold_id: uuid.UUID) -> LockedHold | None:
        """Serializes confirm/release of one hold: a second confirm waits here."""
        stmt = (
            select(Hold.id, Hold.event_id, Hold.buyer_id, Hold.status)
            .where(Hold.id == hold_id)
            .with_for_update(key_share=True)  # FOR NO KEY UPDATE
        )
        row = (await self._session.execute(stmt)).one_or_none()
        return None if row is None else LockedHold(row.id, row.event_id, row.buyer_id, row.status)

    async def lock_seats_of_hold(self, hold_id: uuid.UUID) -> list[uuid.UUID]:
        stmt = (
            select(Seat.id)
            .where(Seat.hold_id == hold_id, Seat.status == SeatStatus.HELD)
            .order_by(Seat.id)
            .with_for_update()
        )
        return list((await self._session.execute(stmt)).scalars())

    async def hold_lines(self, hold_id: uuid.UUID) -> list[tuple[uuid.UUID, Decimal]]:
        stmt = select(HoldSeat.seat_id, HoldSeat.price).where(HoldSeat.hold_id == hold_id)
        return [(r.seat_id, r.price) for r in await self._session.execute(stmt)]

    async def deadline(self, hold_id: uuid.UUID) -> Deadline:
        """Judged after the locks are taken, so the deadline uses the latest possible moment."""
        now = db_now()
        stmt = select(now.label("now"), (Hold.expires_at > now).label("alive")).where(
            Hold.id == hold_id
        )
        row = (await self._session.execute(stmt)).one()
        return Deadline(row.now, row.alive)

    async def create_order(
        self,
        *,
        order_id: uuid.UUID,
        hold: LockedHold,
        lines: Sequence[tuple[uuid.UUID, Decimal]],
        paid_at: datetime,
    ) -> None:
        await self._session.execute(
            insert(Order).values(
                id=order_id,
                hold_id=hold.id,
                event_id=hold.event_id,
                buyer_id=hold.buyer_id,
                total=sum((price for _, price in lines), Decimal(0)),
                created_at=paid_at,
            )
        )
        # uq_tickets_seat_id: if anything above were ever wrong, this INSERT fails.
        await self._session.execute(
            insert(Ticket),
            [
                {
                    "id": uuid.uuid4(),
                    "order_id": order_id,
                    "event_id": hold.event_id,
                    "seat_id": seat_id,
                    "price": price,
                }
                for seat_id, price in lines
            ],
        )
        await self._session.execute(
            update(Seat)
            .where(Seat.hold_id == hold.id, Seat.status == SeatStatus.HELD)
            .values(status=SeatStatus.SOLD, held_until=None, order_id=order_id)
            .execution_options(synchronize_session=False)
        )
        await self._close_hold(hold.id, HoldStatus.CONFIRMED, paid_at)

    async def order_id_for_hold(self, hold_id: uuid.UUID) -> uuid.UUID | None:
        stmt = select(Order.id).where(Order.hold_id == hold_id)
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def release(self, hold_id: uuid.UUID, deadline: Deadline) -> None:
        """Free the seats still attached to the hold (in id order) and close it."""
        mine = (
            select(Seat.id)
            .where(Seat.hold_id == hold_id, Seat.status == SeatStatus.HELD)
            .order_by(Seat.id)
            .with_for_update()
            .cte("mine")
        )
        await self._session.execute(
            update(Seat)
            .where(Seat.id.in_(select(mine.c.id)))
            .values(status=SeatStatus.AVAILABLE, hold_id=None, buyer_id=None, held_until=None)
            .execution_options(synchronize_session=False)
        )
        status = HoldStatus.RELEASED if deadline.alive else HoldStatus.EXPIRED
        await self._close_hold(hold_id, status, deadline.now)

    async def _close_hold(self, hold_id: uuid.UUID, status: HoldStatus, at: datetime) -> None:
        await self._session.execute(
            update(Hold)
            .where(Hold.id == hold_id)
            .values(status=status, closed_at=at)
            .execution_options(synchronize_session=False)
        )

    # --- views -----------------------------------------------------------------------------

    async def holds(
        self,
        *,
        hold_id: uuid.UUID | None = None,
        buyer_id: uuid.UUID | None = None,
        event_id: uuid.UUID | None = None,
        limit: int = 1,
    ) -> list[HoldView]:
        now = db_now()
        stmt = select(
            Hold.id,
            Hold.event_id,
            Hold.status,
            Hold.created_at,
            Hold.expires_at,
            (and_(Hold.status == HoldStatus.ACTIVE, Hold.expires_at <= now)).label("lapsed"),
        )
        if hold_id is not None:
            stmt = stmt.where(Hold.id == hold_id)
        if buyer_id is not None:
            stmt = stmt.where(Hold.buyer_id == buyer_id)
        if event_id is not None:
            stmt = stmt.where(Hold.event_id == event_id)
        rows = (
            await self._session.execute(stmt.order_by(Hold.created_at.desc()).limit(limit))
        ).all()
        if not rows:
            return []

        lines: dict[uuid.UUID, list[SeatLine]] = defaultdict(list)
        line_rows = await self._session.execute(
            select(
                HoldSeat.hold_id,
                HoldSeat.seat_id,
                Seat.row_label,
                Seat.seat_number,
                Seat.category_id,
                HoldSeat.price,
            )
            .join(Seat, Seat.id == HoldSeat.seat_id)
            .where(HoldSeat.hold_id.in_([r.id for r in rows]))
            .order_by(Seat.sort_key)
        )
        for r in line_rows:
            lines[r.hold_id].append(
                SeatLine(r.seat_id, r.row_label, r.seat_number, r.category_id, r.price)
            )
        return [
            HoldView(
                id=r.id,
                event_id=r.event_id,
                # An active hold past its deadline *is* expired, sweeper or not.
                status=HoldStatus.EXPIRED if r.lapsed else r.status,
                created_at=r.created_at,
                expires_at=r.expires_at,
                seats=lines[r.id],
            )
            for r in rows
        ]

    async def hold_owner(self, hold_id: uuid.UUID) -> uuid.UUID | None:
        stmt = select(Hold.buyer_id).where(Hold.id == hold_id)
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def orders(
        self,
        *,
        order_id: uuid.UUID | None = None,
        buyer_id: uuid.UUID | None = None,
        event_id: uuid.UUID | None = None,
        limit: int = 1,
    ) -> list[OrderView]:
        stmt = select(Order.id, Order.hold_id, Order.event_id, Order.total, Order.created_at)
        if order_id is not None:
            stmt = stmt.where(Order.id == order_id)
        if buyer_id is not None:
            stmt = stmt.where(Order.buyer_id == buyer_id)
        if event_id is not None:
            stmt = stmt.where(Order.event_id == event_id)
        rows = (
            await self._session.execute(stmt.order_by(Order.created_at.desc()).limit(limit))
        ).all()
        if not rows:
            return []

        tickets: dict[uuid.UUID, list[TicketView]] = defaultdict(list)
        ticket_rows = await self._session.execute(
            select(
                Ticket.order_id,
                Ticket.id,
                Ticket.seat_id,
                Seat.row_label,
                Seat.seat_number,
                Seat.category_id,
                Ticket.price,
            )
            .join(Seat, Seat.id == Ticket.seat_id)
            .where(Ticket.order_id.in_([r.id for r in rows]))
            .order_by(Seat.sort_key)
        )
        for r in ticket_rows:
            tickets[r.order_id].append(
                TicketView(r.id, r.seat_id, r.row_label, r.seat_number, r.category_id, r.price)
            )
        return [
            OrderView(r.id, r.hold_id, r.event_id, r.total, r.created_at, tickets[r.id])
            for r in rows
        ]

    async def order_owner(self, order_id: uuid.UUID) -> uuid.UUID | None:
        stmt = select(Order.buyer_id).where(Order.id == order_id)
        return (await self._session.execute(stmt)).scalar_one_or_none()

    # --- helpers ---------------------------------------------------------------------------

    @staticmethod
    def _seat_lines() -> Select[uuid.UUID, str, int, uuid.UUID, Decimal]:
        return select(
            Seat.id, Seat.row_label, Seat.seat_number, Seat.category_id, SeatCategory.price
        ).join(SeatCategory, SeatCategory.id == Seat.category_id)

    @staticmethod
    def _to_lines(result: Result[uuid.UUID, str, int, uuid.UUID, Decimal]) -> list[SeatLine]:
        return [SeatLine(*row) for row in result]
