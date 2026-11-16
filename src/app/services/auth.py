import uuid

import anyio.to_thread
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import EmailTakenError, InvalidCredentialsError
from app.core.security import IssuedToken, PasswordHasher, TokenService
from app.db.models import User
from app.domain.enums import Role
from app.domain.views import UserView
from app.repositories.users import UserRepository


class AuthService:
    """Minimal built-in identity provider: register + login -> JWT.

    argon2 is deliberately slow, so hashing runs in a worker thread and never blocks
    the event loop that serves seat holds.
    """

    def __init__(
        self,
        session: AsyncSession,
        users: UserRepository,
        tokens: TokenService,
        hasher: PasswordHasher,
    ) -> None:
        self._session = session
        self._users = users
        self._tokens = tokens
        self._hasher = hasher

    async def register(self, email: str, password: str, role: Role = Role.BUYER) -> UserView:
        password_hash = await anyio.to_thread.run_sync(self._hasher.hash, password)
        user = User(id=uuid.uuid4(), email=email.lower(), password_hash=password_hash, role=role)
        try:
            async with self._session.begin():
                await self._users.add(user)
        except IntegrityError as exc:
            raise EmailTakenError() from exc
        return UserView(id=user.id, email=user.email, role=user.role)

    async def login(self, email: str, password: str) -> IssuedToken:
        user = await self._users.get_by_email(email.lower())
        # Always verify (against a dummy hash for unknown emails): equal timing, no enumeration.
        ok = await anyio.to_thread.run_sync(
            self._hasher.verify, user.password_hash if user else None, password
        )
        if user is None or not ok:
            raise InvalidCredentialsError()
        return self._tokens.issue(user.id, user.role)
