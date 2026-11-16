"""ORM models.

Invariants the *database* enforces, independent of application code:
  * uq_tickets_seat_id            - a seat is sold at most once
  * uq_orders_hold_id             - a hold becomes at most one order
  * ck_seats_state_consistent     - a seat is exactly one of: free / held (with owner,
                                    hold and deadline) / sold (with owner and order)
"""

import uuid
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    MetaData,
    Numeric,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.domain.enums import HoldStatus, Role, SeatStatus

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
    type_annotation_map = {  # noqa: RUF012
        datetime: DateTime(timezone=True),
        Decimal: Numeric(12, 2),
    }


def pg_enum[E: StrEnum](enum_cls: type[E], name: str) -> Enum:
    return Enum(enum_cls, name=name, values_callable=lambda e: [m.value for m in e])


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(320), unique=True)
    password_hash: Mapped[str] = mapped_column(String(256))
    role: Mapped[Role] = mapped_column(pg_enum(Role, "user_role"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class Event(Base):
    __tablename__ = "events"
    __table_args__ = (CheckConstraint("sale_starts_at <= starts_at", name="sale_before_event"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    organizer_id: Mapped[uuid.UUID]
    name: Mapped[str] = mapped_column(String(200))
    venue: Mapped[str | None] = mapped_column(String(200))
    starts_at: Mapped[datetime]
    sale_starts_at: Mapped[datetime]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class SeatCategory(Base):
    __tablename__ = "seat_categories"
    __table_args__ = (
        UniqueConstraint("event_id", "code"),
        CheckConstraint("price >= 0", name="price_non_negative"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    event_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"))
    code: Mapped[str] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(200))
    price: Mapped[Decimal]


class Hold(Base):
    __tablename__ = "holds"
    __table_args__ = (
        Index(
            "uq_holds_buyer_idempotency_key",
            "buyer_id",
            "idempotency_key",
            unique=True,
            postgresql_where=text("idempotency_key IS NOT NULL"),
        ),
        Index(
            "ix_holds_active_expires_at", "expires_at", postgresql_where=text("status = 'active'")
        ),
        Index("ix_holds_buyer_id_created_at", "buyer_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    event_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("events.id"))
    # JWT `sub`. Deliberately no FK to users: identity belongs to the token issuer.
    buyer_id: Mapped[uuid.UUID]
    status: Mapped[HoldStatus] = mapped_column(
        pg_enum(HoldStatus, "hold_status"), server_default=HoldStatus.ACTIVE.value
    )
    created_at: Mapped[datetime]
    expires_at: Mapped[datetime]
    closed_at: Mapped[datetime | None]
    idempotency_key: Mapped[str | None] = mapped_column(String(128))
    request_hash: Mapped[str | None] = mapped_column(String(64))


class Order(Base):
    __tablename__ = "orders"
    __table_args__ = (Index("ix_orders_buyer_id_created_at", "buyer_id", "created_at"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    hold_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("holds.id"), unique=True)
    event_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("events.id"))
    buyer_id: Mapped[uuid.UUID]
    total: Mapped[Decimal]
    created_at: Mapped[datetime]


class Seat(Base):
    __tablename__ = "seats"
    __table_args__ = (
        UniqueConstraint("event_id", "row_label", "seat_number", name="uq_seats_position"),
        CheckConstraint(
            "(status = 'available' AND buyer_id IS NULL AND hold_id IS NULL"
            "   AND held_until IS NULL AND order_id IS NULL)"
            " OR (status = 'held' AND buyer_id IS NOT NULL AND hold_id IS NOT NULL"
            "   AND held_until IS NOT NULL AND order_id IS NULL)"
            " OR (status = 'sold' AND buyer_id IS NOT NULL AND hold_id IS NOT NULL"
            "   AND held_until IS NULL AND order_id IS NOT NULL)",
            name="state_consistent",
        ),
        Index("ix_seats_event_id_category_id_sort_key", "event_id", "category_id", "sort_key"),
        Index("ix_seats_event_id_sort_key", "event_id", "sort_key"),
        Index(
            "ix_seats_event_id_buyer_id",
            "event_id",
            "buyer_id",
            postgresql_where=text("buyer_id IS NOT NULL"),
        ),
        Index("ix_seats_hold_id", "hold_id", postgresql_where=text("hold_id IS NOT NULL")),
        Index("ix_seats_held_until", "held_until", postgresql_where=text("status = 'held'")),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    event_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"))
    category_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("seat_categories.id"))
    row_label: Mapped[str] = mapped_column(String(16))
    seat_number: Mapped[int]
    # Organizer's order ("best first"): the order in which "any N seats" hands them out.
    sort_key: Mapped[int]
    status: Mapped[SeatStatus] = mapped_column(
        pg_enum(SeatStatus, "seat_status"), server_default=SeatStatus.AVAILABLE.value
    )
    buyer_id: Mapped[uuid.UUID | None]
    hold_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("holds.id"))
    held_until: Mapped[datetime | None]
    order_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("orders.id"))


class HoldSeat(Base):
    """Seats a hold asked for, with the price fixed at hold time. Kept as history."""

    __tablename__ = "hold_seats"
    __table_args__ = (Index("ix_hold_seats_seat_id", "seat_id"),)

    hold_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("holds.id"), primary_key=True)
    seat_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("seats.id"), primary_key=True)
    price: Mapped[Decimal]


class Ticket(Base):
    __tablename__ = "tickets"
    __table_args__ = (Index("ix_tickets_order_id", "order_id"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    order_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("orders.id"))
    event_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("events.id"))
    # The last line of defence against double sales: the DB refuses a second ticket for a seat.
    seat_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("seats.id"), unique=True)
    price: Mapped[Decimal]
