from enum import StrEnum


class Role(StrEnum):
    BUYER = "buyer"
    ORGANIZER = "organizer"


class SeatStatus(StrEnum):
    AVAILABLE = "available"
    HELD = "held"
    SOLD = "sold"


class HoldStatus(StrEnum):
    ACTIVE = "active"
    CONFIRMED = "confirmed"
    RELEASED = "released"
    EXPIRED = "expired"
