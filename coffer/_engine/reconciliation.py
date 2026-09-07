"""Invoice reconciliation — settle eligibility against a verified payment.

Pry billing parity: :mod:`pry.commerce.billing.enforcer` reconciles a
customer's entitlement (key -> tier) before the request is trusted; Coffer's
equivalent is :func:`reconcile_invoice`, which reconciles a ``/v1/x402/settle``
request against the server-verified payment record (the invoice) BEFORE any
settlement is attempted. A settle may only move money when the party asking
(recipient/caller), the amount and the asset all match the verified invoice —
anything else is refused and the facilitator is never contacted.

``recipient + amount + asset`` is the canonical triple; network and tx_hash
matching are added as defense-in-depth (a settlement referencing a different
chain or a different on-chain proof than the one verified must never pass).
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from .pricing import normalize_network_id

__all__ = ["ReconcileResult", "reconcile_invoice"]


class ReconcileResult:
    """Outcome of an invoice-reconciliation check."""

    __slots__ = ("ok", "reason")

    def __init__(self, ok: bool, reason: str = "") -> None:
        self.ok = bool(ok)
        self.reason = reason

    def __repr__(self) -> str:  # pragma: no cover — debugging aid
        return f"ReconcileResult(ok={self.ok}, reason={self.reason!r})"

    def __bool__(self) -> bool:
        return self.ok


def _record_alive(verified: Mapping[str, Any]) -> bool:
    """A verified record is only reconcilable while verified + unexpired."""
    if not isinstance(verified, Mapping) or not verified:
        return False
    if verified.get("verified") is not True:
        return False
    expires_at = verified.get("expires_at")
    if not expires_at:
        return False  # no expiry -> fail closed
    try:
        if datetime.fromisoformat(str(expires_at)) <= datetime.now(UTC):
            return False
    except (ValueError, TypeError):
        return False
    return True


def _amount_matches(recorded: Any, claimed: Any) -> bool:
    """Exact-enough amount match: finite positives within fp tolerance.

    The stored amount is the server-confirmed USD figure captured at verify
    time; the settle request must claim the same figure. ``math.isclose``
    absorbs float noise while staying strict for real mismatches.
    """
    try:
        a = float(recorded)
        b = float(claimed)
    except (TypeError, ValueError):
        return False
    if not (math.isfinite(a) and math.isfinite(b)) or a <= 0 or b <= 0:
        return False
    return math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-12)


def reconcile_invoice(
    verified: Mapping[str, Any],
    *,
    caller_key: str,
    network: str,
    asset: str,
    amount_usd: float,
    tx_hash: str,
) -> ReconcileResult:
    """Reconcile a settle request against its verified invoice record.

    Args:
        verified: The ``PaymentStore`` record captured when the payment was
            verified (the invoice).
        caller_key: The caller asking to settle (the recipient of the receipt
            — Coffer binds every verified payment to the caller that verified
            it, mirroring the ``X-Payment-ID`` middleware binding).
        network / asset / amount_usd / tx_hash: The settle request's claims.

    Returns an ``ok`` result only when every dimension matches. Failures are
    fail-closed with a machine-readable reason; the router maps them onto an
    RFC 7807 ``409 Conflict`` problem and NEVER contacts a facilitator.
    """
    if not _record_alive(verified):
        return ReconcileResult(False, "no prior verified (unexpired) payment for this id")

    # Recipient match: the caller settling must be the caller that verified.
    recorded_caller = str(verified.get("caller_key") or "")
    if recorded_caller and caller_key and recorded_caller != caller_key:
        return ReconcileResult(False, "recipient_mismatch: settle caller != verify caller")

    # Asset match (case-insensitive symbol, e.g. USDC == usdc).
    recorded_asset = str(verified.get("asset") or "").upper()
    claimed_asset = str(asset or "").upper()
    if recorded_asset and claimed_asset and recorded_asset != claimed_asset:
        return ReconcileResult(False, "asset_mismatch: settle asset != verified asset")

    # Amount match (USD).
    if not _amount_matches(verified.get("amount_usd"), amount_usd):
        return ReconcileResult(False, "amount_mismatch: settle amount != verified amount")

    # Network match (CAIP-2 normalized).
    recorded_network = normalize_network_id(str(verified.get("network") or ""))
    claimed_network = normalize_network_id(str(network or ""))
    if recorded_network and claimed_network and recorded_network != claimed_network:
        return ReconcileResult(False, "network_mismatch: settle network != verified network")

    # On-chain proof match: settle must reference the verified transaction.
    recorded_tx = str(verified.get("tx_hash") or "")
    if recorded_tx and tx_hash and recorded_tx.strip().lower() != str(tx_hash).strip().lower():
        return ReconcileResult(False, "tx_mismatch: settle tx_hash != verified tx_hash")

    return ReconcileResult(True, "")
