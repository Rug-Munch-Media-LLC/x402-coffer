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
from .replay import REPLAY_HTTP_STATUS

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
        return {"verified": True, "reason": "offline_mode", "settlement": None}

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
        if network_family(network_id) == "solana":
            # C2 honesty fix: SPL RPC verifies receipts, never broadcasts.
            return settle_fallback_solana_self_hosted(network_id, payment_id)
        return await settle_self_hosted_evm(router, payment_id, tx_hash, network_id, asset, amount_usd)

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
    if is_evm_network(network_id):
        return settle_fallback_evm(network_id, payment_id)
    if network_family(network_id) in ("solana", "tron"):
        return settle_fallback_non_evm(network_id, payment_id)
    return {"success": False, "payment_id": payment_id, "reason": last_error}
