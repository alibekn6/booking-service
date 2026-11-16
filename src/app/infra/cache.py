"""Two-level cache for hot read snapshots (seat map, event card).

At on-sale time tens of thousands of clients refresh the seat map. A snapshot may be
slightly stale (well under 2 s): it is only a hint, every hold is re-validated in
Postgres under row locks, so the cache can never cause an oversell.

  * L1: per-process memory, `local_ttl_ms`. Serves the bulk of requests with no I/O.
  * L2: Redis, `ttl_ms`. Shares one rebuild per key across all processes/hosts.
  Worst-case age of a served snapshot ~ ttl_ms + local_ttl_ms (+ one rebuild).

Snapshots are stored gzip-compressed (compressed once per rebuild, not per request):
~10x less to move through Redis, the event loop and the network.

Concurrent misses are coalesced: in-process via a shared future, across processes via
a Redis SET NX lock. If Redis is down we fall back to L1 + Postgres: degraded, not broken.
"""

import asyncio
import contextlib
import gzip
import time
from collections.abc import Awaitable, Callable

from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.core.logging import get_logger

log = get_logger(__name__)

Compute = Callable[[], Awaitable[bytes]]

_GZIP_LEVEL = 5
_L1_MAX_KEYS = 10_000


class Snapshot:
    """A JSON document kept in both encodings, each produced at most once."""

    __slots__ = ("_gzipped", "_raw")

    def __init__(self, *, raw: bytes | None = None, gzipped: bytes | None = None) -> None:
        if raw is None and gzipped is None:
            raise ValueError("raw or gzipped is required")
        self._raw = raw
        self._gzipped = gzipped

    @property
    def raw(self) -> bytes:
        if self._raw is None:
            self._raw = gzip.decompress(self._gzipped or b"")
        return self._raw

    @property
    def gzipped(self) -> bytes:
        if self._gzipped is None:
            self._gzipped = gzip.compress(self.raw, compresslevel=_GZIP_LEVEL, mtime=0)
        return self._gzipped


def _as_bytes(value: bytes | str | None) -> bytes | None:
    return value.encode() if isinstance(value, str) else value


class ReadCache:
    def __init__(
        self,
        redis: Redis,
        ttl_ms: int,
        local_ttl_ms: int = 0,
        follower_wait_ms: int = 250,
    ) -> None:
        self._redis = redis
        self._ttl_ms = ttl_ms
        self._local_ttl_s = local_ttl_ms / 1000
        self._follower_wait_s = follower_wait_ms / 1000
        self._local: dict[str, tuple[float, Snapshot]] = {}
        self._inflight: dict[str, asyncio.Future[Snapshot]] = {}

    async def get_or_compute(self, key: str, compute: Compute) -> Snapshot:
        if self._ttl_ms <= 0:
            return Snapshot(raw=await compute())

        local = self._local.get(key)
        if local is not None and local[0] > time.monotonic():
            return local[1]

        inflight = self._inflight.get(key)
        if inflight is not None:
            try:
                return await asyncio.shield(inflight)
            except asyncio.CancelledError:
                if inflight.cancelled():  # the leader went away, not us
                    return await self._fetch(key, compute)
                raise

        future: asyncio.Future[Snapshot] = asyncio.get_running_loop().create_future()
        self._inflight[key] = future
        try:
            snapshot = await self._fetch(key, compute)
        except asyncio.CancelledError:
            future.cancel()
            raise
        except Exception as exc:
            future.set_exception(exc)
            future.exception()  # mark retrieved: no "exception never retrieved" warnings
            raise
        else:
            future.set_result(snapshot)
            self._remember(key, snapshot)
            return snapshot
        finally:
            self._inflight.pop(key, None)

    def _remember(self, key: str, snapshot: Snapshot) -> None:
        if self._local_ttl_s <= 0:
            return
        now = time.monotonic()
        if len(self._local) >= _L1_MAX_KEYS:
            self._local = {k: v for k, v in self._local.items() if v[0] > now}
        self._local[key] = (now + self._local_ttl_s, snapshot)

    async def _fetch(self, key: str, compute: Compute) -> Snapshot:
        lock_key = f"{key}:lock"
        try:
            cached = _as_bytes(await self._redis.get(key))
            if cached is not None:
                return Snapshot(gzipped=cached)
            leader = await self._redis.set(lock_key, b"1", nx=True, px=max(self._ttl_ms, 1000))
        except (RedisError, OSError) as exc:
            log.warning("cache_unavailable", key=key, error=repr(exc))
            return Snapshot(raw=await compute())

        if not leader:
            cached = await self._wait_for_leader(key)
            if cached is not None:
                return Snapshot(gzipped=cached)
            return Snapshot(raw=await compute())

        try:
            snapshot = Snapshot(raw=await compute())
            try:
                await self._redis.set(key, snapshot.gzipped, px=self._ttl_ms)
            except (RedisError, OSError) as exc:
                log.warning("cache_set_failed", key=key, error=repr(exc))
            return snapshot
        finally:
            with contextlib.suppress(RedisError, OSError):
                await self._redis.delete(lock_key)

    async def _wait_for_leader(self, key: str) -> bytes | None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._follower_wait_s
        while loop.time() < deadline:
            await asyncio.sleep(0.01)
            try:
                cached = _as_bytes(await self._redis.get(key))
            except (RedisError, OSError):
                return None
            if cached is not None:
                return cached
        return None
