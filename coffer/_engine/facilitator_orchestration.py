# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Rug Munch Media LLC
# --- facilitator_orchestration.py: verify/settle orchestration for FacilitatorRouter ---
from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import random
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import httpx

from .evm_verify import _eip7702_verify
from .external_verify import normalize_settlement, verify_external, verify_solana
from .facilitator_config import (
    X402_NETWORK_WALLETS,
    X402_OFFLINE_MODE,
    X402_TIMEOUT_SECONDS,
    X402_WALLET,
)
from .models import VerifyResult
from .pricing import (
    SCAFFOLDED_NETWORKS,
    asset_to_atomic,
    network_family,
    network_verification_status,
    normalize_network_id,
    resolve_asset_entry,
)
from .replay import REPLAY_HTTP_STATUS
from .tron_verifier import verify_tron_payment

if TYPE_CHECKING:
    from .facilitator_router import FacilitatorRouter

log = logging.getLogger(__name__)


# ── verify ────────────────────────────────────────────────────────────────────


async def verify(
    router: FacilitatorRouter,
    payment_id: str,
    tx_hash: str,
    network: str,
    asset: str,
    amount_usd: float,
) -> dict[str, Any]:
    """Verify a payment with the best available facilitator.

    Returns ``{"verified", "reason", "settlement", "facilitator", ...}``.
    Replay-protected: a successfully-verified tx_hash is burned (SETNX,
    24h TTL) and cannot be reused.
    """
    network_id = normalize_network_id(network)

    custom = router._custom_facilitators.get(network_id)
    if custom:
        return await custom.verify(payment_id, tx_hash, network, asset, amount_usd)

    # Reject unsupported networks before any dispatch to verifiers.
    if not network_id:
        return {"verified": False, "reason": "unsupported network", "settlement": None}

    # Reject invalid amounts before any network call.
    if amount_usd is None or not math.isfinite(float(amount_usd)) or amount_usd < 0:
        return {
            "verified": False,
            "reason": "invalid (non-finite/negative) amount",
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
        return {"verified": True, "reason": "offline_mode", "settlement": None}

    # TRON: real TRC-20 verifier (no longer scaffolded).
    if network_family(network_id) == "tron":
        return await _verify_tron(router, payment_id, tx_hash, network_id, asset, amount_usd)

    # Other scaffolded networks stay scaffolded — never claimed verified.
    if network_id in SCAFFOLDED_NETWORKS:
        return _scaffold_non_evm_verify(payment_id, tx_hash, network_id, asset, amount_usd)

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
    network: str,
    asset: str,
    amount_usd: float,
) -> dict[str, Any]:
    network_id = normalize_network_id(network)
    impl = router._impl_for(network_id) or router._get_self_facilitator()
    payment = router._build_payment(payment_id, tx_hash, network_id, asset, amount_usd)
    try:
        result: Any = await impl.verify(payment)
    except Exception as e:  # noqa: BLE001 — facilitator verify never raises
        result = VerifyResult(verified=False, reason=f"facilitator verify error: {e}")
    if getattr(result, "verified", False):
        return {
            "verified": True,
            "reason": "",
            "settlement": None,
            "facilitator": "self-hosted",
            "signer": getattr(result, "signer", ""),
            "amount_atomic": getattr(result, "amount_atomic", 0),
            "network": getattr(result, "network", None) or network_id,
            "asset": getattr(result, "asset", None) or asset,
        }
    reason = getattr(result, "reason", "") or ""
    if reason == "replay" or "replay" in reason:
        return {
            "verified": False,
            "reason": "transaction already used (replay)",
            "settlement": None,
            "network": network_id,
            "http_status": REPLAY_HTTP_STATUS,
        }

    # EIP-7702 self-verify fallback for EVM chains (receipt + payTo check).
    if network_family(network_id) == "evm":
        eip = await _eip7702_verify(tx_hash, network_id, asset, amount_usd)
        if eip["verified"]:
            eip["facilitator"] = "self-hosted-eip7702"
            eip["network"] = network_id
            eip["hex_reason"] = reason
            return eip
        reason = f"{reason}; EIP-7702: {eip.get('reason', '')}"
    elif network_family(network_id) == "solana":
        return await verify_solana(payment_id, tx_hash, network_id, asset, amount_usd)

    return {
        "verified": False,
        "reason": reason,
        "settlement": None,
        "facilitator": "self-hosted",
        "network": network_id,
    }


async def _verify_tron(
    router: FacilitatorRouter,
    payment_id: str,
    tx_hash: str,
    network: str,
    asset: str,
    amount_usd: float,
) -> dict[str, Any]:
    """Verify a TRON TRC-20 USDT payment via the Trongrid RPC."""
    network_id = normalize_network_id(network)
    recipient = X402_NETWORK_WALLETS.get(network_id) or X402_WALLET
    rpc_url = os.getenv("X402_TRON_RPC", "https://api.trongrid.io")
    fallbacks = [u.strip() for u in os.getenv("X402_TRON_RPC_FALLBACKS", "").split(",") if u.strip()]

    result = await verify_tron_payment(
        tx_hash=tx_hash,
        recipient=recipient,
        amount_usd=amount_usd,
        rpc_url=rpc_url,
        fallback_rpc_urls=fallbacks,
    )
    result["network"] = network_id
    result["payment_id"] = payment_id
    result["facilitator"] = "tron-rpc"
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


