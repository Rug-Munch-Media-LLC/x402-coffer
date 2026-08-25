"""Tests for settlement observability (ledger + metrics)."""

from __future__ import annotations

from coffer._engine.observability import (
    SettlementLedger,
    get_ledger,
    observe_settle,
    observe_verify,
)


def test_record_verify_counters() -> None:
    led = SettlementLedger()
    led.record_verify(
        payment_id="p1",
        tx_hash="0x1",
        network="eip155:8453",
        asset="USDC",
        amount_atomic=1000,
        verified=True,
        facilitator="stub",
    )
    led.record_verify(
        payment_id="p2",
        tx_hash="0x2",
        network="eip155:8453",
        asset="USDC",
        amount_atomic=0,
        verified=False,
        reason="bad",
    )
    snap = led.snapshot()
    assert snap["counters"]["verifications_verified"] == 1
    assert snap["counters"]["verifications_rejected"] == 1
    assert snap["by_network"]["eip155:8453"]["verifications_verified"] == 1


def test_record_settle_and_amount_total() -> None:
    led = SettlementLedger()
    led.record_settle(
        payment_id="p1",
        tx_hash="0xtx",
        network="solana:mainnet",
        asset="USDC",
        amount_atomic=2500,
        success=True,
    )
    led.record_settle(
        payment_id="p2",
        tx_hash="0xy",
        network="tron:mainnet",
        asset="USDT",
        amount_atomic=999,
        success=False,
        reason="needs_facilitator",
    )
    snap = led.snapshot()
    assert snap["counters"]["settlements_success"] == 1
    assert snap["counters"]["settlements_failed"] == 1
    assert snap["settled_amount_atomic_total"] == 2500


def test_recent_events_filter_and_order() -> None:
    led = SettlementLedger()
    led.record_verify(
        payment_id="a", tx_hash="0xa", network="n", asset="USDC", amount_atomic=1, verified=True
    )
    led.record_settle(
        payment_id="b", tx_hash="0xb", network="n", asset="USDC", amount_atomic=2, success=True
    )
    settles = led.recent_events(kind="settle")
    assert len(settles) == 1 and settles[0]["payment_id"] == "b"
    assert led.recent_events(limit=1).__len__() == 1


def test_bounded_eviction() -> None:
    led = SettlementLedger(max_entries=3)
    for i in range(5):
        led.record_verify(
            payment_id=f"p{i}",
            tx_hash=f"0x{i}",
            network="n",
            asset="USDC",
            amount_atomic=1,
            verified=True,
        )
    assert led.snapshot()["ledger_entries"] == 3


def test_observe_hooks_fail_safe_on_garbage() -> None:
    # Malformed result dicts must never raise.
    observe_verify(
        {"verified": "not-a-bool", "amount_atomic": "junk"}, payment_id="x", tx_hash="0xx"
    )
    observe_settle({}, payment_id="y", tx_hash="0xy")
    assert get_ledger() is get_ledger()


def test_prometheus_format() -> None:
    from coffer._engine.observability import _prometheus_text

    text = _prometheus_text(
        {
            "counters": {
                "verifications_verified": 2,
                "verifications_rejected": 1,
                "settlements_success": 1,
                "settlements_failed": 0,
            },
            "settled_amount_atomic_total": 42,
            "by_network": {},
            "ledger_entries": 3,
            "generated_at": "now",
        }
    )
    assert 'coffer_verifications_total{outcome="verified"} 2' in text
    assert "coffer_settled_amount_atomic_total 42" in text


async def test_metrics_endpoint_mounted() -> None:
    import os

    os.environ.setdefault("X402_WALLET", "0x742d35Cc6634C0532925a3b844Bc9e7595f2bD18")
    os.environ.setdefault("X402_PAY_TO", "0x742d35Cc6634C0532925a3b844Bc9e7595f2bD18")
    import httpx

    from coffer._engine.config import create_app

    app = create_app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get("/v1/x402/metrics")
        assert r.status_code == 200 and "counters" in r.json()
        r = await c.get("/v1/x402/metrics?format=prometheus")
        assert "coffer_verifications_total" in r.text
