"""Domain errors. Transport-agnostic: the HTTP layer maps error *kinds* to status codes."""

from typing import Any, ClassVar


class DomainError(Exception):
    code: ClassVar[str] = "ERROR"
    message: ClassVar[str] = "Error"

    def __init__(self, message: str | None = None, **details: Any) -> None:
        self.detail_message = message or self.message
        self.details = details
        super().__init__(self.detail_message)


# --- kinds ------------------------------------------------------------------------------------


class NotFoundError(DomainError):
    pass


class ConflictError(DomainError):
    """A normal business refusal (seat taken, limit reached...). Not a server error."""


class GoneError(DomainError):
    pass


class InvalidInputError(DomainError):
    pass


class AuthenticationError(DomainError):
    code = "UNAUTHENTICATED"
    message = "Authentication required"


class PermissionDeniedError(DomainError):
    code = "FORBIDDEN"
    message = "Not allowed"


class RateLimitedError(DomainError):
    code = "RATE_LIMITED"
    message = "Too many requests"

    def __init__(self, retry_after_s: int, **details: Any) -> None:
        super().__init__(retry_after_s=retry_after_s, **details)
        self.retry_after_s = retry_after_s


# --- concrete ---------------------------------------------------------------------------------


class EventNotFoundError(NotFoundError):
    code, message = "EVENT_NOT_FOUND", "Event not found"


class HoldNotFoundError(NotFoundError):
    code, message = "HOLD_NOT_FOUND", "Hold not found"


class OrderNotFoundError(NotFoundError):
    code, message = "ORDER_NOT_FOUND", "Order not found"


class SaleNotStartedError(ConflictError):
    code, message = "SALE_NOT_STARTED", "Sale has not started yet"


class SeatsUnavailableError(ConflictError):
    code, message = "SEATS_UNAVAILABLE", "Some of the selected seats are already taken"


class NotEnoughSeatsError(ConflictError):
    code, message = "NOT_ENOUGH_SEATS", "Not enough free seats in this category"


class HoldLimitExceededError(ConflictError):
    code, message = "HOLD_LIMIT_EXCEEDED", "Seat limit per buyer for this event exceeded"


class HoldReleasedError(ConflictError):
    code, message = "HOLD_RELEASED", "Hold was cancelled"


class HoldAlreadyConfirmedError(ConflictError):
    code, message = "HOLD_ALREADY_CONFIRMED", "Hold is already purchased"


class EmailTakenError(ConflictError):
    code, message = "EMAIL_TAKEN", "Email is already registered"


class HoldExpiredError(GoneError):
    code, message = "HOLD_EXPIRED", "Hold has expired"


class InvalidSeatsError(InvalidInputError):
    code, message = "INVALID_SEATS", "Seats do not exist for this event"


class InvalidCategoryError(InvalidInputError):
    code, message = "INVALID_CATEGORY", "Category does not exist for this event"


class IdempotencyKeyReusedError(InvalidInputError):
    code, message = (
        "IDEMPOTENCY_KEY_REUSED",
        "Idempotency-Key was already used with a different request",
    )


class InvalidCredentialsError(AuthenticationError):
    code, message = "INVALID_CREDENTIALS", "Wrong email or password"
