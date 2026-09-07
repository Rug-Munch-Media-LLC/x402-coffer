"""Caller-bound, TTL'd verified-payment store for the x402 domain.

Owns :class:`PaymentStore` plus the process-wide default-store accessors so
the middleware adapters (:mod:`app.domain.x402.adapters`) can stay thin.
Replay semantics mirror the canonical ``X402Handler`` contract: a verified
``X-Payment-ID`` is honoured only for the caller that recorded it within the
validity window.
"""

from __future__ import annotations

import math
import os
import re
from datetime import UTC, datetime, timedelta
from typing import Any

__all__ = [
    "PAYMENT_VALIDITY_SECONDS",
    "PaymentStore",
    "get_default_store",
    "set_default_store",
    "validate_payment_id",
]

#: TTL for verified payment ids (mirrors x402-py ``PAYMENT_VALIDITY_SECONDS``;
#: env ``X402_PAYMENT_TTL`` overrides).
PAYMENT_VALIDITY_SECONDS: int = int(os.getenv("X402_PAYMENT_TTL", "3600"))

#: Canonical payment-id shape. The x402 wire contract quotes 16-128 chars of
#: ``[A-Za-z0-9_-]``; Coffer REJECTS malformed/oversized ids outright (fail
#: closed) while remaining permissive on the lower bound so short legacy ids
#: keep working. Anything outside this charset (spaces, control chars, `/`,
#: unicode, …) or longer than 128 chars is refused before it can reach a
#: store record, a Redis key or a ledger row.
PAYMENT_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")
PAYMENT_ID_MAX_LEN = 128


def validate_payment_id(payment_id: Any) -> str:
    """Return an error message when ``payment_id`` is malformed/oversized.

    ``""`` means the id is acceptable. Used by :class:`PaymentStore.record`
    (which raises) and by the HTTP router (which maps the message onto an
    RFC 7807 ``400`` problem before any verification is attempted).
    """
    if not isinstance(payment_id, str) or not payment_id:
        return "payment_id must be a non-empty string"
    if len(payment_id) > PAYMENT_ID_MAX_LEN:
        return f"payment_id too long (max {PAYMENT_ID_MAX_LEN} chars)"
    if not PAYMENT_ID_RE.fullmatch(payment_id):
        return "payment_id may only contain letters, digits, '-' or '_'"
    return ""


