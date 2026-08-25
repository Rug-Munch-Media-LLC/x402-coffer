"""Tests for spending controls."""

from __future__ import annotations

import pytest

from coffer._engine.spending import (
    CallerSpendTracker,
    SpendingPolicy,
    enforce_spending,
    get_spending_policy,
)


def test_unrestricted_policy_allows_everything() -> None:
    t = CallerSpendTracker()
    policy = SpendingPolicy()
    for i in range(100):
        ok, reason = t.check_and_record("a", 10**9, policy)
        assert ok, reason


def test_per_payment_ceiling() -> None:
    t = CallerSpendTracker()
    policy = SpendingPolicy(max_per_payment=1000)
    assert t.check_and_record("a", 999, policy) == (True, "")
    ok, reason = t.check_and_record("a", 1001, policy)
    assert ok is False and "over_per_payment_limit" in reason


def test_window_spend_ceiling() -> None:
    t = CallerSpendTracker()
    policy = SpendingPolicy(max_window_spend=2000, window_seconds=60)
    assert t.check_and_record("a", 900, policy)[0] is True
    assert t.check_and_record("a", 900, policy)[0] is True
    ok, reason = t.check_and_record("a", 300, policy)
    assert ok is False and "over_window_spend" in reason


def test_velocity_cap() -> None:
    t = CallerSpendTracker()
    policy = SpendingPolicy(max_payments_per_window=2, max_per_payment=10)
    assert t.check_and_record("v", 1, policy)[0] is True
    assert t.check_and_record("v", 1, policy)[0] is True
    ok, reason = t.check_and_record("v", 1, policy)
    assert ok is False and "over_velocity_limit" in reason


def test_callers_are_isolated() -> None:
    t = CallerSpendTracker()
    policy = SpendingPolicy(max_window_spend=1000, window_seconds=60)
    assert t.check_and_record("x", 800, policy)[0] is True
    ok, _ = t.check_and_record("y", 800, policy)
    assert ok is True  # different caller, separate window


def test_old_payments_prune_out_of_window() -> None:
    t = CallerSpendTracker()
    policy = SpendingPolicy(max_window_spend=1000, window_seconds=60)
    old = __import__("time").time() - 3600
    with t._lock:  # seed an out-of-window payment directly
        t._history["z"] = __import__("collections").deque([(old, 5000)])
    ok, _ = t.check_and_record("z", 400, policy)
    assert ok is True  # aged-out spend no longer counts


def test_usage_snapshot_and_reset() -> None:
    t = CallerSpendTracker()
    policy = SpendingPolicy(max_per_payment=10_000)
    t.check_and_record("u", 300, policy)
    usage = t.usage("u", window_seconds=60)
    assert usage["window_spend_atomic"] == 300 and usage["window_payments"] == 1
    t.reset("u")
    assert t.usage("u", 60)["window_payments"] == 0


def test_global_enforce_defaults_unrestricted(monkeypatch: pytest.MonkeyPatch) -> None:
    import coffer._engine.spending as sp

    monkeypatch.setattr(sp, "_default_policy", None)
    monkeypatch.delenv("X402_MAX_PER_PAYMENT_ATOMIC", raising=False)
    monkeypatch.delenv("X402_MAX_WINDOW_SPEND_ATOMIC", raising=False)
    monkeypatch.delenv("X402_MAX_PAYMENTS_PER_WINDOW", raising=False)
    assert get_spending_policy().is_unrestricted() is True
    ok, reason = enforce_spending("anyone", 10**12)
    assert ok is True and reason == ""


def test_env_configured_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    import coffer._engine.spending as sp

    monkeypatch.setattr(sp, "_default_policy", None)
    monkeypatch.setenv("X402_MAX_PER_PAYMENT_ATOMIC", "500")
    policy = get_spending_policy()
    assert policy.max_per_payment == 500
