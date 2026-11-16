"""Shared SQL expressions."""

from sqlalchemy import ColumnElement, DateTime, and_, func, or_

from app.db.models import Seat
from app.domain.enums import SeatStatus


def db_now() -> ColumnElement[DateTime]:
    """The single clock for all business rules: Postgres time, never the app server's.

    statement_timestamp() is fixed for the whole statement, so every row and every
    re-check of one statement sees the same "now".
    """
    return func.statement_timestamp(type_=DateTime(timezone=True))


def seat_is_free() -> ColumnElement[bool]:
    """Free = available, or held with a deadline that has passed (lazy expiry)."""
    return or_(
        Seat.status == SeatStatus.AVAILABLE,
        and_(Seat.status == SeatStatus.HELD, Seat.held_until <= db_now()),
    )
