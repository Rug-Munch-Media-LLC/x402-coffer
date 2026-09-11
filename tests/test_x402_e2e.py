"""End-to-end tests for the consolidated x402 tree — real app, injected facilitator.

The full round-trip runs against the production ``app.domain.x402`` router +
middleware over ``httpx.AsyncClient(transport=ASGITransport(...))`` with NO
mocking of the 402 -> verify -> replay -> settle path. The only stand-in is the
facilitator (the blockchain/provider seam), injected via the real
``register_facilitator`` / ``set_facilitator_router`` hooks. Pricing, the
replay-nonce store, the caller-bound payment store and the HTTP surface are all
un-mocked production code.

Behaviours asserted (per docs/x402-official-spec/transports-v2__http.md):
unpaid GET -> 402 (PAYMENT-REQUIRED base64 == body, accepts[] valid), pay ->
verify 200, replay with X-Payment-ID -> 200, same id different caller -> 402,
expired id -> 402, nonce reuse (same tx_hash) -> 410, underpayment -> rejected,
overpayment -> 200, settle -> success.
"""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from conftest import (
    ASSET,
    CAIP2_BASE,
    REQUIRED_ATOMIC,
    SCAFFOLDED_NETWORK,
    SERVICE_PRICE_USD,
    X402E2EHarness,
)

from coffer._engine.middleware import get_default_store

SENDER_IP = "1.1.1.1"
OTHER_IP = "2.2.2.2"


def _decode(raw: str) -> dict[str, Any]:
    return json.loads(base64.b64decode(raw).decode())


async def _verify(
    client: httpx.AsyncClient,
    *,
    tx_hash: str,
    payment_id: str,
    amount_usd: float = SERVICE_PRICE_USD,
    network: str = CAIP2_BASE,
    asset: str = ASSET,
) -> httpx.Response:
    """POST the production /v1/x402/verify with an ''on-chain'' payment proof."""
    return await client.post(
        "/v1/x402/verify",
        json={
            "payment_id": payment_id,
            "tx_hash": tx_hash,
            "network": network,
            "asset": asset,
            "amount_usd": amount_usd,
        },
    )


async def _pay(
    e2e: X402E2EHarness,
    client: httpx.AsyncClient,
    *,
    tx_hash: str,
    payment_id: str,
    paid_atomic: int,
    amount_usd: float = SERVICE_PRICE_USD,
    network: str = CAIP2_BASE,
    asset: str = ASSET,
) -> httpx.Response:
    """Simulate paying on-chain, then POST the real verify endpoint."""
    e2e.facilitator.record_payment(tx_hash, amount_atomic=paid_atomic)
    return await _verify(
        client,
        tx_hash=tx_hash,
        payment_id=payment_id,
        amount_usd=amount_usd,
        network=network,
        asset=asset,
    )


def _pid(tag: str) -> str:
    """A payment id that passes 16-128 [a-zA-Z0-9_-] validation if needed."""
    return f"pay_{tag}_{'0' * 16}"


# ── §2.1 — the 402 offer ────────────────────────────────────────────────────


async def test_unpaid_get_402_with_spec_headers(e2e: X402E2EHarness) -> None:
    async with e2e.caller(SENDER_IP) as client:
        r = await client.get("/v1/protected")
    assert r.status_code == 402
    assert r.headers.get("X-Payment-Required") == "true"
    assert r.headers.get("X-Payment-Options") == "x402"
    assert r.headers.get("content-type", "").startswith("application/json")

    raw = r.headers.get("PAYMENT-REQUIRED", "")
    assert raw, "PAYMENT-REQUIRED header must be present"
    decoded = _decode(raw)
    assert decoded.get("x402Version") == 1
    assert r.json() == decoded  # header base64 must decode to the SAME body

    accepts = decoded["accepts"]
    assert accepts, "accepts[] must be non-empty"
    for entry in accepts:
        assert entry["scheme"] in ("exact", "upto")
        assert ":" in entry["network"]  # CAIP-2 id
        assert isinstance(entry["maxAmountRequired"], str) and entry["maxAmountRequired"].isdigit()
        assert entry["payToAddress"]
        assert entry["asset"]


# ── §2.3 / §4 / §5 — the real round-trip ────────────────────────────────────


