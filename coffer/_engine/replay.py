# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Rug Munch Media LLC
# --- replay.py: tx_hash nonce replay protection (Redis SETNX / in-memory) ---
from __future__ import annotations

import asyncio
import logging
import os

log = logging.getLogger(__name__)

REDIS_URL = os.getenv("X402_REDIS_URL", os.getenv("REDIS_URL", ""))
X402_NONCE_TTL = int(os.getenv("X402_NONCE_TTL", "86400"))  # 24h

# Replay rejection HTTP status (410-style) surfaced so routers can map it.
REPLAY_HTTP_STATUS = 410
# ── Replay protection (Redis SETNX nonce burn, 24h TTL) ──────────────────────


class ReplayStore:
    """tx_hash nonce burn via Redis SETNX (24h TTL); in-memory fallback.

    Redis is optional (guard against redis module/REDIS_URL absence): when
    unavailable we fall back to a bounded in-process set. [REVIEW] — in-memory
    is NOT safe across app instances; production MUST configure REDIS_URL.
    """

    def __init__(self, ttl: int = X402_NONCE_TTL, max_mem: int = 50_000) -> None:
        self.ttl = int(ttl)
        self._max_mem = int(max_mem)
        self._mem: set[str] = set()
        self._redis = None
        self._redis_broken = False
        # H2: serialize the in-memory fallback so check-then-add is atomic
        # within one process (Redis SETNX remains canonical for production).
        self._lock = asyncio.Lock()
        if REDIS_URL:
            try:
                from redis import asyncio as aioredis  # type: ignore[import-untyped]

                self._redis = aioredis.from_url(REDIS_URL, decode_responses=True)
            except Exception as e:  # pragma: no cover - defensive
                log.warning("x402_replay_redis_unavailable", extra={"error": str(e)})
                self._redis_broken = True

    @property
    def uses_redis(self) -> bool:
        return self._redis is not None

    async def check_and_burn(self, key: str) -> bool:
        """Atomically consume ``key``. Returns True when newly consumed.

        False means the key was already consumed -> replay. Lock-free via
        ``SET NX EX`` (SETNX) against Redis; in-memory fallback otherwise.
        """
        if self._redis is not None:
            try:
                # Redis SETNX with 24h TTL — atomic claim of the nonce.
                ok = await self._redis.set(key, "1", ex=self.ttl, nx=True)
                return bool(ok)
            except Exception as e:  # pragma: no cover - runtime redis failure
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
        if self._redis is not None:
            try:
                exists = await self._redis.get(key)
                return bool(exists)
            except Exception:  # pragma: no cover
                return key in self._mem
        return key in self._mem
