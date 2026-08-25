"""Tests for the coffer_client SDK — behavior-based, no real chain."""

from __future__ import annotations

import json

import httpx
import pytest

from coffer_client import CofferClient

PAY_TO = "0x742d35Cc6634C0532925a3b844Bc9e7595f2bD18"


def _402_response() -> httpx.Response:
    body = {
        "x402Version": 1,
        "accepts": [
            {
                "scheme": "exact",
                "network": "eip155:8453",
                "asset": "USDC",
                "maxAmountRequired": "1000",
                "payToAddress": PAY_TO,
                "resource": "https://api.test/paid",
            }
        ],
    }
    return httpx.Response(402, json=body)


def _handler(calls: list[str]):
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(f"{request.method} {request.url.path}")
        if request.headers.get("X-Payment-ID"):
            return httpx.Response(200, json={"ok": True, "paid": True})
        if request.url.path == "/v1/x402/verify":
            return httpx.Response(200, json={"verified": True, "payment_id": "pid-1"})
        return _402_response()

    return handler


@pytest.mark.asyncio
async def test_auto_pay_flow_402_verify_replay() -> None:
    calls: list[str] = []
    sender_calls: list[tuple[str, int]] = []

    async def fake_sender(pay_to: str, amount_atomic: int, network: str, asset: str) -> str:
        sender_calls.append((pay_to, amount_atomic))
        assert pay_to == PAY_TO and amount_atomic == 1000
        return "0x" + "ab" * 32

    transport = httpx.MockTransport(_handler(calls))
    client = CofferClient(
        wallet_key="0x" + "22" * 32,
        payment_sender=fake_sender,
    )
    client._http = httpx.AsyncClient(transport=transport)
    resp = await client.get("https://api.test/paid")
    await client.aclose()

    assert resp.status_code == 200 and resp.json()["paid"] is True
    assert ("POST /v1/x402/verify") in calls
    assert len(sender_calls) == 1


@pytest.mark.asyncio
async def test_dry_run_stops_before_broadcast() -> None:
    calls: list[str] = []
    sent: list[int] = []

    async def fake_sender(pay_to: str, amount_atomic: int, network: str, asset: str) -> str:
        sent.append(amount_atomic)
        return "0xtx"

    transport = httpx.MockTransport(_handler(calls))
    client = CofferClient(wallet_key="0x" + "22" * 32, dry_run=True, payment_sender=fake_sender)
    client._http = httpx.AsyncClient(transport=transport)
    resp = await client.get("https://api.test/paid")
    await client.aclose()
    assert sent == []  # nothing broadcast
    assert resp.status_code == 402  # original 402 returned with decision note
    assert resp.headers.get("x-coffer-decision") == "dry-run"


@pytest.mark.asyncio
async def test_spending_ceiling_refuses_overpriced_offer() -> None:
    calls: list[str] = []

    async def fake_sender(pay_to: str, amount_atomic: int, network: str, asset: str) -> str:
        raise AssertionError("must not broadcast over ceiling")

    transport = httpx.MockTransport(_handler(calls))
    client = CofferClient(
        wallet_key="0x" + "22" * 32, max_payment_usd_atomic=999, payment_sender=fake_sender
    )
    client._http = httpx.AsyncClient(transport=transport)
    resp = await client.get("https://api.test/paid")
    await client.aclose()
    assert resp.status_code == 402
    assert resp.headers.get("x-coffer-decision") == "over spending ceiling"


@pytest.mark.asyncio
async def test_no_wallet_returns_402_untouched() -> None:
    calls: list[str] = []
    transport = httpx.MockTransport(_handler(calls))
    client = CofferClient()  # no wallet
    client._http = httpx.AsyncClient(transport=transport)
    resp = await client.get("https://api.test/paid")
    await client.aclose()
    assert resp.status_code == 402
    assert resp.headers.get("x-coffer-decision") == "no wallet configured"


@pytest.mark.asyncio
async def test_unmatched_network_asset_not_paid() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/v1/x402"):
            return httpx.Response(200, json={"verified": True})
        body = json.loads(json.dumps(_402_response().json()))
        body["accepts"][0]["network"] = "tron:mainnet"
        return httpx.Response(402, json=body)

    client = CofferClient(wallet_key="0x" + "22" * 32)
    client._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    resp = await client.get("https://api.test/paid")
    await client.aclose()
    assert resp.status_code == 402  # option for our network absent -> untouched


@pytest.mark.asyncio
async def test_verify_failure_does_not_replay() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/x402/verify":
            return httpx.Response(200, json={"verified": False, "reason": "needs_facilitator"})
        return _402_response()

    client = CofferClient(
        wallet_key="0x" + "22" * 32, payment_sender=lambda a, b, c, d: _fake_ok(a, b, c, d)
    )
    client._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    resp = await client.get("https://api.test/paid")
    await client.aclose()
    assert resp.status_code == 402


async def _fake_ok(pay_to: str, amount_atomic: int, network: str, asset: str) -> str:
    return "0x" + "cd" * 32
