from functools import lru_cache
from typing import Literal

from pydantic import Field, PostgresDsn, RedisDsn, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All configuration comes from the environment (12-factor; ECS task env on AWS)."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    env: Literal["local", "test", "prod"] = "local"
    log_level: str = "INFO"
    log_json: bool = True
    access_log: bool = True

    database_url: PostgresDsn = PostgresDsn(
        "postgresql+asyncpg://booking:booking@localhost:5433/booking"
    )
    redis_url: RedisDsn = RedisDsn("redis://localhost:6380/0")

    # --- auth -------------------------------------------------------------------------------
    # HS256 + shared secret for the MVP. For RS256/ES256 put a PEM key here instead.
    jwt_secret: SecretStr = SecretStr("dev-only-secret-change-me-0123456789abcdef")
    jwt_algorithm: str = "HS256"
    jwt_ttl_s: int = Field(3600, gt=0)
    jwt_issuer: str | None = None
    jwt_audience: str | None = None

    # --- business rules ----------------------------------------------------------------------
    hold_ttl_s: int = Field(600, gt=0)
    max_seats_per_buyer: int = Field(4, gt=0)

    # --- postgres ------------------------------------------------------------------------------
    db_pool_size: int = 20
    db_max_overflow: int = 0
    db_pool_timeout_s: float = 5.0
    db_statement_timeout_ms: int = 5000
    db_lock_timeout_ms: int = 2000

    # --- redis: optimisation only, never on the correctness path ------------------------------
    # Snapshot age <= read_cache_ttl_ms + read_cache_local_ttl_ms; must stay under 2000 ms.
    read_cache_ttl_ms: int = Field(1000, ge=0)  # L2 (Redis), shared; 0 disables caching
    read_cache_local_ttl_ms: int = Field(500, ge=0)  # L1 (process memory)
    hold_rate_limit_per_minute: int = Field(30, ge=0)  # per buyer; 0 disables

    # --- sweeper -------------------------------------------------------------------------------
    sweeper_interval_s: float = 1.0
    sweeper_batch_size: int = 1000


@lru_cache
def get_settings() -> Settings:
    return Settings()
