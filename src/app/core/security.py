"""JWT and password hashing (PyJWT + argon2-cffi, no passlib)."""

import time
import uuid
from dataclasses import dataclass

import jwt
from argon2 import PasswordHasher as _Argon2
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from app.core.errors import AuthenticationError
from app.domain.enums import Role


@dataclass(frozen=True, slots=True)
class Principal:
    user_id: uuid.UUID
    role: Role


@dataclass(frozen=True, slots=True)
class IssuedToken:
    token: str
    expires_in: int


class TokenService:
    """Issues and verifies access tokens. Buyer identity = `sub` (a UUID)."""

    def __init__(
        self,
        *,
        secret: str,
        algorithm: str,
        ttl_s: int,
        issuer: str | None = None,
        audience: str | None = None,
        leeway_s: int = 5,
    ) -> None:
        self._secret = secret
        self._algorithm = algorithm
        self._ttl_s = ttl_s
        self._issuer = issuer
        self._audience = audience
        self._leeway_s = leeway_s

    def issue(self, user_id: uuid.UUID, role: Role, *, ttl_s: int | None = None) -> IssuedToken:
        ttl = ttl_s or self._ttl_s
        now = int(time.time())
        claims: dict[str, object] = {
            "sub": str(user_id),
            "role": role.value,
            "iat": now,
            "exp": now + ttl,
        }
        if self._issuer:
            claims["iss"] = self._issuer
        if self._audience:
            claims["aud"] = self._audience
        return IssuedToken(jwt.encode(claims, self._secret, algorithm=self._algorithm), ttl)

    def verify(self, token: str) -> Principal:
        try:
            claims = jwt.decode(
                token,
                self._secret,
                algorithms=[self._algorithm],
                issuer=self._issuer,
                audience=self._audience,
                leeway=self._leeway_s,
                options={"require": ["sub", "exp"], "verify_aud": self._audience is not None},
            )
            user_id = uuid.UUID(str(claims["sub"]))
            role = Role(claims.get("role", Role.BUYER))
        except jwt.ExpiredSignatureError as exc:
            raise AuthenticationError("Token has expired") from exc
        except (jwt.InvalidTokenError, ValueError) as exc:
            raise AuthenticationError("Invalid token") from exc
        return Principal(user_id=user_id, role=role)


class PasswordHasher:
    """argon2id. CPU-heavy by design: call from a worker thread, not the event loop."""

    def __init__(self) -> None:
        self._argon2 = _Argon2()
        # Verified against when the user does not exist, so timing doesn't reveal emails.
        self._dummy_hash = self._argon2.hash("timing-equaliser")

    def hash(self, password: str) -> str:
        return self._argon2.hash(password)

    def verify(self, password_hash: str | None, password: str) -> bool:
        try:
            return self._argon2.verify(password_hash or self._dummy_hash, password) and bool(
                password_hash
            )
        except (VerifyMismatchError, VerificationError, InvalidHashError):
            return False