def _scaffold_non_evm_verify(
    payment_id: str,
    tx_hash: str,
    network: str,
    asset: str,
    amount_usd: float,
) -> dict[str, Any]:
    """[REVIEW] Scaffolded acceptance for TRON — NEVER returns verified.

    Header structure, asset membership and non-negative USD amount are
    validated; on-chain settlement is NOT checked. Result is explicitly
    marked ``scaffolded`` and ``verified: false`` so it can never be
    treated as paid until a real TRC-20 verifier is wired in.
    """
    network_id = normalize_network_id(network)
    entry = resolve_asset_entry(network_id, asset)
    if not payment_id or not tx_hash:
        return {
            "verified": False,
            "reason": "needs_facilitator: missing payment_id/tx_hash in header",
            "settlement": None,
            "scaffolded": True,
            "network": network_id,
        }
    if entry is None:
        return {
            "verified": False,
            "reason": f"needs_facilitator: asset {asset!r} not in SUPPORTED_ASSETS for {network_id}",
            "settlement": None,
            "scaffolded": True,
            "network": network_id,
        }
    if amount_usd is None or amount_usd < 0:
        return {
            "verified": False,
            "reason": "needs_facilitator: invalid (non-finite/negative) amount",
            "settlement": None,
            "scaffolded": True,
            "network": network_id,
        }
    return {
        "verified": False,
        "reason": "needs_facilitator: on-chain settlement verification not implemented for this chain",
        "settlement": None,
        "scaffolded": True,
        "network": network_id,
        "asset": asset,
        "supported": True,
        "verification_status": network_verification_status(network_id),
        "note": "Header structure, asset and price validated; settlement NOT verified. "
        "See VERIFICATION_STATUS.",
    }


# ── settle ────────────────────────────────────────────────────────────────────


async def settle(
    router: FacilitatorRouter,
    payment_id: str,
    tx_hash: str,
    network: str,
    asset: str,
    amount_usd: float,
) -> dict[str, Any]:
    """Confirm settlement with the best available facilitator."""
    network_id = normalize_network_id(network)
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
        return await _settle_self_hosted(router, payment_id, tx_hash, network_id, asset, amount_usd)

    payload = {
        "paymentId": payment_id,
        "transactionHash": tx_hash,
        "network": network_id,
        "asset": asset,
        "amount": str(asset_to_atomic(amount_usd, asset, network_id)),
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
    if network_family(network_id) == "evm":
        # EIP-7702 self-VERIFY checks on-chain receipts; it does NOT
        # broadcast a settlement. Returning success here would fabricate
        # a settlement record (C1 honesty fix) -> fail closed.
        return {
            "success": False,
            "payment_id": payment_id,
            "reason": (
                "needs_facilitator: no external facilitator settled and "
                "no self-hosted settle key configured (EIP-7702 verifies "
                "on-chain receipts but does not broadcast settlements)"
            ),
            "scaffolded": True,
            "network": network_id,
            "facilitator": "eip-7702-verify-only",
        }
    if network_family(network_id) in ("solana", "tron"):
        return {
            "success": False,
            "payment_id": payment_id,
            "reason": "needs_facilitator: settlement verification not implemented for this chain",
            "scaffolded": True,
            "network": network_id,
        }
    return {"success": False, "payment_id": payment_id, "reason": last_error}


async def _settle_self_hosted(
    router: FacilitatorRouter,
    payment_id: str,
    tx_hash: str,
    network: str,
    asset: str,
    amount_usd: float,
) -> dict[str, Any]:
    network_id = normalize_network_id(network)
    impl = router._impl_for(network_id) or router._get_self_facilitator()
    payment = router._build_payment(payment_id, tx_hash, network_id, asset, amount_usd)
    try:
        settled_tx = await impl.settle(payment)
    except Exception as e:  # noqa: BLE001 — settle failure is handled below
        settled_tx = None
        log.warning("x402_self_settle_error", extra={"error": str(e)})
    if settled_tx:
        return {
            "success": True,
            "transaction": str(settled_tx),
            "network": network_id,
            "payment_id": payment_id,
            "facilitator": "self-hosted",
            "settled_at": datetime.now(UTC).isoformat(),
            "note": "Broadcast by self-hosted facilitator",
        }
    if network_family(network_id) == "solana":
        # SPL RPC verifies on-chain receipts; it does NOT broadcast a
        # settlement. Returning success here would fabricate a settlement
        # record (C2 honesty fix) -> fail closed.
        return {
            "success": False,
            "payment_id": payment_id,
            "reason": (
                "needs_facilitator: solana SPL RPC verifies on-chain "
                "receipts but does not broadcast settlements"
            ),
            "scaffolded": True,
            "network": network_id,
            "facilitator": "solana-rpc-verify-only",
        }
    return {
        "success": False,
        "payment_id": payment_id,
        "reason": "self-hosted settle unavailable: no authorization, "
        "no facilitator key/RPC, or chain unreachable",
        "facilitator": "self-hosted",
    }
