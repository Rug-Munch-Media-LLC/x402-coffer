"""Payment Identifier extension — network-safe idempotency (x402 official spec).

Implements ``docs/x402-official-spec/extensions__payment-identifier.md``
"Idempotency Behavior" (new ID = process+cache, same ID same fingerprint =
cached response, same ID different fingerprint = 409 Conflict, TTL expiry =
process again, required+missing = 400 Bad Request).

Client functions mirror the Python API reference (``generate_payment_id``,
``is_valid_payment_id``, ``append_payment_identifier_to_extensions``); server
functions add a caller-bound, TTL-backed replay store wired into the
FacilitatorRouter's existing :class:`ReplayStore` (caller-bound => key scoped
by payer + request fingerprint so a retried request with the same id and same
fingerprint is deduped, while a reused id with a different fingerprint yields
409 Conflict per the spec).

This is a NEW capability layer on top of the existing tx_hash replay store:
tx_hash replay protects against re-broadcast of a settlement; payment-id covers
the higher-level retry idempotency at the 402/payment-payload layer.
"""

from __future__ import annotations

import re
import secrets
from dataclasses import dataclass, field
from typing import Any

from coffer._engine.service import ReplayStore

PAYMENT_IDENTIFIER = "payment-identifier"
PAYMENT_ID_MIN_LENGTH = 16
PAYMENT_ID_MAX_LENGTH = 128
PAYMENT_ID_PATTERN = re.compile(r"^[a-zA-Z0-9_-]+$")

# Idempotency cache TTL (spec §Configuration Options: short 5-15m, long 1-24h).
DEFAULT_IDEMPOTENCY_TTL = int(3600)  # 1h
# Spec §Request Binding: parts that make a paid operation unique.
_FINGERPRINT_FIELDS = ("scheme", "network", "asset", "amount", "pay_to")
_RESOURCE_FIELDS = ("resource", "method", "operation")


def generate_payment_id(prefix: str = "pay_") -> str:
    """Generate a cryptographically secure payment identifier.

    Returns ``prefix + 32-char-hex`` (spec API ref: ``pay_<32-hex>``).
    """
    return f"{prefix}{secrets.token_hex(16)}"


def is_valid_payment_id(payment_id: str | None) -> bool:
    """Validate a payment identifier (16-128 alnum, ``-``/``_`` only)."""
    if not payment_id:
        return False
    return (
        PAYMENT_ID_MIN_LENGTH <= len(payment_id) <= PAYMENT_ID_MAX_LENGTH
        and PAYMENT_ID_PATTERN.match(payment_id) is not None
    )


def declare_payment_identifier_extension(required: bool = False) -> dict[str, Any]:
    """Declare the payment-identifier extension for a route.

    ``required=True`` means clients MUST supply a payment id or the server
    returns 400 (spec §Request Binding / Idempotency Behavior).
    """
    return {PAYMENT_IDENTIFIER: {"required": bool(required)}}


def extract_payment_identifier(payment_payload: Any) -> str | None:
    """Extract payment id from a payment payload's ``extensions``. (server)"""
    try:
        exts: Any = payment_payload.get("extensions") if isinstance(payment_payload, dict) else None
        if not exts:
            return None
        entry = exts.get(PAYMENT_IDENTIFIER)
        if not isinstance(entry, dict):
            return None
        pid = entry.get("paymentId") or entry.get("payment_id")
        return str(pid) if pid else None
    except (AttributeError, TypeError):  # pragma: no cover - defensive
        return None


def append_payment_identifier_to_extensions(
    extensions: dict[str, Any], payment_id: str | None = None
) -> None:
    """Add the payment id under ``extensions['payment-identifier']``. (client)

    Only writes if the server already declared the extension key; if no
    ``payment_id`` is supplied one is generated automatically.
    """
    if PAYMENT_IDENTIFIER not in extensions:
        return
    pid = payment_id or generate_payment_id()
    entry = extensions[PAYMENT_IDENTIFIER]
    if isinstance(entry, dict):
        entry.setdefault("paymentId", pid)
        entry.setdefault("payment_id", pid)
    else:  # scalar/None declaration -> promote to dict carrying the id
        extensions[PAYMENT_IDENTIFIER] = {"paymentId": pid, "payment_id": pid}


def validate_payment_identifier(extension: Any) -> dict[str, Any]:
    """Validate the payment-identifier extension object structure + id format.

    Mirrors the spec's ``validate_payment_identifier``: returns
    ``{valid: bool, errors: [...]}``.
    """
    errors: list[str] = []
    if not isinstance(extension, dict):
        return {"valid": False, "errors": ["extension must be an object"]}
    if "required" in extension and not isinstance(extension["required"], bool):
        errors.append("'required' must be a boolean")
    pid = extension.get("paymentId") or extension.get("payment_id")
    if pid is not None and not is_valid_payment_id(str(pid)):
        errors.append(f"invalid payment id '{pid}' (expected 16-128 [a-zA-Z0-9_-])")
    return {"valid": not errors, "errors": errors}


