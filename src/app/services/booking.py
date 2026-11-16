"""Holds and purchases. Business rules live here; locking mechanics in BookingRepository."""

import uuid
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import (
    EventNotFoundError,
    HoldAlreadyConfirmedError,
    HoldExpiredError,
    HoldLimitExceededError,
    HoldNotFoundError,
    HoldReleasedError,
    IdempotencyKeyReusedError,
    InvalidCategoryError,
    InvalidSeatsError,
    NotEnoughSeatsError,
    OrderNotFoundError,
    SaleNotStartedError,
    SeatsUnavailableError,
)
from app.core.logging import get_logger
from app.db.errors import constraint_name
from app.domain.commands import HoldRequest
from app.domain.enums import HoldStatus
from app.domain.views import HoldView, OrderView, SeatLine
from app.repositories.booking import BookingRepository

log = get_logger(__name__)

IDEMPOTENCY_CONSTRAINT = "uq_holds_buyer_idempotency_key"


@dataclass(frozen=True, slots=True)
class BookingPolicy:
    hold_ttl: timedelta
    max_seats_per_buyer: int


class BookingService:
    def __init__(
        self, session: AsyncSession, repo: BookingRepository, policy: BookingPolicy
    ) -> None:
        self._session = session
        self._repo = repo
        self._policy = policy

    # --- hold ------------------------------------------------------------------------------

    async def hold(
        self,
        *,
        buyer_id: uuid.UUID,
        event_id: uuid.UUID,
        request: HoldRequest,
        idempotency_key: str | None = None,
    ) -> tuple[HoldView, bool]:
        """Hold seats for the policy TTL, all or nothing. Returns (hold, replayed)."""
        try:
            async with self._session.begin():
                hold_id, replayed = await self._hold_tx(
                    buyer_id, event_id, request, idempotency_key
                )
        except IntegrityError as exc:
            # Same key used concurrently for *another* event (a different advisory lock).
            if constraint_name(exc) == IDEMPOTENCY_CONSTRAINT:
                raise IdempotencyKeyReusedError() from exc
            raise
        return await self._hold_view(hold_id), replayed

    async def _hold_tx(
        self,
        buyer_id: uuid.UUID,
        event_id: uuid.UUID,
        request: HoldRequest,
        idempotency_key: str | None,
    ) -> tuple[uuid.UUID, bool]:
        window = await self._repo.sale_window_locking_buyer(event_id, buyer_id)
        if window is None:
            raise EventNotFoundError()
        if not window.is_open:  # judged by the DB clock
            raise SaleNotStartedError(sale_starts_at=window.starts_at)

        if idempotency_key is not None:
            previous = await self._repo.find_idempotent_hold(buyer_id, idempotency_key)
            if previous is not None:
                previous_id, previous_hash = previous
                if previous_hash != request.fingerprint(event_id):
                    raise IdempotencyKeyReusedError()
                return previous_id, True

        owned = await self._repo.count_owned_seats(event_id, buyer_id)
        if owned + request.seat_count > self._policy.max_seats_per_buyer:
            raise HoldLimitExceededError(
                f"A buyer may own at most {self._policy.max_seats_per_buyer} seats per event",
                limit=self._policy.max_seats_per_buyer,
                owned=owned,
                requested=request.seat_count,
            )

        seats = await self._lock_seats(event_id, request)
        hold_id = uuid.uuid4()
        await self._repo.create_hold(
            hold_id=hold_id,
            event_id=event_id,
            buyer_id=buyer_id,
            ttl=self._policy.hold_ttl,
            seats=seats,
            idempotency_key=idempotency_key,
            request_hash=request.fingerprint(event_id),
        )
        return hold_id, False

    async def _lock_seats(self, event_id: uuid.UUID, request: HoldRequest) -> list[SeatLine]:
        if request.seat_ids is not None:
            seats = await self._repo.lock_free_seats(event_id, request.seat_ids)
            if len(seats) == len(request.seat_ids):
                return seats
            existing = await self._repo.existing_seat_ids(event_id, request.seat_ids)
            unknown = [s for s in request.seat_ids if s not in existing]
            if unknown:
                raise InvalidSeatsError(seat_ids=unknown)
            locked = {s.seat_id for s in seats}
            raise SeatsUnavailableError(seat_ids=[s for s in request.seat_ids if s not in locked])

        if request.category_id is None or request.quantity is None:
            raise InvalidCategoryError("category_id and quantity are required")
        seats = await self._repo.lock_any_free_seats(
            event_id, request.category_id, request.quantity
        )
        if len(seats) == request.quantity:
            return seats
        if not await self._repo.category_exists(event_id, request.category_id):
            raise InvalidCategoryError()
        raise NotEnoughSeatsError(requested=request.quantity)

    # --- confirm ---------------------------------------------------------------------------

    async def confirm(self, *, buyer_id: uuid.UUID, hold_id: uuid.UUID) -> OrderView:
        """Turn a live hold into a paid order.

        Idempotent: a repeated confirm returns the very same order.
        Strict deadline: after expiry it is refused even if nobody took the seats.
        """
        try:
            async with self._session.begin():
                order_id = await self._confirm_tx(buyer_id, hold_id)
        except IntegrityError as exc:
            # The locking protocol makes this unreachable; if it ever fires, the database's
            # uniqueness guard just prevented an oversell. Refuse loudly.
            log.error("oversell_guard_fired", hold_id=str(hold_id), constraint=constraint_name(exc))
            raise SeatsUnavailableError() from exc
        orders = await self._repo.orders(order_id=order_id)
        return orders[0]

    async def _confirm_tx(self, buyer_id: uuid.UUID, hold_id: uuid.UUID) -> uuid.UUID:
        hold = await self._repo.lock_hold(hold_id)  # concurrent confirms queue up here
        if hold is None or hold.buyer_id != buyer_id:
            raise HoldNotFoundError()
        match hold.status:
            case HoldStatus.CONFIRMED:
                order_id = await self._repo.order_id_for_hold(hold_id)
                if order_id is None:  # guarded by the schema; would mean corrupted data
                    raise RuntimeError(f"confirmed hold {hold_id} has no order")
                return order_id
            case HoldStatus.RELEASED:
                raise HoldReleasedError()
            case HoldStatus.EXPIRED:
                raise HoldExpiredError()

        locked = await self._repo.lock_seats_of_hold(hold_id)
        lines = await self._repo.hold_lines(hold_id)
        deadline = await self._repo.deadline(hold_id)
        # Seats can only leave a hold after it expired, so a short count also means expired.
        if not deadline.alive or len(locked) != len(lines):
            raise HoldExpiredError()

        order_id = uuid.uuid4()
        # Payment is simulated; getting here *is* the successful payment.
        await self._repo.create_order(
            order_id=order_id, hold=hold, lines=lines, paid_at=deadline.now
        )
        return order_id

    # --- release ---------------------------------------------------------------------------

    async def release(self, *, buyer_id: uuid.UUID, hold_id: uuid.UUID) -> None:
        """Give the seats back. Idempotent: releasing a closed hold is a no-op."""
        async with self._session.begin():
            hold = await self._repo.lock_hold(hold_id)
            if hold is None or hold.buyer_id != buyer_id:
                raise HoldNotFoundError()
            if hold.status == HoldStatus.CONFIRMED:
                raise HoldAlreadyConfirmedError()
            if hold.status != HoldStatus.ACTIVE:
                return
            await self._repo.release(hold_id, await self._repo.deadline(hold_id))

    # --- the buyer's holds and orders ------------------------------------------------------

    async def get_hold(self, *, buyer_id: uuid.UUID, hold_id: uuid.UUID) -> HoldView:
        holds = await self._repo.holds(hold_id=hold_id, buyer_id=buyer_id)
        if not holds:
            raise HoldNotFoundError()
        return holds[0]

    async def list_holds(
        self, *, buyer_id: uuid.UUID, event_id: uuid.UUID | None, limit: int
    ) -> list[HoldView]:
        return await self._repo.holds(buyer_id=buyer_id, event_id=event_id, limit=limit)

    async def get_order(self, *, buyer_id: uuid.UUID, order_id: uuid.UUID) -> OrderView:
        orders = await self._repo.orders(order_id=order_id, buyer_id=buyer_id)
        if not orders:
            raise OrderNotFoundError()
        return orders[0]

    async def list_orders(
        self, *, buyer_id: uuid.UUID, event_id: uuid.UUID | None, limit: int
    ) -> list[OrderView]:
        return await self._repo.orders(buyer_id=buyer_id, event_id=event_id, limit=limit)

    async def _hold_view(self, hold_id: uuid.UUID) -> HoldView:
        holds = await self._repo.holds(hold_id=hold_id)
        return holds[0]