class PaymentStore:
    """In-memory verified-payment store, caller-bound + TTL'd.

    Mirrors the canonical ``X402Handler`` replay contract:

    - ``is_paid(payment_id, caller_key)`` is True only when the id was verified,
      has not exceeded ``PAYMENT_VALIDITY_SECONDS``, carries a positive
      server-confirmed ``amount_usd`` (zero/absent amounts fail closed), and —
      when it was recorded with a caller key — the replaying caller matches it.
    - A record without an ``expires_at`` (pre-hardening) fails closed.
    """

    def __init__(self, *, validity_seconds: int = PAYMENT_VALIDITY_SECONDS) -> None:
        self.validity_seconds = validity_seconds
        self._records: dict[str, dict[str, Any]] = {}
        #: Settle-outcome cache keyed by payment_id (``{success: True}`` only —
        #: failed attempts are never cached so they can be retried). Drives the
        #: idempotent ``POST /v1/x402/settle`` contract: a repeat settle returns
        #: the stored first outcome instead of re-rolling a facilitator.
        self._settlements: dict[str, dict[str, Any]] = {}
        # W2 persistence: optional Redis mirror so verified payment state
        # survives restarts / multi-instance deployments. Memory remains the
        # primary read path; any Redis error degrades to memory-only.
        self._redis: Any = None
        redis_url = os.getenv("X402_REDIS_URL", os.getenv("REDIS_URL", ""))
        if redis_url:
            try:
                import redis as _sync_redis  # type: ignore[import-untyped]

                self._redis = _sync_redis.Redis.from_url(redis_url, decode_responses=True, socket_timeout=1.0)
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
        """Record a verified payment id bound to ``caller_key`` for the TTL.

        ``amount_usd`` is the SERVER-CONFIRMED paid amount (USD) — the wire
        path only ever records amounts that passed server-side verification
        and the operation's required minimum. ``is_paid`` fails closed on any
        record that does not carry a positive finite amount, so a client-
        asserted zero (the historical default when ``amount_usd`` was omitted)
        can never unlock the gate.
        """
        err = validate_payment_id(payment_id)
        if err:
            raise ValueError(err)
        self._records[payment_id] = {
            "payment_id": payment_id,
            "tx_hash": tx_hash,
            "network": network,
            "asset": asset,
            "amount_usd": amount_usd,
            "facilitator": facilitator,
            "verified": verified,
            "caller_key": caller_key or "",
            "expires_at": (datetime.now(UTC) + timedelta(seconds=self.validity_seconds)).isoformat(),
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
            except Exception:  # noqa: BLE001, S110 — never break recording on redis
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
            except Exception:  # noqa: BLE001, S110 — mirror is best-effort
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
        # Server-authoritative amount gate (C1/H1): a record is only honoured
        # when it carries a positive, finite server-confirmed amount. Zero /
        # missing amounts (e.g. a client-asserted default) never pass — the
        # paid amount is bound at verify time against the operation minimum.
        try:
            recorded_amount = float(rec.get("amount_usd"))
        except (TypeError, ValueError):
            return False
        if not math.isfinite(recorded_amount) or recorded_amount <= 0:
            return False
        stored_caller = rec.get("caller_key") or ""
        if stored_caller:
            return stored_caller == caller_key
        return True

    def get(self, payment_id: str) -> dict[str, Any] | None:
        """Return the raw record for a payment id, if present."""
        return self._records.get(payment_id)

    # ── settle-outcome cache (idempotent /v1/x402/settle) ───────────────────

    def record_settle_outcome(
        self,
        *,
        payment_id: str,
        caller_key: str = "",
        outcome: dict[str, Any] | None = None,
    ) -> None:
        """Cache a SUCCESSFUL settlement so a repeat settle is a no-op replay.

        Only ``success: True`` outcomes are stored — a failed attempt must be
        retryable. Caller-bound like the payment record itself, so a repeat
        settle from a different caller is refused by the reconciliation layer
        (not handed the original caller's receipt).
        """
        err = validate_payment_id(payment_id)
        if err:
            raise ValueError(err)
        result = dict(outcome or {})
        if result.get("success") is not True:
            return  # never cache failures — retries must re-run the facilitator
        entry: dict[str, Any] = {
            "payment_id": payment_id,
            "caller_key": caller_key or "",
            "success": True,
            "settled_at": datetime.now(UTC).isoformat(),
            "result": result,
        }
        self._settlements[payment_id] = entry
        if self._redis is not None:
            try:
                import json as _json

                self._redis.setex(
                    f"coffer:settle:{payment_id}",
                    self.validity_seconds,
                    _json.dumps(entry),
                )
            except Exception:  # noqa: BLE001, S110 — never break recording on redis
                pass

    def get_settle_outcome(self, payment_id: str) -> dict[str, Any] | None:
        """Return the cached successful settlement for a payment id, if any.

        Memory first; on a miss, consult the Redis mirror (restart / multi-
        instance case) and re-hydrate memory so repeat checks stay local.
        """
        if not payment_id:
            return None
        if payment_id not in self._settlements and self._redis is not None:
            try:
                import json as _json

                raw = self._redis.get(f"coffer:settle:{payment_id}")
                if raw:
                    entry = _json.loads(raw)
                    if entry.get("success") is True:
                        self._settlements[payment_id] = entry
            except Exception:  # noqa: BLE001, S110 — mirror is best-effort
                pass
        entry = self._settlements.get(payment_id)
        if entry is None or entry.get("success") is not True:
            return None
        return dict(entry)

    # ── liveness ────────────────────────────────────────────────────────────

    def redis_health(self) -> str:
        """``"ok"`` when the Redis mirror answers a ping, else ``"unavailable"``.

        Memory-only stores (no ``X402_REDIS_URL``) report ``"unavailable"`` —
        honest liveness: settlement state then does not survive a restart.
        """
        if self._redis is None:
            return "unavailable"
        try:
            self._redis.ping()
            return "ok"
        except Exception:  # noqa: BLE001 — a dead mirror is degraded, not fatal
            return "unavailable"

    def health(self) -> dict[str, Any]:
        """Liveness snapshot: in-memory store + Redis mirror status."""
        return {
            "store": "ok",
            "redis": self.redis_health(),
            "payments": len(self),
        }

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
