"""Official x402 spec conformance sweep — the whole checklist in one place.

v1 checklist from docs/X402-SPEC-CONTRACT.md §2; v2 checklist from
docs/x402-official-spec/x402-specification-v2.md §5.1. Driven through the
real ASGI app so headers/body are exactly what a payer sees.
"""

from __future__ import annotations

import base64
import json
import os

import httpx
import pytest


@pytest.fixture()
def app():  # type: ignore[no-untyped-def]
    os.environ.setdefault("X402_WALLET", "0x742d35Cc6634C0532925a3b844Bc9e7595f2bD18")
    os.environ.setdefault("X402_PAY_TO", "0x742d35Cc6634C0532925a3b844Bc9e7595f2bD18")
    from coffer._engine.config import create_app

    return create_app()


@pytest.mark.asyncio
async def test_v1_conformance_sweep(app):  # type: ignore[no-untyped-def]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get("/some-paid-api")
    # ── status + header set (§2.1) ──
    assert r.status_code == 402
    assert r.headers.get("x-payment-required") == "true"
    assert r.headers.get("x-payment-options") == "x402"
    assert "payment-required" in {k.lower() for k in r.headers}
    assert r.headers.get("content-type", "").startswith("application/json")
    # ── body ↔ header agreement ──
    body = r.json()
    decoded = json.loads(base64.b64decode(r.headers["payment-required"]))
    assert decoded == body
    # ── PaymentRequired shape (§2.2) ──
    assert body["x402Version"] == 1
    assert isinstance(body["accepts"], list) and len(body["accepts"]) >= 1
    for opt in body["accepts"]:
        assert opt["scheme"] in ("exact", "upto")
        assert ":" in opt["network"]  # CAIP-2
        assert isinstance(opt["maxAmountRequired"], str) and opt["maxAmountRequired"].isdigit()
        assert opt["resource"].startswith("http")
        assert isinstance(opt["description"], str)
        assert opt["mimeType"] == "application/json"
        assert opt["payToAddress"]
        assert opt["asset"]
        assert "name" in opt["extra"] and "version" in opt["extra"]


@pytest.mark.asyncio
async def test_v2_conformance_sweep(app, monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    monkeypatch.setenv("X402_PROTOCOL_VERSION", "2")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get("/some-paid-api")
    assert r.status_code == 402
    body = r.json()
    decoded = json.loads(base64.b64decode(r.headers["payment-required"]))
    assert decoded == body
    # v2 §5.1 top level
    assert body["x402Version"] == 2
    res = body["resource"]
    assert res["url"].startswith("http")
    assert res["mimeType"] == "application/json"
    assert body["extensions"] == {}
    # v2 accepts entries
    for opt in body["accepts"]:
        assert opt["scheme"] == "exact"
        assert ":" in opt["network"]
        assert isinstance(opt["amount"], str) and opt["amount"].isdigit()
        assert opt["payTo"]
        assert isinstance(opt["maxTimeoutSeconds"], int) and opt["maxTimeoutSeconds"] > 0
        assert opt["asset"]  # contract address or honest symbol fallback
        assert opt["extra"]["name"]


@pytest.mark.asyncio
async def test_free_paths_pass_through_untouched(app):  # type: ignore[no-untyped-def]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        health = await c.get("/v1/x402/health")
        docs = await c.get("/docs")
    assert health.status_code == 200  # protocol routes always free
    assert docs.status_code != 402  # meta routes free


@pytest.mark.asyncio
async def test_payment_challenge_route_is_reachable_and_spec_shaped(app):  # type: ignore[no-untyped-def]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get("/v1/x402/payment")
    assert r.status_code in (200, 402)
    if r.status_code == 402:
        body = r.json()
        assert body["x402Version"] in (1, 2)
        assert body["accepts"]
