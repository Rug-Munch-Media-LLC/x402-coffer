# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Rug Munch Media LLC
# --- replay_redis.py: Redis-backed tx_hash nonce replay protection ---
"""Redis-backed replay store for multi-instance deployments.

The in-memory :class:`ReplayStore` (``replay.py``) is only safe within a single
process: when Coffer runs as several instances behind a load balancer each
instance keeps its own nonce set, so a replayed transaction can pass on a
different instance. :class:`RedisReplayStore` moves the nonce burn into a
shared Redis so the check-and-burn is atomic *across* instances.

Design
------
- ``check_and_burn`` uses Redis ``SETNX`` (``SET ... NX``) for the atomic
  claim, then ``EXPIRE`` for the TTL. Returns ``True`` exactly once per key;
  every later call returns ``False`` (replay).
- ``check`` is a non-destructive membership probe (``GET``).
- If Redis is unavailable (module missing, bad URL, connection error) we fall
  back to a bounded in-process set and log a warning — the service keeps
  running, but replay protection degrades to single-instance semantics.

Config
------
- ``X402_REDIS_URL`` (or legacy ``REDIS_URL``): when set, the factory
  :func:`create_replay_store` returns a :class:`RedisReplayStore`; when unset
  it returns the in-memory :class:`ReplayStore`.
- ``X402_NONCE_TTL``: replay-key TTL in seconds (default 86400 = 24h).
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

from .replay import REPLAY_HTTP_STATUS, ReplayStore

log = logging.getLogger(__name__)

REDIS_URL = os.getenv("X402_REDIS_URL", os.getenv("REDIS_URL", ""))
X402_NONCE_TTL = int(os.getenv("X402_NONCE_TTL", "86400"))  # 24h


class RedisReplayStore:
    """tx_hash nonce burn via Redis SETNX + EXPIRE; in-memory fallback.

    Shares the :class:`ReplayStore` interface (``check_and_burn`` /
    ``check`` / ``uses_redis``) so it can be swapped in transparently. The
    in-memory fallback is bounded and guarded by an asyncio lock so the
    check-then-add stays atomic within one process; Redis remains canonical
    for multi-instance deployments.
    """

    def __init__(
        self,
        redis_url: str | None = None,
        ttl: int = X402_NONCE_TTL,
        max_mem: int = 50_000,
    ) -> None:
        self.ttl = int(ttl)
        self._max_mem = int(max_mem)
        self._mem: set[str] = set()
        self._redis: Any = None
        self._redis_broken = False
        self._client_loop: asyncio.AbstractEventLoop | None = None
        self._lock = asyncio.Lock()
        self._redis_url = redis_url if redis_url is not None else REDIS_URL
        if self._redis_url:
            try:
                from redis import asyncio as aioredis  # type: ignore[import-untyped]

                self._redis = aioredis.from_url(self._redis_url, decode_responses=True)
            except Exception as e:  # noqa: BLE001 - pragma: no cover - defensive
                log.warning("x402_replay_redis_unavailable", extra={"error": str(e)})
                self._redis_broken = True

    def _active_client(self) -> Any:
        """Return the Redis client bound to the RUNNING event loop.

        A client whose connections were created in a previous event loop is
        unusable (``asyncio.run()`` per call, embedded scripts, test helpers).
        When the running loop changes we rebuild the client so replay
        protection keeps working instead of silently degrading to the
        per-process memory fallback. Injected clients (tests) are adopted on
        first use.
        """
        if self._redis is None or self._redis_broken:
            return None
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:  # pragma: no cover - no running loop: use as-is
            return self._redis
        if self._client_loop is None:
            self._client_loop = loop  # adopt (covers injected test clients)
        elif self._client_loop is not loop:
            try:
                from redis import asyncio as aioredis  # type: ignore[import-untyped]

                self._redis = aioredis.from_url(self._redis_url, decode_responses=True)
                self._client_loop = loop
            except Exception as e:  # noqa: BLE001 - pragma: no cover - defensive
                log.warning("x402_replay_redis_unavailable", extra={"error": str(e)})
                self._redis_broken = True
                self._redis = None
        return self._redis

    @property
    def uses_redis(self) -> bool:
        return self._redis is not None

    async def check_and_burn(self, key: str) -> bool:
        """Atomically consume ``key``. Returns True when newly consumed.

        False means the key was already consumed -> replay. Uses Redis
        ``SETNX`` (``SET ... NX``) for the atomic claim followed by
        ``EXPIRE`` for the TTL; falls back to the in-memory set on error.
        """
        client = self._active_client()
        if client is not None:
            try:
                # SETNX: atomic claim of the nonce (only succeeds once).
                ok = await client.set(key, "1", nx=True)
                if ok:
                    # EXPIRE: bound the key's lifetime (24h default).
                    await client.expire(key, self.ttl)
                return bool(ok)
            except Exception as e:  # noqa: BLE001 - pragma: no cover - runtime redis failure
                log.warning("x402_replay_redis_error", extra={"error": str(e)})
                self._redis_broken = True
                self._redis = None
        async with self._lock:
            if key in self._mem:
                return False
            self._mem.add(key)
            if len(self._mem) > self._max_mem:
                self._mem = set(list(self._mem)[-self._max_mem :])
            return True

    async def check(self, key: str) -> bool:
        """Non-destructive membership check (True when already used)."""
        client = self._active_client()
        if client is not None:
            try:
                exists = await client.get(key)
                return bool(exists)
            except Exception:  # noqa: BLE001 - pragma: no cover
                return key in self._mem
        return key in self._mem


def create_replay_store(
    redis_url: str | None = None,
    ttl: int = X402_NONCE_TTL,
) -> ReplayStore:
    """Select the replay store backend based on ``X402_REDIS_URL``.

    - ``X402_REDIS_URL`` set  -> :class:`RedisReplayStore` (shared, safe
      across instances behind a load balancer).
    - ``X402_REDIS_URL`` unset -> in-memory :class:`ReplayStore` (single
      instance only).

    Returns the common :class:`ReplayStore` interface so callers never need
    to branch on the backend. The env var is read at call time (not import
    time) so tests and late configuration can influence the selection.
    """
    url = redis_url if redis_url is not None else os.getenv("X402_REDIS_URL", os.getenv("REDIS_URL", ""))
    if url:
        return RedisReplayStore(redis_url=url, ttl=ttl)  # type: ignore[return-value]
    # redis_url="" forces the in-memory backend even when the module-level
    # REDIS_URL constant was captured from the environment at import time.
    return ReplayStore(ttl=ttl, redis_url="")


__all__ = [
    "REDIS_URL",
    "REPLAY_HTTP_STATUS",
    "X402_NONCE_TTL",
    "RedisReplayStore",
    "create_replay_store",
]
