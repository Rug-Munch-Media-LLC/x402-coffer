# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Rug Munch Media LLC
# --- facilitator_chains.py: Solana/TRON verify + scaffolded-chain logic ---
from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any

from .external_verify import verify_solana
from .facilitator_config import X402_NETWORK_WALLETS, X402_WALLET
from .pricing import (
    SCAFFOLDED_NETWORKS,
    network_verification_status,
    normalize_network_id,
    resolve_asset_entry,
)
from .replay import REPLAY_HTTP_STATUS
from .tron_verifier import verify_tron_payment

if TYPE_CHECKING:
    from .facilitator_router import FacilitatorRouter


async def verify_tron_payment_flow(
    router: FacilitatorRouter,
    payment_id: str,
    tx_hash: str,
    network_id: str,
    asset: str,
    amount_usd: float,
) -> dict[str, Any]:
    """Verify a TRON TRC-20 USDT payment via the Trongrid RPC."""
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


async def verify_solana_payment(
    payment_id: str,
    tx_hash: str,
    network_id: str,
    asset: str,
    amount_usd: float,
) -> dict[str, Any]:
    """Verify a Solana SPL transfer via the external verifier."""
    return await verify_solana(payment_id, tx_hash, network_id, asset, amount_usd)


def scaffold_non_evm_verify(
    payment_id: str,
    tx_hash: str,
    network: str,
    asset: str,
    amount_usd: float,
) -> dict[str, Any]:
    """[REVIEW] Scaffolded acceptance for non-EVM chains — NEVER returns verified.

    Header structure, asset membership and non-negative USD amount are
    validated; on-chain settlement is NOT checked. Result is explicitly
    marked ``scaffolded`` and ``verified: false`` so it can never be
    treated as paid until a real verifier is wired in.
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


def is_scaffolded_network(network_id: str) -> bool:
    return network_id in SCAFFOLDED_NETWORKS


def settle_fallback_solana_self_hosted(network_id: str, payment_id: str) -> dict[str, Any]:
    """Self-hosted Solana settle fail-closed (C2 honesty fix).

    SPL RPC verifies on-chain receipts; it does NOT broadcast a settlement.
    Returning success here would fabricate a settlement record.
    """
    return {
        "success": False,
        "payment_id": payment_id,
        "reason": (
            "needs_facilitator: solana SPL RPC verifies on-chain receipts but does not broadcast settlements"
        ),
        "scaffolded": True,
        "network": network_id,
        "facilitator": "solana-rpc-verify-only",
    }


def settle_fallback_non_evm(network_id: str, payment_id: str) -> dict[str, Any]:
    """Fail-closed Solana/TRON settle fallback (C2 honesty fix).

    SPL/TRON RPC verifiers check on-chain receipts; they do NOT broadcast
    settlements. Returning success here would fabricate a settlement record.
    """
    return {
        "success": False,
        "payment_id": payment_id,
        "reason": "needs_facilitator: settlement verification not implemented for this chain",
        "scaffolded": True,
        "network": network_id,
    }
