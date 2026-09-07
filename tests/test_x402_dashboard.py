"""Tests for the ops dashboard.

The dashboard renders revenue totals and recent settlement tx hashes, so per
audit H3 it now sits BEHIND the payment gate (it was previously on the free
path). These tests assert the gate: unpaid requests get 402, and a caller
holding a server-verified payment (positive bound amount) reaches the HTML.
"""

from __future__ import annotations

import os
import uuid

import httpx
import pytest

from coffer._engine.middleware import HEADER_X_PAYMENT_ID, get_default_store


def _paid_headers() -> dict[str, str]:
    """Record a verified payment on the shared store and return a replay header."""
    payment_id = f"pay_dash_{uuid.uuid4().hex}"
    get_default_store().record(
        payment_id=payment_id,
        caller_key="",
        tx_hash=f"0x{payment_id}",
        network="eip155:8453",
        asset="USDC",
        amount_usd=0.01,
        verified=True,
    )
    return {HEADER_X_PAYMENT_ID: payment_id}


@pytest.fixture()
def app():  # type: ignore[no-untyped-def]
    os.environ.setdefault("X402_WALLET", "0x742d35Cc6634C0532925a3b844Bc9e7595f2bD18")
    os.environ.setdefault("X402_PAY_TO", "0x742d35Cc6634C0532925a3b844Bc9e7595f2bD18")
    from coffer._engine.config import create_app

    return create_app()


@pytest.mark.asyncio
async def test_dashboard_is_payment_gated(app):  # type: ignore[no-untyped-def]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get("/dashboard")
    assert r.status_code == 402  # revenue surface is no longer free


@pytest.mark.asyncio
async def test_dashboard_serves_html_to_paid_caller(app):  # type: ignore[no-untyped-def]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get("/dashboard", headers=_paid_headers())
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert "x402 Coffer" in r.text
    assert "/v1/x402/metrics" in r.text  # wired to live data


@pytest.mark.asyncio
async def test_metrics_snapshot_includes_recent_settlements(app):  # type: ignore[no-untyped-def]
    from coffer._engine.observability import get_ledger

    get_ledger().record_settle(
        payment_id="d1",
        tx_hash="0xd1",
        network="eip155:8453",
        asset="USDC",
        amount_atomic=777,
        success=True,
    )
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get("/v1/x402/metrics", headers=_paid_headers())
    assert r.status_code == 200
    body = r.json()
    assert isinstance(body.get("recent_settlements"), list)
    assert any(e["payment_id"] == "d1" for e in body["recent_settlements"])


@pytest.mark.asyncio
async def test_dashboard_is_read_only(app):  # type: ignore[no-untyped-def]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        r = await c.post("/dashboard", headers=_paid_headers())
    assert r.status_code == 405  # GET-only surface
