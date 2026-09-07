# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Rug Munch Media LLC
# --- facilitator_orchestration.py: verify/settle dispatch for FacilitatorRouter ---
from __future__ import annotations

import asyncio
import json
import logging
import math
import random
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import httpx

from .external_verify import normalize_settlement, verify_external
from .facilitator_chains import (
    is_scaffolded_network,
    scaffold_non_evm_verify,
    settle_fallback_non_evm,
    settle_fallback_solana_self_hosted,
    verify_solana_payment,
    verify_tron_payment_flow,
)
from .facilitator_config import (
    X402_NETWORK_WALLETS,
    X402_OFFLINE_MODE,
    X402_TIMEOUT_SECONDS,
    X402_WALLET,
)
from .facilitator_evm import (
    is_evm_network,
    settle_fallback_evm,
    settle_self_hosted_evm,
    verify_self_hosted_evm,
)
from .pricing import (
    asset_to_atomic,
    network_family,
    normalize_network_id,
)
from .ratelimit import check_rate
from .replay import REPLAY_HTTP_STATUS
from .spending import enforce_spending, get_spending_policy

if TYPE_CHECKING:
    from .facilitator_router import FacilitatorRouter

log = logging.getLogger(__name__)

#: HTTP status for policy denials surfaced by the orchestrator (router maps it
#: onto an RFC 7807 ``application/problem+json`` response).
POLICY_DENIED_HTTP_STATUS = 429


def _resolve_quote(amount_usd: float, asset: str, network_id: str) -> tuple[int | None, str]:
    """Single source of truth for the atomic amount quote (audit #62).

    Previously the atomic conversion was computed independently in the
    spending policy, the verify path and the settle payload — three chances
    for the quoted amount to drift from what was verified. Every path now
    resolves through this one function.

    Returns ``(atomic_amount_or_None, reason)``. ``None`` means the
    (asset, network) pair is unquotable — callers fail closed.
    """
    try:
        return asset_to_atomic(float(amount_usd), asset, network_id), ""
    except Exception as e:  # noqa: BLE001 — unquotable -> fail closed
        return None, str(e)


def _assert_quote_consistency(verify_atomic: int | None, settle_atomic: int | None, payment_id: str) -> str:
    """Assert the settle quote matches the verified quote (audit #62).

    Returns an error reason when the amounts disagree (the settle must be
    refused rather than broadcast with a different amount than was verified).
    """
    if verify_atomic is None or settle_atomic is None:
        return ""
    if verify_atomic != settle_atomic:
        log.error(
            "x402_quote_mismatch payment_id=%s verify_atomic=%s settle_atomic=%s",
            payment_id,
            verify_atomic,
            settle_atomic,
        )
        return f"quote mismatch: verified {verify_atomic} != settle {settle_atomic} atomic"
    return ""


def _enforce_policies(
    *,
    caller_key: str,
    amount_usd: float | None,
    asset: str,
    network_id: str,
) -> dict[str, Any] | None:
    """Wire the (previously dead-code) RateLimiter + SpendingPolicy into the
    verify/settle dispatch.

    Both are caller-attributed; with no caller identity (direct handler calls)
    they are skipped rather than silently charging the empty key. A denial
    returns a result fragment carrying ``http_status`` + ``retry_after`` so the
    HTTP layer can emit a proper 429 problem; ``None`` means allowed.
    """
    if not caller_key:
        return None
    # RateLimiter: per-caller request velocity on verify AND settle.
    allowed, retry_after = check_rate(caller_key)
    if not allowed:
        return {
            "http_status": POLICY_DENIED_HTTP_STATUS,
            "retry_after": retry_after,
            "reason": "rate limit exceeded",
        }
    # SpendingPolicy: value ceiling on the amount accepted for verification.
    if amount_usd is None or asset is None:
        return None
    try:
        finite = math.isfinite(float(amount_usd)) and float(amount_usd) > 0
    except (TypeError, ValueError):
        finite = False
    if not finite:
        return None  # the invalid-amount guards below own this rejection
    try:
        amount_atomic = _resolve_quote(float(amount_usd), asset, network_id)[0]
    except Exception:  # noqa: BLE001 — unquantifiable spend skips the ceiling
        amount_atomic = None
    if amount_atomic is None:
        return None
    ok, reason = enforce_spending(caller_key, amount_atomic)
    if not ok:
        return {
            "http_status": POLICY_DENIED_HTTP_STATUS,
            "retry_after": get_spending_policy().window_seconds,
            "reason": reason,
        }
    return None