async def test_full_roundtrip_unpaid_pay_replay_settle(e2e: X402E2EHarness) -> None:
    tx = "0xtx_roundtrip"
    pid = _pid("roundtrip")
    async with e2e.caller(SENDER_IP) as client:
        # 1. unpaid GET -> 402
        r = await client.get("/v1/protected")
        assert r.status_code == 402

        # 2. pay via test facilitator + real verify endpoint -> 200
        r = await _pay(e2e, client, tx_hash=tx, payment_id=pid, paid_atomic=REQUIRED_ATOMIC)
        assert r.status_code == 200, r.json()
        assert r.json()["verified"] is True
        assert r.json()["replay"]["value"] == pid

        # 3. replay with X-Payment-ID -> 200 (middleware passes)
        r = await client.get("/v1/protected", headers={"X-Payment-ID": pid})
        assert r.status_code == 200
        assert r.json() == {"data": "ok"}

        # 4. settle -> success
        r = await client.post(
            "/v1/x402/settle",
            json={
                "payment_id": pid,
                "tx_hash": tx,
                "network": CAIP2_BASE,
                "asset": ASSET,
                "amount_usd": SERVICE_PRICE_USD,
            },
        )
        assert r.status_code == 200
        assert r.json()["success"] is True


async def test_replay_same_id_from_different_caller_rejected(e2e: X402E2EHarness) -> None:
    tx = "0xtx_alice"
    pid = _pid("alice")
    async with e2e.caller(SENDER_IP) as alice:
        r = await _pay(e2e, alice, tx_hash=tx, payment_id=pid, paid_atomic=REQUIRED_ATOMIC)
        assert r.status_code == 200
    # Same id, DIFFERENT caller -> 402 (payment is caller-bound).
    async with e2e.caller(OTHER_IP) as bob:
        r = await bob.get("/v1/protected", headers={"X-Payment-ID": pid})
    assert r.status_code == 402
    assert r.json().get("x402Version") == 1


async def test_expired_payment_id_rejected(e2e: X402E2EHarness) -> None:
    store = get_default_store()
    pid = _pid("expired")
    store.record(
        payment_id=pid,
        caller_key=SENDER_IP,
        tx_hash="0xexp",
        network=CAIP2_BASE,
        asset=ASSET,
        amount_usd=SERVICE_PRICE_USD,
        verified=True,
    )
    store._records[pid]["expires_at"] = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    async with e2e.caller(SENDER_IP) as client:
        r = await client.get("/v1/protected", headers={"X-Payment-ID": pid})
    assert r.status_code == 402  # expired -> must pay again


async def test_nonce_reuse_same_tx_hash_returns_410(e2e: X402E2EHarness) -> None:
    tx = "0xtx_nonce"
    async with e2e.caller(SENDER_IP) as client:
        first = await _pay(e2e, client, tx_hash=tx, payment_id=_pid("nonce1"), paid_atomic=REQUIRED_ATOMIC)
        assert first.status_code == 200
        # Replay the SAME tx_hash -> 410 Gone (replay-nonce store burned it).
        second = await _pay(e2e, client, tx_hash=tx, payment_id=_pid("nonce2"), paid_atomic=REQUIRED_ATOMIC)
    assert second.status_code == 410
    assert "replay" in second.json().get("error", "").lower()


async def test_underpayment_rejected(e2e: X402E2EHarness) -> None:
    tx = "0xtx_under"
    async with e2e.caller(SENDER_IP) as client:
        # Paid 1 atomic less than the server-required 1000 -> rejected.
        r = await _pay(e2e, client, tx_hash=tx, payment_id=_pid("under"), paid_atomic=REQUIRED_ATOMIC - 1)
    assert r.status_code == 402
    assert "underpayment" in r.json().get("error", "").lower()


async def test_overpayment_accepted(e2e: X402E2EHarness) -> None:
    tx = "0xtx_over"
    pid = _pid("over")
    async with e2e.caller(SENDER_IP) as client:
        r = await _pay(e2e, client, tx_hash=tx, payment_id=pid, paid_atomic=REQUIRED_ATOMIC * 5)
        assert r.status_code == 200
        assert r.json()["verified"] is True
        r = await client.get("/v1/protected", headers={"X-Payment-ID": pid})
    assert r.status_code == 200


# ── §5.3 / §6 — scaffolded TRON + honest health ─────────────────────────────


async def test_tron_live_verifier_rejects_fake_tx(e2e: X402E2EHarness) -> None:
    """TRON is now live — a fake tx_hash is rejected by the TRON RPC verifier."""
    async with e2e.caller(SENDER_IP) as client:
        r = await _pay(
            e2e,
            client,
            tx_hash="0xtx_tron",
            payment_id=_pid("tron"),
            paid_atomic=REQUIRED_ATOMIC * 10,
            network=SCAFFOLDED_NETWORK,
            asset="USDT",
        )
    # TRON is live but the fake tx_hash won't be found on-chain → rejected.
    assert r.status_code == 402
    assert r.json().get("verified") is False or "error" in r.json()


async def test_health_exposes_honest_verification_status(e2e: X402E2EHarness) -> None:
    async with e2e.caller(SENDER_IP) as client:
        r = await client.get("/v1/x402/health")
    assert r.status_code == 200
    body = r.json()
    status = body["verification_status"]
    # All three chain families are now live (TRON TRC-20 verifier implemented).
    assert status.get("evm") == "live"
    assert status.get("solana") == "live"
    assert status.get("tron") == "live"
