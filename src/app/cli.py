"""Operational commands: python -m app.cli <command>.

migrate                      apply Alembic migrations (container start-up job)
sweeper                      run the expired-hold sweeper loop
create-user                  create a user, e.g. the first organizer
issue-token                  mint a JWT signed with the configured key (dev/load tests)
check-invariants             audit the DB after a load test; exit 1 on any violation
"""

import argparse
import asyncio
import sys
import uuid
from decimal import Decimal
from typing import Any

import orjson

from app.core.config import get_settings
from app.core.logging import configure_logging
from app.core.security import PasswordHasher, TokenService
from app.db.migrate import upgrade_head
from app.db.models import User
from app.db.session import create_engine, create_session_factory
from app.domain.enums import Role
from app.invariants import audit
from app.workers.sweeper import run_sweeper


def _token_service() -> TokenService:
    s = get_settings()
    return TokenService(
        secret=s.jwt_secret.get_secret_value(),
        algorithm=s.jwt_algorithm,
        ttl_s=s.jwt_ttl_s,
        issuer=s.jwt_issuer,
        audience=s.jwt_audience,
    )


async def _create_user(email: str, password: str, role: Role) -> uuid.UUID:
    engine = create_engine(get_settings(), application_name="booking-cli")
    try:
        async with create_session_factory(engine)() as session, session.begin():
            user = User(
                id=uuid.uuid4(),
                email=email.lower(),
                password_hash=PasswordHasher().hash(password),
                role=role,
            )
            session.add(user)
        return user.id
    finally:
        await engine.dispose()


async def _check_invariants(grace_s: float) -> bool:
    settings = get_settings()
    engine = create_engine(settings, application_name="booking-audit")
    try:
        async with engine.connect() as conn:
            report = await audit(conn, max_seats=settings.max_seats_per_buyer, grace_s=grace_s)
    finally:
        await engine.dispose()

    def default(obj: Any) -> str:
        # asyncpg returns its own uuid.UUID subclass, which orjson does not recognise
        if isinstance(obj, Decimal | uuid.UUID):
            return str(obj)
        raise TypeError(type(obj).__name__)

    print(
        orjson.dumps(
            {"ok": report.ok, "events": report.events, "violations": report.violations},
            default=default,
            option=orjson.OPT_INDENT_2,
        ).decode()
    )
    return report.ok


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("migrate")
    sub.add_parser("sweeper")
    user = sub.add_parser("create-user")
    user.add_argument("--email", required=True)
    user.add_argument("--password", required=True)
    user.add_argument("--role", choices=[r.value for r in Role], default=Role.BUYER.value)
    token = sub.add_parser("issue-token")
    token.add_argument("--sub", default=None, help="user id (UUID); random if omitted")
    token.add_argument("--role", choices=[r.value for r in Role], default=Role.BUYER.value)
    token.add_argument("--ttl", type=int, default=3600)
    check = sub.add_parser("check-invariants")
    check.add_argument("--grace-s", type=float, default=60, help="sweeper lag tolerated")
    args = parser.parse_args(argv)

    settings = get_settings()
    configure_logging(settings.log_level, json=settings.log_json)

    match args.command:
        case "migrate":
            upgrade_head(str(settings.database_url))
        case "sweeper":
            asyncio.run(run_sweeper(settings))
        case "create-user":
            print(asyncio.run(_create_user(args.email, args.password, Role(args.role))))
        case "issue-token":
            sub_id = uuid.UUID(args.sub) if args.sub else uuid.uuid4()
            print(_token_service().issue(sub_id, Role(args.role), ttl_s=args.ttl).token)
        case "check-invariants":
            return 0 if asyncio.run(_check_invariants(args.grace_s)) else 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
