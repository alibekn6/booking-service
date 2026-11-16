"""HTTP request/response contracts. Requests convert themselves into domain commands."""

import uuid
from collections import defaultdict
from datetime import datetime
from decimal import Decimal
from itertools import pairwise
from typing import Annotated, Any, Literal, Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, EmailStr, Field, model_validator

from app.domain.commands import HoldRequest, NewCategory, NewEvent, NewSeat
from app.domain.enums import HoldStatus, Role, SeatStatus

MAX_SEATS_PER_EVENT = 100_000
MAX_SEATS_PER_REQUEST = 4  # 1..4 seats per hold request

Money = Annotated[Decimal, Field(ge=0, max_digits=12, decimal_places=2)]
CategoryCode = Annotated[str, Field(min_length=1, max_length=64)]
RowLabel = Annotated[str, Field(min_length=1, max_length=16)]
SeatNumber = Annotated[int, Field(ge=1, le=10_000)]


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


# --- auth -----------------------------------------------------------------------------------


class RegisterIn(_In):
    email: EmailStr
    password: Annotated[str, Field(min_length=8, max_length=128)]


class LoginIn(_In):
    email: EmailStr
    password: Annotated[str, Field(min_length=1, max_length=128)]


class UserOut(BaseModel):
    id: uuid.UUID
    email: str
    role: Role


class TokenOut(BaseModel):
    access_token: str
    token_type: Literal["bearer"] = "bearer"  # noqa: S105
    expires_in: int


# --- create event ---------------------------------------------------------------------------


class CategoryIn(_In):
    code: CategoryCode
    name: Annotated[str, Field(min_length=1, max_length=200)] | None = None
    price: Money = Decimal(0)


class SeatIn(_In):
    row: RowLabel
    number: SeatNumber
    category: CategoryCode


class SeatRunIn(_In):
    """Shorthand for seats `seat_from..seat_to` (inclusive) of one row."""

    row: RowLabel
    seat_from: SeatNumber
    seat_to: SeatNumber
    category: CategoryCode

    @model_validator(mode="after")
    def _check_range(self) -> Self:
        if self.seat_to < self.seat_from:
            raise ValueError("seat_to must be >= seat_from")
        return self


class EventCreateIn(_In):
    name: Annotated[str, Field(min_length=1, max_length=200)]
    venue: Annotated[str, Field(max_length=200)] | None = None
    starts_at: AwareDatetime
    sale_starts_at: AwareDatetime
    categories: Annotated[list[CategoryIn] | None, Field(max_length=50)] = Field(
        default=None,
        description="Optional. Categories used by seats but not listed here get price 0.",
    )
    seats: Annotated[list[SeatIn], Field(max_length=MAX_SEATS_PER_EVENT)] = Field(
        default_factory=list, description="Explicit seats (row, number, category)."
    )
    rows: Annotated[list[SeatRunIn], Field(max_length=10_000)] = Field(
        default_factory=list, description="Shorthand: runs of seats per row."
    )

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.sale_starts_at > self.starts_at:
            raise ValueError("sale_starts_at must not be later than starts_at")
        if not self.seats and not self.rows:
            raise ValueError("provide seats and/or rows")
        total = len(self.seats) + sum(r.seat_to - r.seat_from + 1 for r in self.rows)
        if total > MAX_SEATS_PER_EVENT:
            raise ValueError(f"too many seats: {total} > {MAX_SEATS_PER_EVENT}")

        used = {s.category for s in self.seats} | {r.category for r in self.rows}
        if self.categories is not None:
            codes = [c.code for c in self.categories]
            if len(set(codes)) != len(codes):
                raise ValueError("category codes must be unique")
            if unknown := used - set(codes):
                raise ValueError(f"seats reference undeclared categories: {sorted(unknown)}")
        if len(used) > 50:
            raise ValueError("too many categories")

        spans: dict[str, list[tuple[int, int]]] = defaultdict(list)
        for s in self.seats:
            spans[s.row].append((s.number, s.number))
        for r in self.rows:
            spans[r.row].append((r.seat_from, r.seat_to))
        for row, ranges in spans.items():
            ranges.sort()
            for (_, prev_to), (cur_from, _) in pairwise(ranges):
                if cur_from <= prev_to:
                    raise ValueError(f"duplicate seat {cur_from} in row {row!r}")
        return self

    def to_command(self) -> NewEvent:
        declared = {c.code: c for c in self.categories or []}
        order = [c.code for c in self.categories or []]
        seats = [NewSeat(s.row, s.number, s.category) for s in self.seats]
        for r in self.rows:
            seats.extend(NewSeat(r.row, n, r.category) for n in range(r.seat_from, r.seat_to + 1))
        for code in (s.category for s in seats):
            if code not in declared:
                declared[code] = CategoryIn(code=code)
                order.append(code)
        return NewEvent(
            name=self.name,
            venue=self.venue,
            starts_at=self.starts_at,
            sale_starts_at=self.sale_starts_at,
            categories=tuple(
                NewCategory(code, declared[code].name or code, declared[code].price)
                for code in order
            ),
            seats=tuple(seats),
        )


