"""Caller-bound, TTL'd verified-payment store for the x402 domain.

Owns :class:`PaymentStore` plus the process-wide default-store accessors so
the middleware adapters (:mod:`app.domain.x402.adapters`) can stay thin.
Replay semantics mirror the canonical ``X402Handler`` contract: a verified
``X-Payment-ID`` is honoured only for the caller that recorded it within the
validity window.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from typing import Any

__all__ = [
    "PAYMENT_VALIDITY_SECONDS",
    "PaymentStore",
    "get_default_store",
    "set_default_store",
]

#: TTL for verified payment ids (mirrors x402-py ``PAYMENT_VALIDITY_SECONDS``;
#: env ``X402_PAYMENT_TTL`` overrides).
PAYMENT_VALIDITY_SECONDS: int = int(os.getenv("X402_PAYMENT_TTL", "3600"))


class PaymentStore:
    """In-memory verified-payment store, caller-bound + TTL'd.

    Mirrors the canonical ``X402Handler`` replay contract:

    - ``is_paid(payment_id, caller_key)`` is True only when the id was verified,
      has not exceeded ``PAYMENT_VALIDITY_SECONDS``, and — when it was recorded
      with a caller key — the replaying caller matches it.
    - A record without an ``expires_at`` (pre-hardening) fails closed.
    """

    def __init__(self, *, validity_seconds: int = PAYMENT_VALIDITY_SECONDS) -> None:
        self.validity_seconds = validity_seconds
        self._records: dict[str, dict[str, Any]] = {}
        # W2 persistence: optional Redis mirror so verified payment state
        # survives restarts / multi-instance deployments. Memory remains the
        # primary read path; any Redis error degrades to memory-only.
        self._redis: Any = None
        redis_url = os.getenv("X402_REDIS_URL", os.getenv("REDIS_URL", ""))
        if redis_url:
            try:
                import redis as _sync_redis  # type: ignore[import-untyped]

                self._redis = _sync_redis.Redis.from_url(
                    redis_url, decode_responses=True, socket_timeout=1.0
                )
                self._redis.ping()
            except Exception:  # noqa: BLE001 — degrade to memory-only
                self._redis = None

    def record(
        self,
        *,
        payment_id: str,
        caller_key: str = "",
        tx_hash: str = "",
        network: str = "",
        asset: str = "",
        amount_usd: float = 0.0,
        facilitator: str = "",
        verified: bool = True,
    ) -> None:
        """Record a verified payment id bound to ``caller_key`` for the TTL."""
        self._records[payment_id] = {
            "payment_id": payment_id,
            "tx_hash": tx_hash,
            "network": network,
            "asset": asset,
            "amount_usd": amount_usd,
            "facilitator": facilitator,
            "verified": verified,
            "caller_key": caller_key or "",
            "expires_at": (
                datetime.now(UTC) + timedelta(seconds=self.validity_seconds)
            ).isoformat(),
            "verified_at": datetime.now(UTC).isoformat(),
        }
        if self._redis is not None:
            try:
                import json as _json

                self._redis.setex(
                    f"coffer:payment:{payment_id}",
                    self.validity_seconds,
                    _json.dumps(self._records[payment_id]),
                )
            except Exception:  # noqa: BLE001 — never break recording on redis
                pass

    def is_paid(self, payment_id: str, caller_key: str = "") -> bool:
        """True when the id is verified, unexpired and caller-bound.

        Memory first; on a miss, consult the Redis mirror (restart/multi-instance
        case) and re-hydrate memory so repeat checks stay local.
        """
        if not payment_id:
            return False
        if payment_id not in self._records and self._redis is not None:
            try:
                import json as _json

                raw = self._redis.get(f"coffer:payment:{payment_id}")
                if raw:
                    rec = _json.loads(raw)
                    if datetime.fromisoformat(rec["expires_at"]) > datetime.now(UTC):
                        self._records[payment_id] = rec
            except Exception:  # noqa: BLE001 — mirror is best-effort
                pass
        rec = self._records.get(payment_id)
        if rec is None or not rec.get("verified"):
            return False
        expires_at = rec.get("expires_at")
        if not expires_at:
            return False  # no expiry → fail closed
        try:
            if datetime.fromisoformat(expires_at) <= datetime.now(UTC):
                return False
        except ValueError:
            return False
        stored_caller = rec.get("caller_key") or ""
        if stored_caller and (not caller_key or stored_caller != caller_key):
            return False
        return True

    def get(self, payment_id: str) -> dict[str, Any] | None:
        """Return the raw record for a payment id, if present."""
        return self._records.get(payment_id)

    def __len__(self) -> int:
        return len(self._records)


_default_store: PaymentStore | None = None


def get_default_store() -> PaymentStore:
    """Return (creating once) the process-wide verified-payment store."""
    global _default_store
    if _default_store is None:
        _default_store = PaymentStore()
    return _default_store


def set_default_store(store: PaymentStore | None) -> None:
    """Override (or clear, with ``None``) the process-wide payment store."""
    global _default_store
    _default_store = store
