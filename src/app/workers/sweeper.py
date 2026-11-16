"""Frees seats of expired holds and marks those holds expired.

Correctness never depends on this process: an expired hold already counts as free in
every query (lazy expiry). The sweeper makes the *stored* state converge so
that rows don't keep pointing at dead holds.

SKIP LOCKED: it never waits for, nor blocks, a request, and several replicas are safe.
"""

import asyncio
import contextlib
import signal
from dataclasses import dataclass

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.config import Settings
from app.core.logging import get_logger
from app.db.models import Hold, Seat
from app.db.session import create_engine
from app.db.sql import db_now
from app.domain.enums import HoldStatus, SeatStatus

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class SweepResult:
    seats_released: int
    holds_expired: int


class Sweeper:
    def __init__(self, engine: AsyncEngine, batch_size: int) -> None:
        self._engine = engine
        self._batch_size = batch_size

    async def sweep_once(self) -> SweepResult:
        async with self._engine.begin() as conn:
            expired_seats = (
                select(Seat.id)
                .where(Seat.status == SeatStatus.HELD, Seat.held_until <= db_now())
                .order_by(Seat.held_until)
                .limit(self._batch_size)
                .with_for_update(skip_locked=True)
                .cte("expired_seats")
            )
            seats = await conn.execute(
                update(Seat)
                .where(
                    Seat.id.in_(select(expired_seats.c.id)),
                    Seat.status == SeatStatus.HELD,
                    Seat.held_until <= db_now(),
                )
                .values(status=SeatStatus.AVAILABLE, hold_id=None, buyer_id=None, held_until=None)
            )
        async with self._engine.begin() as conn:
            lapsed_holds = (
                select(Hold.id)
                .where(Hold.status == HoldStatus.ACTIVE, Hold.expires_at <= db_now())
                .order_by(Hold.expires_at)
                .limit(self._batch_size)
                .with_for_update(skip_locked=True)
                .cte("lapsed_holds")
            )
            holds = await conn.execute(
                update(Hold)
                .where(Hold.id.in_(select(lapsed_holds.c.id)))
                .values(status=HoldStatus.EXPIRED, closed_at=Hold.expires_at)
            )
        return SweepResult(seats.rowcount, holds.rowcount)

    async def run(self, stop: asyncio.Event, interval_s: float) -> None:
        log.info("sweeper_started", interval_s=interval_s, batch_size=self._batch_size)
        while not stop.is_set():
            try:
                result = await self.sweep_once()
            except Exception:
                log.exception("sweep_failed")
                result = SweepResult(0, 0)
            if result.seats_released or result.holds_expired:
                log.info(
                    "sweep",
                    seats_released=result.seats_released,
                    holds_expired=result.holds_expired,
                )
            backlog = max(result.seats_released, result.holds_expired) >= self._batch_size
            if not backlog:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=interval_s)
        log.info("sweeper_stopped")


async def run_sweeper(settings: Settings) -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    engine = create_engine(settings, application_name="booking-sweeper")
    try:
        await Sweeper(engine, settings.sweeper_batch_size).run(stop, settings.sweeper_interval_s)
    finally:
        await engine.dispose()
