"""Inputs to services. The HTTP layer validates and converts its schemas into these."""

import hashlib
import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

import orjson


@dataclass(frozen=True, slots=True)
class NewCategory:
    code: str
    name: str
    price: Decimal


@dataclass(frozen=True, slots=True)
class NewSeat:
    row: str
    number: int
    category: str


@dataclass(frozen=True, slots=True)
class NewEvent:
    name: str
    venue: str | None
    starts_at: datetime
    sale_starts_at: datetime
    categories: tuple[NewCategory, ...]
    seats: tuple[NewSeat, ...]  # in organizer order: "best" seats first


@dataclass(frozen=True, slots=True)
class HoldRequest:
    """Either specific seats or any N free seats of a category."""

    seat_ids: tuple[uuid.UUID, ...] | None = None
    category_id: uuid.UUID | None = None
    quantity: int | None = None

    @property
    def seat_count(self) -> int:
        return len(self.seat_ids) if self.seat_ids is not None else int(self.quantity or 0)

    def fingerprint(self, event_id: uuid.UUID) -> str:
        """Stable hash used to detect an Idempotency-Key reused for a different request."""
        payload = {
            "event_id": str(event_id),
            "seat_ids": sorted(map(str, self.seat_ids)) if self.seat_ids else None,
            "category_id": str(self.category_id) if self.category_id else None,
            "quantity": self.quantity,
        }
        return hashlib.sha256(orjson.dumps(payload, option=orjson.OPT_SORT_KEYS)).hexdigest()
