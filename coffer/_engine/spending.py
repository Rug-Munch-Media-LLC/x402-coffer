"""Spending controls — per-caller spend limits for x402 Coffer.

Agents pay with wallets; operators need per-wallet ceilings so a runaway
agent can't drain itself (or abuse free-retry windows). This module enforces:

* ``max_per_payment``   — hard ceiling on any single payment (atomic units)
* ``max_window_spend``  — rolling-window total ceiling per caller
* ``max_payments_per_window`` — velocity cap per caller

Fail-open by default (no limits configured => allow); every configured limit
that is exceeded fails CLOSED with a machine-readable reason.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict, deque
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class SpendingPolicy:
    """All limits optional; unset = unrestricted for that dimension."""

    max_per_payment: int | None = None
    max_window_spend: int | None = None
    window_seconds: int = 3600
    max_payments_per_window: int | None = None

    def is_unrestricted(self) -> bool:
        return (
            self.max_per_payment is None
            and self.max_window_spend is None
            and self.max_payments_per_window is None
        )


class CallerSpendTracker:
    """Bounded per-caller rolling-window payment history."""

    def __init__(self, max_callers: int = 50_000) -> None:
        self._max = int(max_callers)
        self._lock = threading.Lock()
        self._history: OrderedDict[str, deque[tuple[float, int]]] = OrderedDict()

    def _prune(self, caller: str, window_start: float) -> int:
        dq = self._history.get(caller)
        if dq is None:
            return 0
        while dq and dq[0][0] <= window_start:
            dq.popleft()
        return sum(amount for _, amount in dq)

    def check_and_record(
        self,
        caller: str,
        amount_atomic: int,
        policy: SpendingPolicy,
        *,
        now: float | None = None,
    ) -> tuple[bool, str]:
        """Atomically check all limits; record when allowed. ``(ok, reason)``."""
        if policy.is_unrestricted():
            return True, ""
        ts = time.time() if now is None else now
        window_start = ts - policy.window_seconds
        with self._lock:
            if policy.max_per_payment is not None and amount_atomic > policy.max_per_payment:
                return False, f"over_per_payment_limit:{amount_atomic}>{policy.max_per_payment}"
            window_spend = self._prune(caller, window_start)
            count = len(self._history.get(caller, ()))
            if (
                policy.max_payments_per_window is not None
                and count >= policy.max_payments_per_window
            ):
                return False, f"over_velocity_limit:{count}>={policy.max_payments_per_window}"
            if (
                policy.max_window_spend is not None
                and window_spend + amount_atomic > policy.max_window_spend
            ):
                return (
                    False,
                    f"over_window_spend:{window_spend + amount_atomic}>{policy.max_window_spend}",
                )
            dq = self._history.get(caller)
            if dq is None:
                dq = deque()
                self._history[caller] = dq
                while len(self._history) > self._max:
                    self._history.popitem(last=False)
            dq.append((ts, amount_atomic))
        return True, ""

    def usage(self, caller: str, window_seconds: int) -> dict[str, Any]:
        """Current-window usage snapshot for a caller."""
        window_start = time.time() - window_seconds
        with self._lock:
            spent = self._prune(caller, window_start)
            count = len(self._history.get(caller, ()))
        return {"window_spend_atomic": spent, "window_payments": count}

    def reset(self, caller: str) -> None:
        with self._lock:
            self._history.pop(caller, None)


_default_tracker: CallerSpendTracker | None = None
_default_policy: SpendingPolicy | None = None


def get_spending_policy() -> SpendingPolicy:
    """Global policy from env (X402_MAX_PER_PAYMENT_ATOMIC, X402_MAX_WINDOW_SPEND_ATOMIC,
    X402_SPEND_WINDOW_SECONDS, X402_MAX_PAYMENTS_PER_WINDOW). Unset = unrestricted."""
    global _default_policy
    if _default_policy is None:
        import os

        def _int_env(name: str) -> int | None:
            raw = os.getenv(name, "").strip()
            try:
                return int(raw) if raw else None
            except ValueError:
                return None

        _default_policy = SpendingPolicy(
            max_per_payment=_int_env("X402_MAX_PER_PAYMENT_ATOMIC"),
            max_window_spend=_int_env("X402_MAX_WINDOW_SPEND_ATOMIC"),
            window_seconds=_int_env("X402_SPEND_WINDOW_SECONDS") or 3600,
            max_payments_per_window=_int_env("X402_MAX_PAYMENTS_PER_WINDOW"),
        )
    return _default_policy


def get_spending_tracker() -> CallerSpendTracker:
    global _default_tracker
    if _default_tracker is None:
        _default_tracker = CallerSpendTracker()
    return _default_tracker


def enforce_spending(caller: str, amount_atomic: int) -> tuple[bool, str]:
    """One-call enforcement against the global policy/tracker. ``(ok, reason)``."""
    return get_spending_tracker().check_and_record(caller, amount_atomic, get_spending_policy())
