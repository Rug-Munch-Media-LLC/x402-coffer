"""Per-caller request rate limiting for x402 Coffer.

Distinct from spending controls (which caps VALUE): this caps REQUEST
VELOCITY so a runaway agent can't hammer paid endpoints. Sliding-window
counter per caller key, bounded memory, fail-open on internal errors
(rate limiting must never take down payment processing).

Env: ``X402_RATE_LIMIT_PER_MIN`` (requests/minute/caller; unset = off).
"""

from __future__ import annotations

import os
import threading
import time
from collections import OrderedDict, deque


class RateLimiter:
    """Sliding-window per-caller limiter."""

    def __init__(
        self, per_minute: int, *, window_seconds: float = 60.0, max_callers: int = 50_000
    ) -> None:
        self.per_minute = int(per_minute)
        self.window = float(window_seconds)
        self._max = int(max_callers)
        self._lock = threading.Lock()
        self._hits: OrderedDict[str, deque[float]] = OrderedDict()

    def allow(self, caller: str, *, now: float | None = None) -> tuple[bool, int]:
        """``(allowed, retry_after_seconds)`` — atomic check+record."""
        if self.per_minute <= 0:
            return True, 0
        ts = time.monotonic() if now is None else now
        window_start = ts - self.window
        with self._lock:
            dq = self._hits.get(caller)
            if dq is None:
                dq = deque()
                self._hits[caller] = dq
                while len(self._hits) > self._max:
                    self._hits.popitem(last=False)
            while dq and dq[0] <= window_start:
                dq.popleft()
            if len(dq) >= self.per_minute:
                retry_after = max(1, int(dq[0] + self.window - ts) + 1)
                return False, retry_after
            dq.append(ts)
        return True, 0

    def reset(self, caller: str) -> None:
        with self._lock:
            self._hits.pop(caller, None)


_limiter: RateLimiter | None = None


def get_rate_limiter() -> RateLimiter | None:
    """Global limiter from ``X402_RATE_LIMIT_PER_MIN``; None when unset/off."""
    global _limiter
    if _limiter is None:
        raw = os.getenv("X402_RATE_LIMIT_PER_MIN", "").strip()
        try:
            per_min = int(raw) if raw else 0
        except ValueError:
            per_min = 0
        if per_min <= 0:
            return None
        _limiter = RateLimiter(per_min)
    return _limiter


def check_rate(caller: str) -> tuple[bool, int]:
    """One-call enforcement against the global limiter. Fail-open."""
    try:
        limiter = get_rate_limiter()
        if limiter is None:
            return True, 0
        return limiter.allow(caller)
    except Exception:  # noqa: BLE001 — never break payments over rate limiting
        return True, 0
