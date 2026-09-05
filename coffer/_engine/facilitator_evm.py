# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Rug Munch Media LLC
# --- facilitator_evm.py: EVM verify/settle logic for the facilitator engine ---
from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from .evm_verify import _eip7702_verify
from .models import VerifyResult
from .pricing import network_family, normalize_network_id
from .replay import REPLAY_HTTP_STATUS

if TYPE_CHECKING:
    from .facilitator_router import FacilitatorRouter

log = logging.getLogger(__name__)


async def verify_self_hosted_evm(
    router: FacilitatorRouter,
    payment_id: str,
    tx_hash: str,
    network_id: str,
    asset: str,
    amount_usd: float,
) -> dict[str, Any]:
    """Self-hosted EVM verify: impl.verify + EIP-7702 receipt fallback."""
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
    eip = await _eip7702_verify(tx_hash, network_id, asset, amount_usd)
    if eip["verified"]:
        eip["facilitator"] = "self-hosted-eip7702"
        eip["network"] = network_id
        eip["hex_reason"] = reason
        return eip
    reason = f"{reason}; EIP-7702: {eip.get('reason', '')}"

    return {
        "verified": False,
        "reason": reason,
        "settlement": None,
        "facilitator": "self-hosted",
        "network": network_id,
    }


async def settle_self_hosted_evm(
    router: FacilitatorRouter,
    payment_id: str,
    tx_hash: str,
    network_id: str,
    asset: str,
    amount_usd: float,
) -> dict[str, Any]:
    """Self-hosted EVM settle via the configured facilitator impl."""
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
    return {
        "success": False,
        "payment_id": payment_id,
        "reason": "self-hosted settle unavailable: no authorization, "
        "no facilitator key/RPC, or chain unreachable",
        "facilitator": "self-hosted",
    }


def settle_fallback_evm(network_id: str, payment_id: str) -> dict[str, Any]:
    """Fail-closed EVM settle fallback (C1 honesty fix).

    EIP-7702 self-VERIFY checks on-chain receipts; it does NOT broadcast a
    settlement. Returning success here would fabricate a settlement record.
    """
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


def is_evm_network(network_id: str) -> bool:
    return network_family(normalize_network_id(network_id)) == "evm"
