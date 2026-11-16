"""Helpers to read Postgres error details through SQLAlchemy's asyncpg wrapping."""

from sqlalchemy.exc import DBAPIError

LOCK_NOT_AVAILABLE = "55P03"  # lock_timeout
QUERY_CANCELED = "57014"  # statement_timeout
DEADLOCK_DETECTED = "40P01"
TOO_MANY_CONNECTIONS = "53300"
UNIQUE_VIOLATION = "23505"

RETRYABLE_SQLSTATES = frozenset(
    {LOCK_NOT_AVAILABLE, QUERY_CANCELED, DEADLOCK_DETECTED, TOO_MANY_CONNECTIONS}
)


def _candidates(exc: DBAPIError) -> tuple[object, ...]:
    orig = exc.orig
    return (orig, getattr(orig, "__cause__", None))


def sqlstate(exc: DBAPIError) -> str | None:
    for candidate in _candidates(exc):
        code = getattr(candidate, "sqlstate", None) or getattr(candidate, "pgcode", None)
        if code:
            return str(code)
    return None


def constraint_name(exc: DBAPIError) -> str | None:
    for candidate in _candidates(exc):
        name = getattr(candidate, "constraint_name", None)
        if name:
            return str(name)
    return None
