# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Rug Munch Media LLC
# --- replay.py: tx_hash nonce replay protection (Redis SETNX / in-memory) ---
from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

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

    def __init__(
        self,
        ttl: int = X402_NONCE_TTL,
        max_mem: int = 50_000,
        redis_url: str | None = None,
    ) -> None:
        self.ttl = int(ttl)
        self._max_mem = int(max_mem)
        self._mem: set[str] = set()
        self._redis: Any = None
        self._redis_broken = False
        self._client_loop: asyncio.AbstractEventLoop | None = None
        # None -> module REDIS_URL (import-time env); "" -> force in-memory
        # (used by the factory when no URL is configured).
        self._redis_url = redis_url if redis_url is not None else REDIS_URL
        # H2: serialize the in-memory fallback so check-then-add is atomic
        # within one process (Redis SETNX remains canonical for production).
        self._lock = asyncio.Lock()
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
        When the running loop changes we rebuild the client from REDIS_URL so
        replay protection keeps working instead of silently degrading to the
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

        False means the key was already consumed -> replay. Lock-free via
        ``SET NX EX`` (SETNX) against Redis; in-memory fallback otherwise.
        """
        client = self._active_client()
        if client is not None:
            try:
                # Redis SETNX with 24h TTL — atomic claim of the nonce.
                ok = await client.set(key, "1", ex=self.ttl, nx=True)
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
