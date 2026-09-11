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
- ``POST /v1/x402/settle``   — settle a VERIFIED payment via the
  FacilitatorRouter. Idempotent + replay-checked: a prior successful verify is
  required (RFC 7807 ``409`` otherwise), the invoice is reconciled
  (recipient+amount+asset, :mod:`coffer._engine.reconciliation`) and a repeat
  settle returns the stored first outcome instead of re-rolling a facilitator.
- ``GET  /v1/x402/health``   — honest acceptance/verification status.
- ``GET  /health``           — liveness: in-memory payment store + Redis mirror.

Client errors follow RFC 7807 (``application/problem+json``) with the legacy
``error`` member kept as an alias of ``detail`` so older clients keep working.
Request bodies are size-capped and schema-validated before any logic runs.

Holds NO verification logic itself — it only wires the middleware's payment
store to the facilitator layer. The verify/settle endpoints use the
PROCESS-WIDE ``get_facilitator_router()`` singleton so failure counters,
registered per-network facilitators and the replay-nonce store are shared.
"""

from __future__ import annotations

import json
import re
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
from .reconciliation import reconcile_invoice
from .service import (
    REPLAY_HTTP_STATUS,
    VERIFICATION_STATUS,
    get_facilitator_router,
    validate_client_amount,
)
from .store import validate_payment_id

router = APIRouter(prefix="/v1/x402", tags=["x402"])

#: Top-level liveness router (mounted by ``create_app`` alongside the x402
#: router so ``GET /health`` is exempt from the payment gate — see
#: ``X402Settings.free_paths``).
health_router = APIRouter(tags=["x402-ops"])

__all__ = [
    "health",
    "health_router",
    "liveness",
    "payment_challenge",
    "router",
    "settle",
    "verify",
]

#: Operation the middleware gates every non-free path under. The 402 challenge
#: quotes this operation's server-authoritative price, so a verify that claims
#: LESS than that quote is an underpayment and must never be honoured. This is
#: the "bind 402 quote -> verify" half of server-authoritative pricing: the
#: client can never assert an amount below the operation's required minimum.
GATE_OPERATION = "api_access"

#: RFC 7807 problem+json media type for every 4xx contract error.
PROBLEM_MEDIA_TYPE = "application/problem+json"
#: Request-body cap for the verify/settle JSON endpoints (generous — the
#: payloads are a handful of scalars; oversized bodies are rejected 413).
MAX_REQUEST_BODY_BYTES = 64 * 1024


def _get_store() -> PaymentStore:
    """Return the shared, process-wide verified-payment store."""
    return get_default_store()


def _problem(
    status_code: int,
    code: str,
    detail: str,
    *,
    title: str | None = None,
    extra: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    """Build an RFC 7807 ``application/problem+json`` error response.

    Members: ``type`` (stable URI keyed by ``code``), ``title``, ``status``,
    ``detail``, ``instance`` (the code). The legacy ``error`` member is kept as
    an exact alias of ``detail`` so pre-RFC7807 clients/tests keep resolving.
    """
    content: dict[str, Any] = {
        "type": f"https://x402.org/probs/{code}",
        "title": title or code.replace("-", " ").title(),
        "status": status_code,
        "detail": detail,
        "instance": f"urn:x402:{code}",
        "error": detail,  # legacy alias of detail
    }
    if extra:
        content.update(extra)
    return JSONResponse(
        status_code=status_code, content=content, headers=headers, media_type=PROBLEM_MEDIA_TYPE
    )


def _402_challenge(resource: str = "") -> JSONResponse:
    """Build a spec-compliant 402 response for an unpaid caller."""
    body = payment_required_body(resource=resource, operation="api_access")
    headers = {
        HEADER_PAYMENT_REQUIRED: payment_required_headers(body)[HEADER_PAYMENT_REQUIRED],
        HEADER_X_PAYMENT_REQUIRED: "true",
        HEADER_X_PAYMENT_OPTIONS: "x402",
    }
    return JSONResponse(status_code=402, content=body, headers=headers)


async def _read_payload(request: Request) -> tuple[dict[str, Any] | None, JSONResponse | None]:
    """Read + schema-validate the JSON request body.

    Returns ``(payload, None)`` on success (``payload`` may be ``{}`` for an
    empty body) and ``(None, problem_response)`` on any client error: a body
    over :data:`MAX_REQUEST_BODY_BYTES` (``413``), invalid JSON (``400``) or a
    JSON value that is not an object (``400``).
    """
    try:
        raw = await request.body()
    except RuntimeError:
        return None, _problem(400, "body-unreadable", "could not read the request body")
    if len(raw) > MAX_REQUEST_BODY_BYTES:
        return (
            None,
            _problem(
                413,
                "payload-too-large",
                f"request body exceeds the {MAX_REQUEST_BODY_BYTES}-byte cap",
            ),
        )
    if not raw:
        return {}, None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None, _problem(400, "invalid-json", "request body is not valid JSON")
    if not isinstance(data, dict):
        return None, _problem(400, "invalid-json", "request body must be a JSON object")
    return data, None


def _scalar_field(payload: dict[str, Any], *names: str) -> tuple[str, JSONResponse | None]:
    """Extract the first present string-typed field among ``names``.

    Returns ``(value, None)`` or ``(value, problem_response)`` when the field
    is present but not a string (schema validation — a list/dict/object must
    never be stringified into a payment id/tx_hash/network/asset).
    """
    for name in names:
        if name not in payload or payload[name] is None:
            continue
        value = payload[name]
        if not isinstance(value, str):
            return "", _problem(400, "invalid-schema", f"field {name!r} must be a string")
        return value, None
    return "", None


def _amount(payload: dict[str, Any]) -> float | None:
    """Parse a client-asserted USD amount, or ``None`` when absent/invalid.

    Security (C1): a MISSING amount must never silently default to a valid
    zero — the caller has to state an amount, and the endpoints reject
    ``None``/``<= 0`` with HTTP 400 before any verification is attempted.
    """
    raw = payload.get("amount_usd", payload.get("amountUsd"))
    if raw is None:
        return None
    if isinstance(raw, bool):
        return None  # True/False are not amounts
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _reject_amount(message: str) -> JSONResponse:
    """400 problem for an invalid/under-minimum client amount on the wire path."""
    return _problem(
        400,
        "amount-rejected",
        message,
        extra={"x402Version": 1},
    )


def _policy_denied(http_status: int, reason: str, retry_after: int | None = None) -> JSONResponse:
    """RFC 7807 problem for a rate/spending-policy denial from the orchestrator."""
    code = "rate-limited" if http_status == 429 else "policy-denied"
    headers = {}
    if retry_after is not None:
        headers["Retry-After"] = str(max(1, int(retry_after)))
    return _problem(429, code, reason or "request limited by policy", headers=headers or None)


def _validate_common_schema(payload: dict[str, Any]) -> JSONResponse | None:
    """Shared verify/settle schema validation. Returns a problem or ``None``."""
    for name in ("payment_id", "paymentId", "tx_hash", "transactionHash", "network", "asset"):
        if name in payload and payload[name] is not None and not isinstance(payload[name], str):
            return _problem(400, "invalid-schema", f"field {name!r} must be a string")
    return None


@router.get("/payment")
async def payment_challenge() -> JSONResponse:
    """402 challenge: advertise what this server accepts for a paid call."""
    return _402_challenge()


@router.post("/verify")
async def verify(request: Request) -> JSONResponse:
    """Verify a payment proof via the FacilitatorRouter; record it on success."""
    payload, problem = await _read_payload(request)
    if problem is not None:
        return problem
    assert payload is not None  # contract: problem is None iff payload is set
    schema_problem = _validate_common_schema(payload)
    if schema_problem is not None:
        return schema_problem
    payment_id, pid_problem = _scalar_field(payload, "payment_id", "paymentId")
    if pid_problem is not None:
        return pid_problem
    tx_hash, tx_problem = _scalar_field(payload, "tx_hash", "transactionHash")
    if tx_problem is not None:
        return tx_problem
    network = str(payload.get("network") or "")
    asset = str(payload.get("asset") or "")
    amount_usd = _amount(payload)

    if not payment_id or not tx_hash:
        return _problem(
            400,
            "missing-fields",
            "payment_id and tx_hash are required",
            extra={"x402Version": 1},
        )

    # Payment-id format/length (audit): reject oversized/malformed ids BEFORE
    # any delegation — they must never reach the store, Redis or the ledger.
    pid_err = validate_payment_id(payment_id)
    if pid_err:
        return _problem(400, "invalid-payment-id", pid_err, extra={"x402Version": 1})

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

    caller_key = caller_key_from_request(request)
    result = await get_facilitator_router().verify(
        payment_id,
        tx_hash,
        network,
        asset,
        amount_usd,
        caller_key=caller_key,
    )

    # Rate/spending-policy denial surfaced by the orchestrator -> RFC 7807 429.
    if int(result.get("http_status") or 0) == 429:
        return _policy_denied(
            429, str(result.get("reason") or ""), retry_after=_int_or_none(result.get("retry_after"))
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
            payer = _trusted_payer(request, result)
            if payer:
                get_agent_registry().record_payment(
                    wallet=payer,
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
        caller_key=caller_key,
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
    """Settle a VERIFIED payment via the FacilitatorRouter.

    Idempotent + replay-checked (spec contract G2/G8):

    1. Schema/body-size validation (RFC 7807 problems).
    2. A prior successful verify for ``payment_id`` is REQUIRED — settling an
       unverified/unknown/expired invoice is a ``409``.
    3. The invoice is reconciled (recipient+amount+asset+network+tx match).
    4. A previously recorded successful settlement is returned unchanged
       (repeat settle never re-rolls a facilitator).
    5. Otherwise the FacilitatorRouter settles and the successful outcome is
       stored so a repeat call returns it.
    """
    payload, problem = await _read_payload(request)
    if problem is not None:
        return problem
    assert payload is not None  # contract: problem is None iff payload is set
    schema_problem = _validate_common_schema(payload)
    if schema_problem is not None:
        return schema_problem
    payment_id, pid_problem = _scalar_field(payload, "payment_id", "paymentId")
    if pid_problem is not None:
        return pid_problem
    tx_hash, tx_problem = _scalar_field(payload, "tx_hash", "transactionHash")
    if tx_problem is not None:
        return tx_problem
    network = str(payload.get("network") or "")
    asset = str(payload.get("asset") or "")
    amount_usd = _amount(payload)

    if not payment_id or not tx_hash:
        return _problem(400, "missing-fields", "payment_id and tx_hash are required")
    pid_err = validate_payment_id(payment_id)
    if pid_err:
        return _problem(400, "invalid-payment-id", pid_err)
    if amount_usd is None or amount_usd <= 0:
        return _reject_amount("amount_usd must be a positive number (missing/zero/negative rejected)")

    caller_key = caller_key_from_request(request)
    store = _get_store()
    verified = store.get(payment_id)

    # Prior-verify gate (idempotency + replay-checked): never settle an
    # invoice that was not verified, or whose verification expired.
    recon = reconcile_invoice(
        verified or {},
        caller_key=caller_key,
        network=network,
        asset=asset,
        amount_usd=amount_usd,
        tx_hash=tx_hash,
    )
    if not recon:
        if verified:
            # A record exists but reconciliation failed — surface the exact
            # mismatch (recipient/amount/asset/network/tx) to the caller.
            return _problem(409, "settle-conflict", recon.reason)
        return _problem(
            409,
            "settle-not-verified",
            "settle requires a prior successful verify of this payment_id "
            "by the same caller with matching amount/asset",
        )

    # Idempotent replay: a stored successful settlement is returned unchanged.
    stored = store.get_settle_outcome(payment_id)
    if stored is not None:
        if (stored.get("caller_key") or "") and caller_key and stored["caller_key"] != caller_key:
            return _problem(
                409,
                "recipient-mismatch",
                "settlement was recorded for a different caller",
            )
        result = dict(stored.get("result") or {})
        result.setdefault("idempotent_replay", True)
        return JSONResponse(content=result)

    settlement = await get_facilitator_router().settle(
        payment_id,
        tx_hash,
        network,
        asset,
        amount_usd,
        caller_key=caller_key,
    )

    # Rate-limit denial surfaced by the orchestrator -> RFC 7807 429.
    if int(settlement.get("http_status") or 0) == 429:
        return _policy_denied(
            429, str(settlement.get("reason") or ""), retry_after=_int_or_none(settlement.get("retry_after"))
        )

    # Customer attribution: explicit user_id wins (auth-proxy deployments),
    # otherwise fall back to the caller identity (client IP / API key subject).
    user_id = str(payload.get("user_id") or payload.get("userId") or "").strip()
    if not user_id:
        user_id = caller_key
    if user_id:
        settlement["user_id"] = user_id

    # Persist successful outcomes so a repeat settle replays this receipt.
    if settlement.get("success") is True:
        store.record_settle_outcome(
            payment_id=payment_id,
            caller_key=caller_key,
            outcome=settlement,
        )
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


@health_router.get("/health")
async def liveness() -> JSONResponse:
    """Liveness probe: in-memory payment store + Redis mirror status."""
    store = _get_store()
    return JSONResponse(
        content={
            "status": "ok",
            "store": store.health(),
            "payment_validity_seconds": PAYMENT_VALIDITY_SECONDS,
        }
    )


def _int_or_none(value: Any) -> int | None:
    """Best-effort int conversion for optional policy-denial retry-after."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


