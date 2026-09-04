"""x402 — batch payment helpers.

Owns the in-memory batch payment registry and the create/verify/paid
helpers that let a single x402 payment cover multiple operations. Config
constants come from the package root; scheme/facilitator helpers from their
modules.
"""
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Rug Munch Media LLC
#
# Standalone x402 implementation — https://git.rugmunch.io/RugMunchMedia/x402-py
# Licensed under MIT. See LICENSE.

from __future__ import annotations

import dataclasses
import hashlib
import os
import threading
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from .facilitator_config import PAYMENT_VALIDITY_SECONDS, X402_DEFAULT_ASSET, X402_DEFAULT_NETWORK
from .facilitator_router import get_facilitator_router
from .pricing import X402_PRICING
from .scheme import asset_to_atomic, build_payment_required

# Active batch payment requests: batch_id -> {operations, total_usd, paid, tx_hash}
_batch_payments: dict[str, dict[str, Any]] = {}
_batch_lock = threading.Lock()


@dataclasses.dataclass
class BatchChannel:
    """On-chain batch settlement channel with voucher/claim/refund model.

    A channel is opened by a depositor on a given chain and funds a set of
    claims. Claims increment ``cumulative_claimed`` up to
    ``max_claims_per_batch``; a channel can be refunded to return the
    remaining balance.
    """

    channel_id: str
    chain_id: str
    depositor: str
    cumulative_claimed: int = 0
    max_claims_per_batch: int = 100
    status: str = "open"  # open / claimed / refunded


# Active batch settlement channels: channel_id -> BatchChannel
_channels: dict[str, BatchChannel] = {}


async def create_batch_payment(operations: list[str]) -> dict[str, Any]:
    """Create a single x402 payment requirement covering multiple operations.

    Returns a dict with batch_id, total_usd, per-operation breakdown, and a
    spec-compliant PaymentRequired body.
    """
    valid_ops: list[str] = []
    breakdown: list[dict[str, Any]] = []
    total = 0.0
    for op in operations:
        info = X402_PRICING.get(op)
        if not info:
            continue
        valid_ops.append(op)
        price = info["price_usd"]
        total += price
        breakdown.append({"operation": op, "price_usd": price})

    if not valid_ops:
        return {"error": "No valid operations provided"}

    batch_id = f"batch_{uuid.uuid4().hex[:12]}"
    _base = os.getenv("X402_BATCH_RESOURCE_BASE", "https://your-domain.com")
    payment_required = build_payment_required(
        operation=valid_ops[0],
        resource=f"{_base}/api/batch?ops={','.join(valid_ops)}",
    )
    # Override the accepts amount to the batch total
    for acceptance in payment_required.get("accepts", []):
        asset = acceptance.get("asset", X402_DEFAULT_ASSET)
        network = acceptance.get("network", X402_DEFAULT_NETWORK)
        acceptance["maxAmountRequired"] = str(asset_to_atomic(total, asset, network))
        acceptance["description"] = f"Batch: {', '.join(valid_ops)}"
        acceptance["extra"]["batch_id"] = batch_id
        acceptance["extra"]["operations"] = valid_ops

    with _batch_lock:
        _batch_payments[batch_id] = {
            "batch_id": batch_id,
            "operations": valid_ops,
            "total_usd": round(total, 6),
            "paid": False,
            "tx_hash": "",
            "payment_required": payment_required,
            "created_at": datetime.now(UTC).isoformat(),
            "expires_at": (datetime.now(UTC) + timedelta(seconds=PAYMENT_VALIDITY_SECONDS)).isoformat(),
        }

    return {
        "batch_id": batch_id,
        "total_usd": round(total, 6),
        "operations": breakdown,
        "payment_required": payment_required,
    }


