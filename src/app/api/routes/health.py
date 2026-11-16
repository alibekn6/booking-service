from fastapi import APIRouter
from fastapi.responses import JSONResponse
from redis.exceptions import RedisError
from sqlalchemy import text

from app.api.deps import ContainerDep

router = APIRouter(tags=["health"])


@router.get("/health", summary="Liveness")
async def live() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/health/ready", summary="Readiness: Postgres required, Redis optional")
async def ready(container: ContainerDep) -> JSONResponse:
    checks: dict[str, str] = {}
    try:
        async with container.engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        checks["postgres"] = "ok"
    except Exception as exc:
        checks["postgres"] = f"error: {type(exc).__name__}"
    try:
        await container.redis.ping()
        checks["redis"] = "ok"
    except (RedisError, OSError) as exc:
        checks["redis"] = f"degraded: {type(exc).__name__}"  # optimisation only
    ok = checks["postgres"] == "ok"
    return JSONResponse(
        {"status": "ok" if ok else "fail", **checks}, status_code=200 if ok else 503
    )
