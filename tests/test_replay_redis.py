# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Rug Munch Media LLC
# --- test_replay_redis.py: RedisReplayStore + factory selection tests ---
"""Tests for the Redis-backed replay store (multi-instance replay protection).

Covers:
- atomic check-and-burn (same key returns True once, then False)
- TTL expiry (key is forgotten after the TTL elapses)
- fallback to in-memory when Redis is unavailable
- factory selection based on X402_REDIS_URL
"""

from __future__ import annotations

import asyncio
from typing import Any

import fakeredis
import pytest

from coffer._engine.replay import ReplayStore
from coffer._engine.replay_redis import RedisReplayStore, create_replay_store


@pytest.fixture
def fake_redis() -> Any:
    """A shared in-process fakeredis server (async, decode_responses=True)."""
    server = fakeredis.FakeServer()
    client = fakeredis.FakeAsyncRedis(server=server, decode_responses=True)
    return client


@pytest.fixture
def redis_store(fake_redis: Any) -> RedisReplayStore:
    """A RedisReplayStore wired to the fakeredis client (short TTL)."""
    store = RedisReplayStore(redis_url="redis://fake", ttl=60)
    # Inject the fakeredis client directly so no real network is touched.
    store._redis = fake_redis
    return store


async def test_check_and_burn_atomic(redis_store: RedisReplayStore) -> None:
    """Same key returns True once, then False (replay)."""
    key = "x402:replay:eip155:8453:0xabc"
    assert await redis_store.check_and_burn(key) is True
    assert await redis_store.check_and_burn(key) is False
    assert await redis_store.check_and_burn(key) is False


async def test_check_non_destructive(redis_store: RedisReplayStore) -> None:
    """``check`` reports membership without consuming the key."""
    key = "x402:replay:eip155:8453:0xdef"
    assert await redis_store.check(key) is False
    assert await redis_store.check_and_burn(key) is True
    assert await redis_store.check(key) is True
    # check() does not consume — a subsequent burn still reports replay.
    assert await redis_store.check_and_burn(key) is False


async def test_distinct_keys_independent(redis_store: RedisReplayStore) -> None:
    """Different keys do not collide."""
    assert await redis_store.check_and_burn("key-a") is True
    assert await redis_store.check_and_burn("key-b") is True
    assert await redis_store.check_and_burn("key-a") is False
    assert await redis_store.check_and_burn("key-b") is False


async def test_ttl_expiry(redis_store: RedisReplayStore) -> None:
    """Key is forgotten after the TTL elapses (replay allowed again)."""
    key = "x402:replay:eip155:8453:0xexpire"
    assert await redis_store.check_and_burn(key) is True
    assert await redis_store.check_and_burn(key) is False
    # Simulate TTL expiry by deleting the key (fakeredis honours real TTLs,
    # but we drive expiry deterministically here).
    await redis_store._redis.delete(key)
    assert await redis_store.check_and_burn(key) is True


async def test_ttl_set_on_burn(redis_store: RedisReplayStore) -> None:
    """EXPIRE is applied so the key does not persist forever."""
    key = "x402:replay:eip155:8453:0xttl"
    await redis_store.check_and_burn(key)
    ttl = await redis_store._redis.ttl(key)
    assert 0 < ttl <= 60


async def test_fallback_to_memory_when_redis_unavailable() -> None:
    """Redis unavailable -> in-memory fallback still burns (single instance)."""
    # A URL that cannot connect: the client is created but every op fails,
    # so check_and_burn must degrade to the in-memory set without raising.
    store = RedisReplayStore(redis_url="redis://127.0.0.1:1", ttl=60)
    # Force the client to fail fast by pointing at a dead port.
    store._redis = fakeredis.FakeAsyncRedis(server=fakeredis.FakeServer(), decode_responses=True)

    # Simulate a broken backend: replace with a client that raises on use.
    class _Broken:
        async def set(self, *a: Any, **k: Any) -> None:
            raise ConnectionError("redis down")

        async def expire(self, *a: Any, **k: Any) -> None:
            raise ConnectionError("redis down")

        async def get(self, *a: Any, **k: Any) -> None:
            raise ConnectionError("redis down")

    store._redis = _Broken()  # type: ignore[assignment]
    key = "x402:replay:eip155:8453:0xfallback"
    assert await store.check_and_burn(key) is True
    assert await store.check_and_burn(key) is False
    # After the failure the store has dropped Redis and is purely in-memory.
    assert store.uses_redis is False


async def test_fallback_when_redis_module_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Import failure of the redis module -> in-memory fallback, no crash."""
    import builtins

    real_import = builtins.__import__

    def _block_redis(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "redis" or name.startswith("redis."):
            raise ImportError("redis module unavailable")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _block_redis)
    store = RedisReplayStore(redis_url="redis://fake", ttl=60)
    assert store.uses_redis is False
    key = "x402:replay:eip155:8453:0xnomod"
    assert await store.check_and_burn(key) is True
    assert await store.check_and_burn(key) is False


async def test_factory_selects_redis_when_url_set(monkeypatch: pytest.MonkeyPatch) -> None:
    """X402_REDIS_URL set -> RedisReplayStore."""
    monkeypatch.setenv("X402_REDIS_URL", "redis://fake:6379/0")
    store = create_replay_store()
    assert isinstance(store, RedisReplayStore)
    assert store.uses_redis is True


async def test_factory_selects_memory_when_url_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """X402_REDIS_URL unset -> in-memory ReplayStore."""
    monkeypatch.delenv("X402_REDIS_URL", raising=False)
    monkeypatch.delenv("REDIS_URL", raising=False)
    store = create_replay_store()
    assert isinstance(store, ReplayStore)
    assert store.uses_redis is False


async def test_factory_explicit_url_overrides_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Explicit redis_url argument wins over the environment."""
    monkeypatch.delenv("X402_REDIS_URL", raising=False)
    store = create_replay_store(redis_url="redis://explicit:6379/0")
    assert isinstance(store, RedisReplayStore)


async def test_concurrent_burn_single_winner(redis_store: RedisReplayStore) -> None:
    """Concurrent burns of the same key yield exactly one True."""
    key = "x402:replay:eip155:8453:0xrace"
    results = await asyncio.gather(*[redis_store.check_and_burn(key) for _ in range(20)])
    assert sum(results) == 1
