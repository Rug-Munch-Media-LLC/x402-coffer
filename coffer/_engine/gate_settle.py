"""Coffer — settle orchestration behind the gate (reserve -> settle/release).

Invariants (see ``docs/PRY-PARITY.md``):

* No settle without a reservation: ``check_and_reserve`` runs first and the
  hold is released on every non-success path.
* Exactly one usage-ledger row per attempt: ``settled`` (money moved),
  ``released`` (hold freed, no money), ``failed`` (error before/around the
  facilitator call).
* Fail closed: an exception, a refusal, or a missing settler returns a refusal
  decision — never ``paid=True``.
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any

from .attestation import verify_payment_required
from .budgets import BudgetExceeded
from .gate_types import GateDecision, GatePolicy, Settler
from .payment_headers import settlement_header_pairs
from .usage_ledger import record_usage

__all__ = ["attestation_refusal", "make_handler_settler", "pay_with_settler"]

log = logging.getLogger(__name__)


def attestation_refusal(payload: dict[str, Any], policy: GatePolicy) -> str:
    """Return a refusal reason when the presented challenge attestation fails."""
    challenge = payload.get("challenge")
    if not isinstance(challenge, dict):
        return "attestation_required" if policy.require_attestation else ""
    result = verify_payment_required(challenge)
    if result.ok:
        return ""
    if not result.attested and not policy.require_attestation:
        return ""
    return "invalid_attestation" if result.attested else "attestation_required"


def _row(policy: GatePolicy, *, key_id: str, outcome: str, **fields: Any) -> None:
    """Write one ledger row; a ledger failure never changes the decision."""
    if policy.ledger_path is None:
        return
    try:
        record_usage(policy.ledger_path, key_id=key_id or "anonymous", outcome=outcome, **fields)
    except OSError as exc:
        log.warning("x402_usage_ledger_write_failed", extra={"error": str(exc)})


def _release(policy: GatePolicy, reservation: Any) -> None:
    if reservation is not None and policy.budgets is not None:
        policy.budgets.release(reservation.reservation_id)


async def pay_with_settler(
    payload: dict[str, Any],
    *,
    policy: GatePolicy,
    amount: int,
    key_id: str,
) -> GateDecision:
    """Attestation -> reserve -> settler -> ledger. Never raises."""
    refusal = attestation_refusal(payload, policy)
    if refusal:
        _row(
            policy,
            key_id=key_id,
            outcome="failed",
            route=policy.operation,
            extra={"reason": refusal},
        )
        return GateDecision(paid=False, reason=refusal)
    reservation = None
    if policy.budgets is not None:
        try:
            reservation = policy.budgets.check_and_reserve(key_id, amount, route=policy.operation)
        except BudgetExceeded as exc:
            _row(
                policy,
                key_id=key_id,
                outcome="failed",
                route=policy.operation,
                amount_atomic=amount,
                extra={"reason": "budget_exhausted"},
            )
            return GateDecision(paid=False, reason="budget_exhausted", retry_after=exc.retry_after)
    if policy.settler is None:
        _release(policy, reservation)
        _row(
            policy,
            key_id=key_id,
            outcome="released",
            route=policy.operation,
            amount_atomic=amount,
            extra={"reason": "settler_unavailable"},
        )
        return GateDecision(paid=False, reason="settler_unavailable")
    try:
        receipt = await policy.settler(payload)
    except Exception as exc:  # noqa: BLE001 — fail closed, never serve unpaid
        log.warning("x402_settle_exception", extra={"error": str(exc)})
        _release(policy, reservation)
        _row(
            policy,
            key_id=key_id,
            outcome="failed",
            route=policy.operation,
            amount_atomic=amount,
            extra={"reason": "settler_exception"},
        )
        return GateDecision(paid=False, reason="settlement_failed")
    if not (receipt.get("success") or receipt.get("verified")):
        _release(policy, reservation)
        reason = str(receipt.get("reason", "settlement_refused"))
        _row(
            policy,
            key_id=key_id,
            outcome="released",
            route=policy.operation,
            amount_atomic=amount,
            extra={"reason": reason},
        )
        return GateDecision(paid=False, reason=reason)
    if reservation is not None and policy.budgets is not None:
        policy.budgets.settle(reservation.reservation_id)
    _row(
        policy,
        key_id=key_id,
        outcome="settled",
        route=policy.operation,
        amount_atomic=amount,
        payment_id=str(payload.get("payment_id", "")),
        network=str(payload.get("network", "")),
        asset=str(payload.get("asset", "")),
        tx_hash=str(receipt.get("transaction") or receipt.get("txHash") or ""),
        reservation_id=reservation.reservation_id if reservation else "",
    )
    return GateDecision(paid=True, receipt=receipt, headers=settlement_header_pairs(receipt))


def make_handler_settler(handler: Any) -> Settler:
    """Bridge an ``X402Handler`` into a settler (verify -> settle)."""

    async def _settle(payload: dict[str, Any]) -> dict[str, Any]:
        payment_id = str(payload.get("payment_id", ""))
        authorization = payload.get("authorization") or {}
        tx_hash = str(payload.get("tx_hash") or "")
        if not tx_hash:
            seed = str(authorization.get("nonce", "")) + str(authorization.get("signature", ""))
            tx_hash = "0x" + hashlib.sha256(seed.encode()).hexdigest()
        network = str(payload.get("network", ""))
        asset = str(payload.get("asset", ""))
        operation = str(payload.get("operation", ""))
        key_id = str(payload.get("key_id", ""))
        handler.router.submit_authorization(payment_id, authorization)
        verified = await handler.verify_payment(
            payment_id,
            tx_hash,
            network=network,
            asset=asset,
            operation=operation,
            caller_key=key_id,
        )
        if not verified.get("verified"):
            return {"success": False, "reason": str(verified.get("reason", "not_verified"))}
        handler.router.submit_authorization(payment_id, authorization)
        return await handler.settle_payment(
            payment_id, tx_hash, network=network, asset=asset, operation=operation, user_id=key_id
        )

    return _settle
