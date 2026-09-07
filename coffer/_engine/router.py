"""x402 HTTP router — spec-compliant 402-flow endpoints.

Exposes the official x402 HTTP transport surface:

- ``GET  /v1/x402/payment``  — 402 challenge: the spec header set
  (``PAYMENT-REQUIRED`` base64 + ``X-Payment-Required`` + ``X-Payment-Options``)
  and the ``{x402Version:1, accepts:[...]}`` body for a not-yet-paid caller.
- ``POST /v1/x402/verify``   — delegate a payment proof to the
  ``FacilitatorRouter`` (from :mod:`app.domain.x402.service`); on success the
  verified id is recorded caller-bound + TTL'd in the shared
  :class:`PaymentStore` so replay with ``X-Payment-ID`` passes the middleware.
  Replay rejection (an already-used tx_hash) surfaces as ``410 Gone``.
- ``POST /v1/x402/settle``   — delegate settlement to the FacilitatorRouter.
- ``GET  /v1/x402/health``   — honest acceptance/verification status.

Holds NO verification logic itself — it only wires the middleware's payment
store to the facilitator layer. The verify/settle endpoints use the
PROCESS-WIDE ``get_facilitator_router()`` singleton so failure counters,
registered per-network facilitators and the replay-nonce store are shared.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from .middleware import (
    HEADER_PAYMENT_REQUIRED,
    HEADER_X_PAYMENT_ID,
    HEADER_X_PAYMENT_OPTIONS,
    HEADER_X_PAYMENT_REQUIRED,
    PAYMENT_VALIDITY_SECONDS,
    PaymentStore,
    caller_key_from_request,
    get_default_store,
    payment_required_body,
    payment_required_headers,
)
from .service import (
    REPLAY_HTTP_STATUS,
    VERIFICATION_STATUS,
    get_facilitator_router,
    validate_client_amount,
)

router = APIRouter(prefix="/v1/x402", tags=["x402"])

__all__ = ["health", "payment_challenge", "router", "settle", "verify"]

#: Operation the middleware gates every non-free path under. The 402 challenge
#: quotes this operation's server-authoritative price, so a verify that claims
#: LESS than that quote is an underpayment and must never be honoured. This is
#: the "bind 402 quote -> verify" half of server-authoritative pricing: the
#: client can never assert an amount below the operation's required minimum.
GATE_OPERATION = "api_access"


def _get_store() -> PaymentStore:
    """Return the shared, process-wide verified-payment store."""
    return get_default_store()


def _402_challenge(resource: str = "") -> JSONResponse:
    """Build a spec-compliant 402 response for an unpaid caller."""
    body = payment_required_body(resource=resource, operation="api_access")
    headers = {
        HEADER_PAYMENT_REQUIRED: payment_required_headers(body)[HEADER_PAYMENT_REQUIRED],
        HEADER_X_PAYMENT_REQUIRED: "true",
        HEADER_X_PAYMENT_OPTIONS: "x402",
    }
    return JSONResponse(status_code=402, content=body, headers=headers)


async def _read_json(request: Request) -> dict[str, Any]:
    try:
        raw = await request.body()
        return json.loads(raw or b"{}")
    except (json.JSONDecodeError, RuntimeError):
        return {}


def _amount(payload: dict[str, Any]) -> float | None:
    """Parse a client-asserted USD amount, or ``None`` when absent/invalid.

    Security (C1): a MISSING amount must never silently default to a valid
    zero — the caller has to state an amount, and the endpoints reject
    ``None``/``<= 0`` with HTTP 400 before any verification is attempted.
    """
    raw = payload.get("amount_usd", payload.get("amountUsd"))
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _reject_amount(message: str) -> JSONResponse:
    """400 for an invalid/under-minimum client amount on the wire path."""
    return JSONResponse(
        status_code=400,
        content={"x402Version": 1, "error": message},
    )


@router.get("/payment")
async def payment_challenge() -> JSONResponse:
    """402 challenge: advertise what this server accepts for a paid call."""
    return _402_challenge()


@router.post("/verify")
async def verify(request: Request) -> JSONResponse:
    """Verify a payment proof via the FacilitatorRouter; record it on success."""
    payload = await _read_json(request)
    payment_id = str(payload.get("payment_id") or payload.get("paymentId") or "")
    tx_hash = str(payload.get("tx_hash") or payload.get("transactionHash") or "")
    network = str(payload.get("network") or "")
    asset = str(payload.get("asset") or "")
    amount_usd = _amount(payload)

    if not payment_id or not tx_hash:
        return JSONResponse(
            status_code=400,
            content={"x402Version": 1, "error": "payment_id and tx_hash are required"},
        )

    # C1/H1: never verify a zero, negative or MISSING amount (an absent amount
    # must not default to a valid zero), and never let the client assert an
    # amount below the gated operation's server-authoritative price. Both are
    # hard HTTP 400s — nothing is delegated to a facilitator.
    if amount_usd is None or amount_usd <= 0:
        return _reject_amount("amount_usd must be a positive number (missing/zero/negative rejected)")
    if network or asset:
        ok, reason = validate_client_amount(GATE_OPERATION, amount_usd, asset, network)
        if not ok:
            return _reject_amount(f"amount_usd rejected: {reason}")

    result = await get_facilitator_router().verify(
        payment_id,
        tx_hash,
        network,
        asset,
        amount_usd,
    )

    # Agent-identity attribution (W6): optional declared identity, recorded
    # only for VERIFIED payments — attribution bookkeeping, never authz.
    if result.get("verified"):
        try:
            from coffer._engine.extensions.agent_identity import (
                extract_agent_identity,
                get_agent_registry,
            )

            identity = extract_agent_identity(payload)
            get_agent_registry().record_payment(
                wallet=str(result.get("signer") or payload.get("payer") or payment_id),
                identity=identity,
                amount_atomic=int(result.get("amount_atomic", 0) or 0),
            )
        except Exception:  # noqa: BLE001, S110 — attribution never breaks verification
            pass

    if not result.get("verified"):
        body = payment_required_body(operation="api_access")
        headers = payment_required_headers(body)
        # Replay of an already-used tx_hash maps to 410 Gone (contract G2/G8);
        # every other failure re-offers the 402 acceptance surface.
        status = int(result.get("http_status") or 402)
        return JSONResponse(
            status_code=status,
            content={
                "x402Version": 1,
                "error": result.get("reason", "payment verification failed"),
                **body,
            },
            headers=headers,
        )

    _get_store().record(
        payment_id=payment_id,
        caller_key=caller_key_from_request(request),
        tx_hash=tx_hash,
        network=network or str(result.get("network") or ""),
        asset=asset or str(result.get("asset") or ""),
        amount_usd=float(result.get("amount_usd") or amount_usd),
        facilitator=str(result.get("facilitator") or ""),
        verified=True,
    )
    return JSONResponse(
        content={
            "verified": True,
            "reason": result.get("reason", ""),
            "facilitator": result.get("facilitator", ""),
            "payment_id": payment_id,
            "transaction": tx_hash,
            "network": network or result.get("network", ""),
            "asset": asset or result.get("asset", ""),
            "amount_atomic": result.get("amount_atomic", ""),
            "signer": result.get("signer", ""),
            "settlement": result.get("settlement"),
            "replay": {"header": HEADER_X_PAYMENT_ID, "value": payment_id},
        }
    )


@router.post("/settle")
async def settle(request: Request) -> JSONResponse:
    """Settle a verified payment via the FacilitatorRouter."""
    payload = await _read_json(request)
    payment_id = str(payload.get("payment_id") or payload.get("paymentId") or "")
    tx_hash = str(payload.get("tx_hash") or payload.get("transactionHash") or "")
    network = str(payload.get("network") or "")
    asset = str(payload.get("asset") or "")
    amount_usd = _amount(payload)

    if not payment_id or not tx_hash:
        return JSONResponse(
            status_code=400,
            content={"error": "payment_id and tx_hash are required"},
        )
    if amount_usd is None or amount_usd <= 0:
        return _reject_amount("amount_usd must be a positive number (missing/zero/negative rejected)")

    settlement = await get_facilitator_router().settle(
        payment_id,
        tx_hash,
        network,
        asset,
        amount_usd,
    )
    # Customer attribution: explicit user_id wins (auth-proxy deployments),
    # otherwise fall back to the caller identity (client IP / API key subject).
    user_id = str(payload.get("user_id") or payload.get("userId") or "").strip()
    if not user_id:
        user_id = caller_key_from_request(request)
    if user_id:
        settlement["user_id"] = user_id
    return JSONResponse(content=settlement)


@router.get("/health")
async def health() -> JSONResponse:
    """Honest acceptance + verification status. Scaffolded chains are NEVER
    reported as fully live."""
    store = _get_store()
    return JSONResponse(
        content={
            "status": "ok",
            "x402Version": 1,
            "verification_status": dict(VERIFICATION_STATUS),
            "replay_http_status": REPLAY_HTTP_STATUS,
            "payment_validity_seconds": PAYMENT_VALIDITY_SECONDS,
            "active_payments": len(store),
            "replay_header": HEADER_X_PAYMENT_ID,
            "offer_header": HEADER_PAYMENT_REQUIRED,
        }
    )