# --- availability ---------------------------------------------------------------------------


class CategoryOut(BaseModel):
    id: uuid.UUID
    code: str
    name: str
    price: Decimal
    total_seats: int
    available_seats: int


class EventOut(BaseModel):
    id: uuid.UUID
    name: str
    venue: str | None
    starts_at: datetime
    sale_starts_at: datetime
    categories: list[CategoryOut]


class SeatOut(BaseModel):
    id: uuid.UUID
    row: str
    number: int
    category: str = Field(description="Category code; see `categories` for id and price")
    status: SeatStatus


class SeatMapOut(BaseModel):
    event_id: uuid.UUID
    generated_at: datetime = Field(description="Snapshot time; lags reality by <= cache TTL")
    categories: list[CategoryOut]
    seats: list[SeatOut]


# --- holds and orders -------------------------------------------------------------------------


class HoldCreateIn(_In):
    """Either `seat_ids` or `category_id` + `quantity`."""

    seat_ids: Annotated[
        list[uuid.UUID] | None, Field(min_length=1, max_length=MAX_SEATS_PER_REQUEST)
    ] = None
    category_id: uuid.UUID | None = None
    quantity: Annotated[int | None, Field(ge=1, le=MAX_SEATS_PER_REQUEST)] = None

    @model_validator(mode="after")
    def _one_mode(self) -> Self:
        by_seats = self.seat_ids is not None
        by_category = self.category_id is not None or self.quantity is not None
        if by_seats == by_category:
            raise ValueError("provide either seat_ids, or category_id together with quantity")
        if by_category and (self.category_id is None or self.quantity is None):
            raise ValueError("category_id and quantity must be provided together")
        if self.seat_ids is not None and len(set(self.seat_ids)) != len(self.seat_ids):
            raise ValueError("seat_ids must not contain duplicates")
        return self

    def to_command(self) -> HoldRequest:
        return HoldRequest(
            seat_ids=tuple(self.seat_ids) if self.seat_ids is not None else None,
            category_id=self.category_id,
            quantity=self.quantity,
        )


class SeatLineOut(BaseModel):
    seat_id: uuid.UUID
    row: str
    number: int
    category_id: uuid.UUID
    price: Decimal


class HoldOut(BaseModel):
    id: uuid.UUID
    event_id: uuid.UUID
    status: HoldStatus
    created_at: datetime
    expires_at: datetime
    total: Decimal
    seats: list[SeatLineOut]


class TicketOut(BaseModel):
    id: uuid.UUID
    seat_id: uuid.UUID
    row: str
    number: int
    category_id: uuid.UUID
    price: Decimal


class OrderOut(BaseModel):
    id: uuid.UUID
    hold_id: uuid.UUID
    event_id: uuid.UUID
    status: Literal["paid"] = "paid"
    total: Decimal
    created_at: datetime
    tickets: list[TicketOut]


# --- errors -----------------------------------------------------------------------------------


class ErrorBody(BaseModel):
    code: str = Field(examples=["SEATS_UNAVAILABLE"])
    message: str
    request_id: str | None = None
    details: dict[str, Any] | list[Any] | None = None


class ErrorOut(BaseModel):
    error: ErrorBody