# ── caller-bound, TTL-backed replay store ────────────────────────────────────


@dataclass
class PaymentIdReplay:
    """Idempotency decision for a (payment_id, fingerprint) pair."""

    state: str  # "accepted" | "cached" | "conflict" | "expired" | "missing"
    reason: str = ""
    payment_id: str = ""


@dataclass
class PaymentIdentifierStore:
    """Caller-bound, TTL-backed idempotency cache (spec §Request Binding).

    Reuses the domain's :class:`ReplayStore` for the TTL burner backend
    (Redis SETNX with 24h TTL; in-memory bounded fallback). The store is
    CALLER-BOUND: the replay key is scoped by a ``caller`` scope string
    (payer + resource fingerprint) so one caller retrying with the same id is
    deduped, but a second caller replaying that id does NOT collide.


    .. note::
       The authoritative "already settled" fingerprint bookkeeping lives in the
       FacilitatorRouter's tx_hash store. This extension adds the higher-level
       idempotency layer so a *pre-settlement* retry (same id, same
       fingerprint) is served from cache without a second payment.

       [REVIEW] Spec recommends Redis in production. ReplayStore falls back to
       in-process memory when Redis is absent, which is NOT safe across app
       instances (mirrors the existing nonce store's caveat).
    """

    ttl: int = DEFAULT_IDEMPOTENCY_TTL
    _store: ReplayStore = field(default_factory=ReplayStore)
    # payment_id -> fingerprint marker (who claimed it) for the 409 path.
    _claims: dict[str, str] = field(default_factory=dict)

    def _claim_key(self, caller_scope: str, payment_id: str) -> str:
        return f"x402:payid:{caller_scope}:{payment_id}"

    @staticmethod
    def fingerprint(payment_req: dict[str, Any], resource_ctx: dict[str, Any] | None = None) -> str:
        """Normalized request fingerprint (spec §Request Binding).

        Covers scheme/network/asset/amount/payTo + resource path/method +
        application operation id.
        """
        req = {k: payment_req.get(k) for k in _FINGERPRINT_FIELDS}
        res: dict[str, Any] = {}
        ctx = resource_ctx or {}
        for k in _RESOURCE_FIELDS:
            if k in ctx or k in payment_req:
                res.setdefault(k, ctx.get(k, payment_req.get(k)))
        blob = {**{k: v for k, v in req.items() if v is not None}, **res}
        # Stable, sorted, repr-normalised fingerprint (no secret/PII).
        return repr(sorted((k, str(v)) for k, v in blob.items() if k != "pay_to"))

    async def check_before_payment(
        self,
        payment_id: str | None,
        caller_scope: str,
        fingerprint: str,
        required: bool = False,
    ) -> PaymentIdReplay:
        """Idempotency gate to run BEFORE processing a payment.

        Returns one of: accepted / cached / conflict / expired / missing —
        semantics per spec §Idempotency Behavior table.
        """
        if not payment_id:
            return PaymentIdReplay(
                state="missing",
                reason="payment id missing",
            ) if required else PaymentIdReplay(
                state="accepted",
                reason="no payment id (idempotency skipped)",
            )
        if not is_valid_payment_id(payment_id):
            return PaymentIdReplay(state="missing", reason="invalid payment id")

        key = self._claim_key(caller_scope, payment_id)

        # 409 Conflict: same id already used for a different request.
        claimed = self._claims.get(payment_id)
        if claimed is not None and claimed != fingerprint:
            return PaymentIdReplay(
                "conflict", "payment id reused for a different request", payment_id
            )

        # Cached within TTL -> serve cached, skip payment.
        if await self._store.check(key):
            return PaymentIdReplay("cached", "payment id already processed within TTL", payment_id)

        # New (or expired beyond TTL) -> burn the id now, process normally.
        await self._store.check_and_burn(key)
        self._claims[payment_id] = fingerprint
        return PaymentIdReplay("accepted", "first use of payment id", payment_id)


__all__ = [
    "DEFAULT_IDEMPOTENCY_TTL",
    "PAYMENT_IDENTIFIER",
    "PAYMENT_ID_MAX_LENGTH",
    "PAYMENT_ID_MIN_LENGTH",
    "PAYMENT_ID_PATTERN",
    "PaymentIdReplay",
    "PaymentIdentifierStore",
    "append_payment_identifier_to_extensions",
    "declare_payment_identifier_extension",
    "extract_payment_identifier",
    "generate_payment_id",
    "is_valid_payment_id",
    "validate_payment_identifier",
]