async def verify_batch_payment(
    batch_id: str, tx_hash: str, network: str = "", asset: str = ""
) -> dict[str, Any]:
    """Verify a batch payment on-chain and mark it paid if valid."""
    with _batch_lock:
        batch = _batch_payments.get(batch_id)
    if not batch:
        return {"verified": False, "reason": "Batch not found"}
    if batch.get("paid"):
        return {"verified": True, "batch_id": batch_id, "note": "Already paid"}

    router = get_facilitator_router()
    result = await router.verify(
        batch_id,
        tx_hash,
        network=network or X402_DEFAULT_NETWORK,
        asset=asset or X402_DEFAULT_ASSET,
        amount_usd=batch["total_usd"],
    )
    if result["verified"]:
        with _batch_lock:
            _batch_payments[batch_id]["paid"] = True
            _batch_payments[batch_id]["tx_hash"] = tx_hash
            _batch_payments[batch_id]["verified_at"] = datetime.now(UTC).isoformat()
    return {
        "verified": result["verified"],
        "batch_id": batch_id,
        "reason": result.get("reason", ""),
        "total_usd": batch["total_usd"],
    }


def is_batch_paid(batch_id: str, caller_key: str = "") -> bool:
    """Check whether a batch payment has been verified and is still valid.

    Batch payments are in-memory and expire after PAYMENT_VALIDITY_SECONDS;
    expired batches fail closed (client must create a new batch payment).
    """
    batch = _batch_payments.get(batch_id)
    if not batch or not batch.get("paid"):
        return False
    expires_at = batch.get("expires_at")
    if expires_at:
        try:
            if datetime.fromisoformat(expires_at) <= datetime.now(UTC):
                return False
        except ValueError:
            return False
    stored_caller = batch.get("caller_key", "")
    return not (stored_caller and stored_caller != caller_key)


def create_channel(chain_id: str, depositor: str) -> BatchChannel:
    """Open a new on-chain batch settlement channel for a depositor.

    Generates a deterministic-ish channel id from chain, depositor, and the
    current time, then stores the channel in the in-memory registry.
    """
    channel_id = hashlib.sha256(f"{chain_id}:{depositor}:{time.time()}".encode()).hexdigest()[:16]
    channel = BatchChannel(
        channel_id=channel_id,
        chain_id=chain_id,
        depositor=depositor,
    )
    with _batch_lock:
        _channels[channel_id] = channel
    return channel


def claim_channel(channel_id: str, amount: int) -> dict:
    """Claim ``amount`` from an open channel.

    Fails if the channel does not exist, is not open, or the claim would
    exceed ``max_claims_per_batch``. On success increments
    ``cumulative_claimed`` and returns the remaining balance.
    """
    with _batch_lock:
        channel = _channels.get(channel_id)
        if not channel:
            return {"success": False, "reason": "Channel not found"}
        if channel.status != "open":
            return {"success": False, "reason": f"Channel is {channel.status}"}
        if channel.cumulative_claimed + amount > channel.max_claims_per_batch:
            return {
                "success": False,
                "reason": "Exceeds max_claims_per_batch",
                "max_claims_per_batch": channel.max_claims_per_batch,
            }
        channel.cumulative_claimed += amount
        remaining = channel.max_claims_per_batch - channel.cumulative_claimed
    return {
        "success": True,
        "claimed": amount,
        "remaining": remaining,
    }


def refund_channel(channel_id: str) -> dict:
    """Refund a channel, marking it as refunded and returning the balance.

    The remaining balance is the unclaimed portion of ``max_claims_per_batch``.
    """
    with _batch_lock:
        channel = _channels.get(channel_id)
        if not channel:
            return {"success": False, "reason": "Channel not found"}
        if channel.status == "refunded":
            return {"success": False, "reason": "Channel already refunded"}
        channel.status = "refunded"
        remaining = channel.max_claims_per_batch - channel.cumulative_claimed
    return {
        "success": True,
        "channel_id": channel_id,
        "refunded": remaining,
    }


def get_channel(channel_id: str) -> BatchChannel | None:
    """Return the channel for ``channel_id``, or None if it does not exist."""
    with _batch_lock:
        return _channels.get(channel_id)