# Audit #61: the payer wallet used for attribution must NEVER come from the
# client-asserted payload (``payload["payer"]`` is trivially spoofable). It is
# taken from a TRUSTED PROXY header set only, and only when the deployment
# declares a trusted proxy (X402_TRUSTED_PROXY=1). The address is validated
# (0x + 40 hex chars) before it is ever recorded or used for signing.
_TRUSTED_PROXY_HEADERS = ("x-payer", "x-forwarded-payer")
_EVM_ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")


def _trusted_payer(request: Request, result: dict[str, Any]) -> str:
    """Resolve the payer address from trusted sources only.

    Priority: server-verified ``signer`` from the facilitator result, then a
    trusted-proxy header (only when X402_TRUSTED_PROXY=1 and the value is a
    well-formed EVM address). Client-asserted payload fields are ignored.
    """
    signer = str(result.get("signer") or "").strip()
    if signer and _EVM_ADDRESS_RE.fullmatch(signer):
        return signer
    import os as _os

    if _os.getenv("X402_TRUSTED_PROXY", "").lower() in ("1", "true", "yes", "on"):
        for header in _TRUSTED_PROXY_HEADERS:
            raw = str(request.headers.get(header) or "").strip()
            if raw and _EVM_ADDRESS_RE.fullmatch(raw):
                return raw
    return ""
