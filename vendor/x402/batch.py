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

import threading
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from x402 import PAYMENT_VALIDITY_SECONDS, X402_DEFAULT_ASSET, X402_DEFAULT_NETWORK
from x402.facilitator import get_facilitator_router
from x402.scheme import X402_PRICING, _to_atomic, build_payment_required

# Active batch payment requests: batch_id -> {operations, total_usd, paid, tx_hash}
_batch_payments: dict[str, dict[str, Any]] = {}
_batch_lock = threading.Lock()


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
    payment_required = build_payment_required(
        operation=valid_ops[0],
        resource=f"https://coffer.example/api/batch?ops={','.join(valid_ops)}",
        description=f"Batch payment for {len(valid_ops)} operations",
    )
    # Override the accepts amount to the batch total
    for acceptance in payment_required.get("accepts", []):
        asset = acceptance.get("asset", X402_DEFAULT_ASSET)
        acceptance["maxAmountRequired"] = str(_to_atomic(total, asset))
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


async def verify_batch_payment(batch_id: str, tx_hash: str, network: str = "", asset: str = "") -> dict[str, Any]:
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


def is_batch_paid(batch_id: str) -> bool:
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
    return True
