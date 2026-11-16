"""Maps domain errors to HTTP. Every refusal has a stable machine-readable `code`.

Business refusals (seat taken, limit, expired, not started) are 4xx, never 5xx.
Contention/overload in Postgres becomes 503 + Retry-After (client may retry safely).
"""

from typing import Any, cast

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import DBAPIError
from sqlalchemy.exc import TimeoutError as PoolTimeoutError

from app.api.middleware import current_request_id
from app.core.errors import (
    AuthenticationError,
    ConflictError,
    DomainError,
    GoneError,
    InvalidInputError,
    NotFoundError,
    PermissionDeniedError,
    RateLimitedError,
)
from app.core.logging import get_logger
from app.db.errors import RETRYABLE_SQLSTATES, sqlstate

log = get_logger(__name__)

_STATUS_BY_KIND: tuple[tuple[type[DomainError], int], ...] = (
    (AuthenticationError, 401),
    (PermissionDeniedError, 403),
    (NotFoundError, 404),
    (ConflictError, 409),
    (GoneError, 410),
    (InvalidInputError, 422),
    (RateLimitedError, 429),
)


def _body(code: str, message: str, details: Any = None) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message, "request_id": current_request_id()}
    if details:
        error["details"] = jsonable_encoder(details)
    return {"error": error}


def _status_for(exc: DomainError) -> int:
    for kind, status in _STATUS_BY_KIND:
        if isinstance(exc, kind):
            return status
    return 400


async def _domain_error(_: Request, error: Exception) -> JSONResponse:
    exc = cast("DomainError", error)
    headers: dict[str, str] = {}
    if isinstance(exc, RateLimitedError):
        headers["Retry-After"] = str(exc.retry_after_s)
    if isinstance(exc, AuthenticationError):
        headers["WWW-Authenticate"] = "Bearer"
    return JSONResponse(
        _body(exc.code, exc.detail_message, exc.details),
        status_code=_status_for(exc),
        headers=headers,
    )


async def _validation_error(_: Request, error: Exception) -> JSONResponse:
    exc = cast("RequestValidationError", error)
    details = [
        {"loc": list(e.get("loc", ())), "msg": e.get("msg"), "type": e.get("type")}
        for e in exc.errors()
    ]
    return JSONResponse(_body("VALIDATION_ERROR", "Invalid request", details), status_code=422)


def _busy() -> JSONResponse:
    return JSONResponse(
        _body("SERVICE_BUSY", "Service is busy, please retry"),
        status_code=503,
        headers={"Retry-After": "1"},
    )


async def _pool_timeout(_: Request, exc: Exception) -> JSONResponse:
    log.warning("db_pool_exhausted", error=str(exc))
    return _busy()


async def _db_error(_: Request, error: Exception) -> JSONResponse:
    exc = cast("DBAPIError", error)
    code = sqlstate(exc)
    if code in RETRYABLE_SQLSTATES:
        log.warning("db_contention", sqlstate=code)
        return _busy()
    raise exc  # a genuine bug: let the middleware log it and answer 500


def register_exception_handlers(app: FastAPI) -> None:
    app.add_exception_handler(DomainError, _domain_error)
    app.add_exception_handler(RequestValidationError, _validation_error)
    app.add_exception_handler(PoolTimeoutError, _pool_timeout)
    app.add_exception_handler(DBAPIError, _db_error)
