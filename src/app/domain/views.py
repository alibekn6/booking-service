"""Read models returned by services. Plain dataclasses: no ORM sessions leak upwards."""

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from app.domain.enums import HoldStatus, Role, SeatStatus


@dataclass(frozen=True, slots=True)
class CategoryAvailability:
    id: uuid.UUID
    code: str
    name: str
    price: Decimal
    total_seats: int
    available_seats: int


@dataclass(frozen=True, slots=True)
class EventView:
    id: uuid.UUID
    name: str
    venue: str | None
    starts_at: datetime
    sale_starts_at: datetime
    categories: list[CategoryAvailability]


@dataclass(frozen=True, slots=True)
class SeatState:
    id: uuid.UUID
    row: str
    number: int
    category: str  # code; ids/prices are in SeatMap.categories (keeps the payload small)
    status: SeatStatus


@dataclass(frozen=True, slots=True)
class SeatMap:
    event_id: uuid.UUID
    generated_at: datetime
    categories: list[CategoryAvailability]
    seats: list[SeatState]


@dataclass(frozen=True, slots=True)
class SeatLine:
    """A seat inside a hold or an order, with the price fixed at hold time."""

    seat_id: uuid.UUID
    row: str
    number: int
    category_id: uuid.UUID
    price: Decimal


@dataclass(frozen=True, slots=True)
class HoldView:
    id: uuid.UUID
    event_id: uuid.UUID
    status: HoldStatus
    created_at: datetime
    expires_at: datetime
    seats: list[SeatLine]
    total: Decimal = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "total", sum((s.price for s in self.seats), Decimal(0)))


@dataclass(frozen=True, slots=True)
class TicketView:
    id: uuid.UUID
    seat_id: uuid.UUID
    row: str
    number: int
    category_id: uuid.UUID
    price: Decimal


@dataclass(frozen=True, slots=True)
class OrderView:
    id: uuid.UUID
    hold_id: uuid.UUID
    event_id: uuid.UUID
    total: Decimal
    created_at: datetime
    tickets: list[TicketView]


@dataclass(frozen=True, slots=True)
class UserView:
    id: uuid.UUID
    email: str
    role: Role