# ── verify ────────────────────────────────────────────────────────────────────


async def verify(
    router: FacilitatorRouter,
    payment_id: str,
    tx_hash: str,
    network: str,
    asset: str,
    amount_usd: float,
    caller_key: str = "",
) -> dict[str, Any]:
    """Verify a payment with the best available facilitator.

    Returns ``{"verified", "reason", "settlement", "facilitator", ...}``.
    Replay-protected: a successfully-verified tx_hash is burned (SETNX,
    24h TTL) and cannot be reused. When ``caller_key`` is supplied the
    RateLimiter + SpendingPolicy are enforced before any dispatch (policy
    denials return ``http_status`` 429 + ``retry_after``).
    """
    network_id = normalize_network_id(network)

    # RateLimiter / SpendingPolicy (previously dead code) — enforced per
    # caller before any facilitator is consulted.
    denied = _enforce_policies(
        caller_key=caller_key,
        amount_usd=amount_usd,
        asset=asset,
        network_id=network_id,
    )
    if denied is not None:
        return {
            "verified": False,
            "settlement": None,
            "network": network_id,
            **denied,
        }

    # Chain activity gauge (fail-safe): 1 = supported network attempted.
    try:
        from .observability import set_chain_active

        set_chain_active(network_id, bool(network_id))
    except Exception:  # noqa: BLE001, S110 — observability never breaks payments
        pass

    custom = router._custom_facilitators.get(network_id)
    if custom:
        return await custom.verify(payment_id, tx_hash, network, asset, amount_usd)

    # Reject unsupported networks before any dispatch to verifiers.
    if not network_id:
        return {"verified": False, "reason": "unsupported network", "settlement": None}

    # Reject invalid amounts before any network call. C1: zero is NOT a valid
    # amount — a $0/dust claim would make the required atomic amount 0 and let
    # a worthless on-chain transfer verify. Only a strictly positive, finite
    # amount may proceed (None is handled by the isfinite check below).
    if amount_usd is None or not math.isfinite(float(amount_usd)) or amount_usd <= 0:
        return {
            "verified": False,
            "reason": "invalid (non-finite/<=0) amount",
            "settlement": None,
            "network": network_id,
        }

    if X402_OFFLINE_MODE:
        ok, reason = await router._enforce_replay(tx_hash, network_id)
        if not ok:
            return {
                "verified": False,
                "reason": reason,
                "settlement": None,
                "network": network_id,
                "http_status": REPLAY_HTTP_STATUS,
            }
        return {
            "verified": True,
            "reason": "offline_mode",
            "settlement": None,
            "amount_atomic": _resolve_quote(amount_usd, asset, network_id)[0],
        }

    # TRON: real TRC-20 verifier (no longer scaffolded).
    if network_family(network_id) == "tron":
        return await verify_tron_payment_flow(router, payment_id, tx_hash, network_id, asset, amount_usd)

    # Other scaffolded networks stay scaffolded — never claimed verified.
    if is_scaffolded_network(network_id):
        return scaffold_non_evm_verify(payment_id, tx_hash, network_id, asset, amount_usd)

    # Self-hosted / custom impl path (default).
    if router.self_hosted or router._impl_for(network_id) is not None:
        result = await _verify_self_hosted(router, payment_id, tx_hash, network_id, asset, amount_usd)
        if result.get("verified"):
            ok, reason = await router._enforce_replay(tx_hash, network_id)
            if not ok:
                return {
                    "verified": False,
                    "reason": reason,
                    "settlement": None,
                    "network": network_id,
                    "http_status": REPLAY_HTTP_STATUS,
                }
        return result

    # External facilitator path.
    for attempt in range(3):
        try:
            verified_result = await verify_external(
                router, payment_id, tx_hash, network_id, asset, amount_usd
            )
            break
        except (httpx.HTTPError, asyncio.TimeoutError):  # noqa: UP041 — explicit per spec
            if attempt == 2:
                raise
            delay = 0.5 * (2**attempt)
            await asyncio.sleep(delay)
    from coffer._engine.observability import observe_verify
    from coffer._engine.webhooks import emit_payment_event

    observe_verify(verified_result, payment_id=payment_id, tx_hash=tx_hash)
    emit_payment_event(
        "payment.verified",
        payment_id=payment_id,
        transaction=tx_hash,
        network=network_id,
        verified=bool(verified_result.get("verified")),
    )
    return verified_result


