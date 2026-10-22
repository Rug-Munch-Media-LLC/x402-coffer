"""Coffer — per-key budgets and quotas with atomic reserve/settle/release.

Parity reference (Pry): ``pryscraper/src/pry/payments/x402/handler_authorize.py``
plus ``src/pry/payments/x402/scheme/pricing/manifest.py`` — a priced accept set
is only served when the caller's key still has authorization headroom; the
ledger writes the resulting spend.

Coffer gap this closes: ``coffer/_engine/spending.py`` caps *per-caller* rolling
spend (IP/wallet keyed) and ``ratelimit.py`` caps *request velocity* per token,
but nothing enforces a **money budget per API key** with a reserve-then-commit
step. Without a reservation a concurrent burst can each pass the same
remaining-balance check and overspend (TOCTOU).

Semantics (all fail-closed once configured):
    * ``check_and_reserve`` atomically validates the per-payment cap and the
      period ceiling, then holds the amount. Over-budget raises
      :class:`BudgetExceeded` carrying a ``retry_after`` hint.
    * ``settle`` commits a reservation (writes the spend); ``release`` frees it
      (settle failed, client aborted, RPC timeout). Neither can double-count.
    * Unconfigured key => unrestricted (mirrors coffer's existing opt-in limits).
"""

from __future__ import annotations

import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field

DEFAULT_PERIOD_SECONDS = 30 * 24 * 3600


@dataclass(frozen=True)
class BudgetPolicy:
    """Ceilings in atomic units. ``None`` means "no ceiling" on that axis."""

    limit_atomic: int | None = None
    period_seconds: int = DEFAULT_PERIOD_SECONDS
    max_per_payment: int | None = None


@dataclass
class Reservation:
    """A held authorization amount for one request."""

    reservation_id: str
    key_id: str
    amount_atomic: int
    route: str
    created_at: float
    state: str = "reserved"


@dataclass
class _Window:
    started_at: float
    spent_atomic: int = 0
    reserved_atomic: int = 0


class BudgetExceeded(PermissionError):
    """The key cannot afford this payment. Callers answer 402 with the hint."""

    status_code = 402

    def __init__(self, reason: str, retry_after: int = 0, remaining_atomic: int = 0) -> None:
        self.reason = reason
        self.retry_after = int(retry_after)
        self.remaining_atomic = int(remaining_atomic)
        super().__init__(reason)


@dataclass
class KeyBudgetLedger:
    """In-process budget ledger. Durable spend lives in ``usage_ledger`` rows."""

    policies: dict[str, BudgetPolicy] = field(default_factory=dict)
    clock: Callable[[], float] = time.time
    _windows: dict[str, _Window] = field(default_factory=dict, repr=False)
    _reservations: dict[str, Reservation] = field(default_factory=dict, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def _window(self, key_id: str, period_seconds: int, now: float) -> _Window:
        window = self._windows.get(key_id)
        if window is None or now - window.started_at >= period_seconds:
            window = _Window(started_at=now)
            self._windows[key_id] = window
        return window

    def check_and_reserve(self, key_id: str, amount_atomic: int, route: str = "") -> Reservation:
        """Atomically validate limits and hold *amount_atomic* for *key_id*."""
        if amount_atomic < 0:
            raise BudgetExceeded("negative amount")
        policy = self.policies.get(key_id)
        if policy is None:
            # Unconfigured key: no ceiling, but still a registered reservation
            # so settle/release behave symmetrically for callers.
            reservation = Reservation(uuid.uuid4().hex, key_id, amount_atomic, route, self.clock())
            with self._lock:
                self._reservations[reservation.reservation_id] = reservation
            return reservation
        if policy.max_per_payment is not None and amount_atomic > policy.max_per_payment:
            raise BudgetExceeded(
                "amount exceeds per-payment cap",
                remaining_atomic=policy.max_per_payment,
            )
        now = self.clock()
        with self._lock:
            window = self._window(key_id, policy.period_seconds, now)
            if policy.limit_atomic is not None:
                committed = window.spent_atomic + window.reserved_atomic
                if committed + amount_atomic > policy.limit_atomic:
                    retry_after = max(0, int(window.started_at + policy.period_seconds - now))
                    raise BudgetExceeded(
                        "budget exhausted for period",
                        retry_after=retry_after,
                        remaining_atomic=max(0, policy.limit_atomic - committed),
                    )
            window.reserved_atomic += amount_atomic
            reservation = Reservation(uuid.uuid4().hex, key_id, amount_atomic, route, now)
            self._reservations[reservation.reservation_id] = reservation
            return reservation

    def settle(self, reservation_id: str) -> dict[str, object]:
        """Commit a reservation; idempotent (a re-settle returns the same row)."""
        with self._lock:
            reservation = self._reservations.get(reservation_id)
            if reservation is None:
                raise KeyError(f"unknown reservation {reservation_id}")
            if reservation.state == "settled":
                return self._row(reservation, "settled")
            if reservation.state == "released":
                raise BudgetExceeded("reservation was released")
            reservation.state = "settled"
            policy = self.policies.get(reservation.key_id)
            if policy is not None:
                window = self._window(reservation.key_id, policy.period_seconds, self.clock())
                window.reserved_atomic = max(0, window.reserved_atomic - reservation.amount_atomic)
                window.spent_atomic += reservation.amount_atomic
            return self._row(reservation, "settled")

    def release(self, reservation_id: str) -> bool:
        """Free a hold without spending (failed settle, aborted client)."""
        with self._lock:
            reservation = self._reservations.get(reservation_id)
            if reservation is None or reservation.state != "reserved":
                return False
            reservation.state = "released"
            policy = self.policies.get(reservation.key_id)
            if policy is not None:
                window = self._window(reservation.key_id, policy.period_seconds, self.clock())
                window.reserved_atomic = max(0, window.reserved_atomic - reservation.amount_atomic)
            return True

    def remaining(self, key_id: str) -> int | None:
        """Atomic units still spendable this period, or ``None`` if uncapped."""
        policy = self.policies.get(key_id)
        if policy is None or policy.limit_atomic is None:
            return None
        now = self.clock()
        with self._lock:
            window = self._window(key_id, policy.period_seconds, now)
            return max(0, policy.limit_atomic - window.spent_atomic - window.reserved_atomic)

    def usage(self, key_id: str) -> dict[str, int]:
        """Committed vs held amounts for *key_id* (observability surface)."""
        policy = self.policies.get(key_id)
        with self._lock:
            window = self._windows.get(key_id)
            spent = window.spent_atomic if window else 0
            reserved = window.reserved_atomic if window else 0
            limit = policy.limit_atomic if policy and policy.limit_atomic is not None else -1
        return {"spent_atomic": spent, "reserved_atomic": reserved, "limit_atomic": limit}

    @staticmethod
    def _row(reservation: Reservation, outcome: str) -> dict[str, object]:
        return {
            "reservation_id": reservation.reservation_id,
            "key_id": reservation.key_id,
            "route": reservation.route,
            "amount_atomic": reservation.amount_atomic,
            "outcome": outcome,
            "created_at": reservation.created_at,
        }
