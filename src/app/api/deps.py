"""Dependency wiring. The only module that knows how request-scoped objects are built.

Routes declare what they need (`BookingServiceDep`, `CurrentUser`, ...) and never touch
the container, sessions or repositories directly. Tests override any of these with
`app.dependency_overrides`.
"""

from collections.abc import AsyncIterator
from typing import Annotated, cast

import structlog
from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.container import Container
from app.core.errors import AuthenticationError, PermissionDeniedError
from app.core.security import Principal
from app.domain.enums import Role
from app.repositories.booking import BookingRepository
from app.repositories.events import EventRepository
from app.repositories.users import UserRepository
from app.services.auth import AuthService
from app.services.booking import BookingService
from app.services.events import EventService

# --- infrastructure ---------------------------------------------------------------------------


def get_container(request: Request) -> Container:
    return cast("Container", request.app.state.container)


ContainerDep = Annotated[Container, Depends(get_container)]


async def get_session(container: ContainerDep) -> AsyncIterator[AsyncSession]:
    # The session only checks out a pool connection on first use, so cache hits are free.
    async with container.session_factory() as session:
        yield session


SessionDep = Annotated[AsyncSession, Depends(get_session)]

# --- services ---------------------------------------------------------------------------------


def get_event_service(session: SessionDep, container: ContainerDep) -> EventService:
    return EventService(session, EventRepository(session), container.cache)


def get_booking_service(session: SessionDep, container: ContainerDep) -> BookingService:
    return BookingService(session, BookingRepository(session), container.booking_policy)


def get_auth_service(session: SessionDep, container: ContainerDep) -> AuthService:
    return AuthService(session, UserRepository(session), container.tokens, container.hasher)


EventServiceDep = Annotated[EventService, Depends(get_event_service)]
BookingServiceDep = Annotated[BookingService, Depends(get_booking_service)]
AuthServiceDep = Annotated[AuthService, Depends(get_auth_service)]

# --- identity ---------------------------------------------------------------------------------

_bearer = HTTPBearer(auto_error=False, description="JWT access token; buyer id = `sub`")


async def get_current_user(
    container: ContainerDep,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> Principal:
    if credentials is None:
        raise AuthenticationError("Bearer token is required")
    principal = container.tokens.verify(credentials.credentials)
    structlog.contextvars.bind_contextvars(user_id=str(principal.user_id))
    return principal


CurrentUser = Annotated[Principal, Depends(get_current_user)]


async def require_organizer(user: CurrentUser) -> Principal:
    if user.role != Role.ORGANIZER:
        raise PermissionDeniedError("Organizer role is required")
    return user


Organizer = Annotated[Principal, Depends(require_organizer)]


async def limit_hold_rate(user: CurrentUser, container: ContainerDep) -> None:
    await container.rate_limiter.hit("hold", str(user.user_id))
