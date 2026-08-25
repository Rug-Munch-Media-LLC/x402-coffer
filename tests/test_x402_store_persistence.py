"""Tests for PaymentStore Redis persistence (W2)."""

from __future__ import annotations

import pytest

from coffer._engine.store import PaymentStore


def test_no_redis_configured_is_memory_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("REDIS_URL", raising=False)
    monkeypatch.delenv("X402_REDIS_URL", raising=False)
    store = PaymentStore()
    assert store._redis is None
    store.record(payment_id="p1", caller_key="c1")
    assert store.is_paid("p1", "c1") is True


def test_bad_redis_url_degrades_to_memory(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("X402_REDIS_URL", "redis://127.0.0.1:1/0")  # nothing listens
    store = PaymentStore()
    assert store._redis is None  # ping failed -> degraded, never raised
    store.record(payment_id="p1", caller_key="c1")
    assert store.is_paid("p1", "c1") is True


def test_record_survives_new_store_instance_via_mirror(monkeypatch: pytest.MonkeyPatch) -> None:
    """The restart scenario: record on one instance, read from a fresh one."""
    fakeredis = _FakeRedis()
    monkeypatch.setattr("coffer._engine.store.PaymentStore.__init__", PaymentStore.__init__)

    store1 = PaymentStore()
    object.__setattr__(store1, "_redis", None)
    # Inject the fake client directly (no live redis in CI).
    store1._redis = fakeredis
    store1.record(payment_id="pid-x", caller_key="9.9.9.9")

    store2 = PaymentStore()
    store2._redis = fakeredis
    # Fresh instance: memory empty, mirror has it.
    assert store2._records == {}
    assert store2.is_paid("pid-x", "9.9.9.9") is True
    # Re-hydrated into memory for repeat reads.
    assert "pid-x" in store2._records


class _FakeRedis:
    """Minimal sync-redis stand-in (setex/get with TTL ignored)."""

    def __init__(self) -> None:
        self.data: dict[str, str] = {}

    def setex(self, key: str, ttl: int, value: str) -> None:
        self.data[key] = value

    def get(self, key: str) -> str | None:
        return self.data.get(key)

    def ping(self) -> bool:
        return True
