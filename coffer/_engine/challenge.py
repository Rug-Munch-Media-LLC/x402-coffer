"""Coffer — the ONE 402-challenge builder (body + attested headers + reason).

Every refusal path in the repo (ASGI middleware, ``require_payment`` decorator,
``X402Handler.require_payment``) builds its challenge here, so a client never
sees two different 402 shapes and the attestation can never be attached on one
path and forgotten on another.

Attestation: :func:`coffer._engine.attestation.attest_payment_required` is
applied to the body *before* the ``PAYMENT-REQUIRED`` header is encoded, so the
signed bytes and the wire bytes are identical. With no signing key configured
the body is emitted unattested and ``verify_payment_required`` reports
``attested=False`` — callers decide whether to enforce.
"""

from __future__ import annotations

from typing import Any

from .attestation import attest_payment_required
from .body import payment_required_body, payment_required_headers

__all__ = ["REFUSAL_KEY", "build_attested_challenge"]

REFUSAL_KEY = "reason"


def build_attested_challenge(
    *,
    resource: str = "",
    operation: str = "api_access",
    reason: str = "",
) -> tuple[dict[str, Any], dict[str, str]]:
    """Return ``(body, headers)`` for a 402, attested, with an optional reason.

    ``reason`` is machine-readable (``invalid_attestation``,
    ``insufficient_amount``, ``budget_exhausted``, ``settler_unavailable`` …)
    and travels in the JSON body so an agent can act without parsing prose.
    """
    body = payment_required_body(resource=resource, operation=operation)
    if reason:
        body[REFUSAL_KEY] = reason
    attest_payment_required(body)
    return body, payment_required_headers(body)
