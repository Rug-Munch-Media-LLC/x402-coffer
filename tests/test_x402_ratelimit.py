"""Tests for per-caller rate limiting."""

from __future__ import annotations

import pytest

from coffer._engine.ratelimit import RateLimiter, check_rate, get_rate_limiter


def test_allows_under_limit() -> None:
    rl = RateLimiter(5, window_seconds=60)
    for _ in range(5):
        ok, retry = rl.allow("a", now=100.0)
        assert ok is True and retry == 0


def test_blocks_over_limit_with_retry_after() -> None:
    rl = RateLimiter(3, window_seconds=60)
    for i in range(3):
        assert rl.allow("a", now=100.0 + i)[0] is True
    ok, retry = rl.allow("a", now=103.0)
    assert ok is False and retry >= 1


def test_window_slides() -> None:
    rl = RateLimiter(2, window_seconds=10)
    assert rl.allow("a", now=0.0)[0] is True
    assert rl.allow("a", now=1.0)[0] is True
    assert rl.allow("a", now=5.0)[0] is False
    # Both hits age out by t=11.
    assert rl.allow("a", now=11.5)[0] is True


def test_callers_isolated() -> None:
    rl = RateLimiter(1, window_seconds=60)
    assert rl.allow("x", now=0.0)[0] is True
    assert rl.allow("y", now=0.0)[0] is True
    assert rl.allow("x", now=0.5)[0] is False


def test_disabled_limiter_allows_all() -> None:
    rl = RateLimiter(0)
    for _ in range(50):
        assert rl.allow("a")[0] is True


def test_reset_clears_caller() -> None:
    rl = RateLimiter(1, window_seconds=60)
    assert rl.allow("a", now=0.0)[0] is True
    assert rl.allow("a", now=1.0)[0] is False
    rl.reset("a")
    assert rl.allow("a", now=2.0)[0] is True


def test_global_off_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    import coffer._engine.ratelimit as rl_mod

    monkeypatch.setattr(rl_mod, "_limiter", None)
    monkeypatch.delenv("X402_RATE_LIMIT_PER_MIN", raising=False)
    assert get_rate_limiter() is None
    assert check_rate("anyone") == (True, 0)


def test_global_env_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    import coffer._engine.ratelimit as rl_mod

    monkeypatch.setattr(rl_mod, "_limiter", None)
    monkeypatch.setenv("X402_RATE_LIMIT_PER_MIN", "2")
    limiter = get_rate_limiter()
    assert limiter is not None and limiter.per_minute == 2
    assert check_rate("z") == (True, 0)


def test_check_rate_fail_open_on_internal_error(monkeypatch: pytest.MonkeyPatch) -> None:
    import coffer._engine.ratelimit as rl_mod

    def boom(caller: str) -> tuple[bool, int]:
        raise RuntimeError("internal")

    monkeypatch.setattr(rl_mod, "get_rate_limiter", lambda: boom("x"))
    assert check_rate("a") == (True, 0)  # never break payments over rate limiting