async def _verify_self_hosted(
    router: FacilitatorRouter,
    payment_id: str,
    tx_hash: str,
    network_id: str,
    asset: str,
    amount_usd: float,
) -> dict[str, Any]:
    """Dispatch self-hosted verify to the chain-family module."""
    if is_evm_network(network_id):
        return await verify_self_hosted_evm(router, payment_id, tx_hash, network_id, asset, amount_usd)
    if network_family(network_id) == "solana":
        return await verify_solana_payment(payment_id, tx_hash, network_id, asset, amount_usd)
    return {
        "verified": False,
        "reason": "self-hosted verify unavailable for this chain family",
        "settlement": None,
        "facilitator": "self-hosted",
        "network": network_id,
    }


# ── settle ────────────────────────────────────────────────────────────────────


async def settle(
    router: FacilitatorRouter,
    payment_id: str,
    tx_hash: str,
    network: str,
    asset: str,
    amount_usd: float,
    caller_key: str = "",
) -> dict[str, Any]:
    """Confirm settlement with the best available facilitator.

    When ``caller_key`` is supplied the RateLimiter is enforced first (policy
    denials return ``http_status`` 429 + ``retry_after``).
    """
    network_id = normalize_network_id(network)

    # RateLimiter (previously dead code) — per-caller request velocity.
    denied = _enforce_policies(
        caller_key=caller_key,
        amount_usd=None,  # settle confirms value already capped at verify
        asset="",
        network_id="",
    )
    if denied is not None:
        return {"success": False, "payment_id": payment_id, **denied}

    if X402_OFFLINE_MODE:
        return {
            "success": True,
            "transaction": tx_hash,
            "network": network_id,
            "payment_id": payment_id,
            "facilitator": "offline",
            "settled_at": datetime.now(UTC).isoformat(),
            "note": "Offline mode",
        }

    if router.self_hosted or router._impl_for(network_id) is not None:
        if network_family(network_id) == "solana":
            # C2 honesty fix: SPL RPC verifies receipts, never broadcasts.
            return settle_fallback_solana_self_hosted(network_id, payment_id)
        return await settle_self_hosted_evm(router, payment_id, tx_hash, network_id, asset, amount_usd)

    payload = {
        "paymentId": payment_id,
        "transactionHash": tx_hash,
        "network": network_id,
        "asset": asset,
        "amount": str(_resolve_quote(amount_usd, asset, network_id)[0]),
        "payToAddress": X402_NETWORK_WALLETS.get(network_id) or X402_WALLET,
        "scheme": "exact",
        "timestamp": datetime.now(UTC).isoformat(),
    }
    last_error = "No facilitators available"
    async with httpx.AsyncClient(timeout=X402_TIMEOUT_SECONDS) as client:
        healthy = [f for f in router.facilitators if router.health.is_healthy(f) and f.settle_url]
        if not healthy:
            return {
                "success": False,
                "payment_id": payment_id,
                "reason": last_error,
            }
        # Weighted random: higher health score = higher selection probability
        scores = [max(1, router.health.health_score(f)) for f in healthy]
        total = sum(scores)
        weights = [s / total for s in scores]
        selected = random.choices(healthy, weights=weights, k=1)[0]
        # Try the weighted-random pick first, then fall through to the rest.
        ordered = [selected] + [f for f in healthy if f is not selected]
        for config in ordered:
            try:
                start_time = time.time()
                response = await client.post(config.settle_url, json=payload)
                response.raise_for_status()
                data = response.json()
                router.health.mark_success(config, start_time)
                settled = normalize_settlement(data, payment_id, tx_hash, network_id, config.name)
                from coffer._engine.observability import observe_settle

                observe_settle(settled, payment_id=payment_id, tx_hash=tx_hash)
                return settled
            except httpx.HTTPStatusError as e:
                last_error = f"{config.name}: HTTP {e.response.status_code}"
                router.health.mark_failure(config)
            except httpx.RequestError as e:
                last_error = f"{config.name}: {e}"
                router.health.mark_failure(config)
            except json.JSONDecodeError as e:
                last_error = f"{config.name}: invalid JSON - {e}"
                router.health.mark_failure(config)
    if is_evm_network(network_id):
        return settle_fallback_evm(network_id, payment_id)
    if network_family(network_id) in ("solana", "tron"):
        return settle_fallback_non_evm(network_id, payment_id)
    return {"success": False, "payment_id": payment_id, "reason": last_error}
