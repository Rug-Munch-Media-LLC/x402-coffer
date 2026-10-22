"""Coffer — the ONE payment gate used by the middleware and the decorator.

Decision order (every branch fails closed):

1. v2/legacy payment header present -> :func:`gate_settle.pay_with_settler`
   (attestation -> budget reserve -> settler -> ledger).
2. otherwise ``X-Payment-ID`` (legacy replay rail) -> store lookup.
3. otherwise 402 with a machine-readable ``reason``.

Settle/budget invariant lives in :mod:`coffer._engine.gate_settle`: no
reservation, no settle; release on every failure; ``Retry-After`` on budget
exhaustion.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from .gate_settle import pay_with_settler
from .gate_types import GateDecision, GatePolicy, Settler
from .payment_headers import PaymentHeaderError, has_payment_header
from .payment_headers import decode_payment_header as _decode

__all__ = ["GateDecision", "GatePolicy", "Settler", "authorize_payment", "make_handler_settler"]

log = logging.getLogger(__name__)

LEGACY_PAYMENT_ID_HEADER = "x-payment-id"


def _amount_atomic(payload: Mapping[str, Any]) -> int:
    """Signed amount from the authorization, else the top-level ``amount``."""
    authorization = payload.get("authorization")
    if isinstance(authorization, Mapping):
        try:
            return int(authorization.get("value", 0) or 0)
        except (TypeError, ValueError):
            return 0
    try:
        return int(payload.get("amount", 0) or 0)
    except (TypeError, ValueError):
        return 0


async def authorize_payment(
    headers: Mapping[str, str],
    *,
    policy: GatePolicy,
    path: str = "",
    caller_key: str = "",
) -> GateDecision:
    """Decide whether *headers* authorise the request. Fail-closed, never raises."""
    if has_payment_header(headers):
        try:
            payload = _decode(headers)
        except PaymentHeaderError as exc:
            return GateDecision(paid=False, reason=exc.reason)
        return await pay_with_settler(
            payload,
            policy=policy,
            amount=_amount_atomic(payload),
            key_id=str(payload.get("key_id") or caller_key or ""),
        )
    if policy.store is not None:
        payment_id = str(headers.get(LEGACY_PAYMENT_ID_HEADER, "")).strip()
        if payment_id and policy.store.is_paid(payment_id, caller_key=caller_key):
            return GateDecision(paid=True, reason="legacy_payment_id")
    return GateDecision(paid=False, reason="payment_required")


def make_handler_settler(handler: Any) -> Settler:
    """Re-export so callers import the bridge from the gate module."""
    from .gate_settle import make_handler_settler as _make

    return _make(handler)
