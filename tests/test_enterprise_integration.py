"""Enterprise integration test — full x402 payment lifecycle.

Exercises the complete happy path through ``X402Handler`` with a mocked
facilitator router: hooks (pre/post verify + settle), verify, settle,
batch channels (create/claim/refund), and PaymentInfo expiry/fee
validation. All persistence is redirected to a temp directory.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from coffer._engine.batch import (
    BatchChannel,
    claim_channel,
    create_channel,
    get_channel,
    refund_channel,
)
from coffer._engine.facilitator_router import FacilitatorRouter
from coffer._engine.handler import X402Handler
from coffer._engine.scheme import PaymentInfo


class MockRouter(FacilitatorRouter):
    """Deterministic stand-in for the facilitator router seam."""

    def __init__(self) -> None:
        self.verify_calls: list[tuple[str, str, str, str, float]] = []
        self.settle_calls: list[tuple[str, str, str, str, float]] = []

    async def verify(
        self, payment_id: str, tx_hash: str, network: str, asset: str, amount_usd: float
    ) -> dict[str, Any]:
        self.verify_calls.append((payment_id, tx_hash, network, asset, amount_usd))
        return {"verified": True, "reason": "", "facilitator": "mock"}

    async def settle(
        self, payment_id: str, tx_hash: str, network: str, asset: str, amount_usd: float
    ) -> dict[str, Any]:
        self.settle_calls.append((payment_id, tx_hash, network, asset, amount_usd))
        return {"success": True, "transaction": f"0xsettled_{tx_hash}", "network": network}


def test_full_payment_lifecycle(tmp_path: Path) -> None:
    """End-to-end: hooks fire, verify settles, batch channel lifecycle, PaymentInfo."""
    router = MockRouter()
    handler = X402Handler(wallet="0x0000000000000000000000000000000000000001", router=router)
    # Temp paths: keep the audit logs out of the real X402_DIR.
    handler.payments_file = tmp_path / "payments.jsonl"
    handler.settlements_file = tmp_path / "settlements.jsonl"

    # ── Hooks ──────────────────────────────────────────────────────────────
    pre_verify_calls: list[dict[str, Any]] = []
    post_verify_calls: list[dict[str, Any]] = []
    pre_settle_calls: list[dict[str, Any]] = []
    post_settle_calls: list[dict[str, Any]] = []

    handler.on_pre_verify(lambda **kw: pre_verify_calls.append(kw))
    handler.on_post_verify(post_verify_calls.append)
    handler.on_pre_settle(lambda **kw: pre_settle_calls.append(kw))
    handler.on_post_settle(post_settle_calls.append)

    # ── Verify ─────────────────────────────────────────────────────────────
    payment_id = "pay_lifecycle_001"
    tx_hash = "0x" + "ab" * 32
    result = asyncio.run(
        handler.verify_payment(
            payment_id,
            tx_hash,
            network="eip155:8453",
            asset="USDC",
            amount_usd=0.01,
            caller_key="0xcaller",
        )
    )
    assert result["verified"] is True
    assert len(router.verify_calls) == 1
    assert router.verify_calls[0][0] == payment_id
    assert len(pre_verify_calls) == 1
    assert pre_verify_calls[0]["payment_id"] == payment_id
    assert len(post_verify_calls) == 1
    assert post_verify_calls[0]["verified"] is True

    # Payment recorded with expiry + caller binding.
    assert handler.is_paid(payment_id, caller_key="0xcaller") is True
    assert handler.is_paid(payment_id, caller_key="0xother") is False

    # ── Settle ─────────────────────────────────────────────────────────────
    settlement = asyncio.run(
        handler.settle_payment(
            payment_id,
            tx_hash,
            network="eip155:8453",
            asset="USDC",
            amount_usd=0.01,
        )
    )
    assert settlement["success"] is True
    assert len(router.settle_calls) == 1
    assert len(pre_settle_calls) == 1
    assert pre_settle_calls[0]["tx_hash"] == tx_hash
    assert len(post_settle_calls) == 1
    assert post_settle_calls[0]["success"] is True

    # Idempotent re-settle returns the cached receipt without a second call.
    settlement2 = asyncio.run(
        handler.settle_payment(payment_id, tx_hash, network="eip155:8453", asset="USDC")
    )
    assert settlement2["success"] is True
    assert len(router.settle_calls) == 1

    # Audit logs written to temp paths.
    assert (tmp_path / "payments.jsonl").exists()
    assert (tmp_path / "settlements.jsonl").exists()

    # ── Batch channel lifecycle ────────────────────────────────────────────
    channel: BatchChannel = create_channel("eip155:8453", "0xdepositor")
    assert channel.status == "open"
    assert get_channel(channel.channel_id) is channel

    claim = claim_channel(channel.channel_id, 10)
    assert claim["success"] is True
    assert claim["remaining"] == channel.max_claims_per_batch - 10

    refund = refund_channel(channel.channel_id)
    assert refund["success"] is True
    assert refund["refunded"] == channel.max_claims_per_batch - 10
    stored = get_channel(channel.channel_id)
    assert stored is not None and stored.status == "refunded"

    # Post-refund claims are rejected.
    assert claim_channel(channel.channel_id, 1)["success"] is False

    # ── PaymentInfo (auth-capture escrow scheme) ───────────────────────────
    now = 1_800_000_000
    info = PaymentInfo(
        payer="0xpayer",
        payee="0x0000000000000000000000000000000000000001",
        amount_usd=0.01,
        asset="USDC",
        network="eip155:8453",
        pre_approval_expiry=now + 3600,
        authorization_expiry=now + 7200,
        refund_expiry=now + 86400,
    )
    assert info.is_expired(ts=now) is False
    assert info.is_expired(ts=now + 7201) is True
    assert info.validate_fee(50) is True  # within 0..100 bps
    assert info.validate_fee(200) is False  # above 1% cap
    assert info.validate_fee(-1) is False
