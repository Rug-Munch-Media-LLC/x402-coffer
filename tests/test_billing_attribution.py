"""Regression tests for the H2 billing-IDOR fix.

``/v1/billing/history`` and ``/v1/billing/balance`` must attribute EXCLUSIVELY
to the verified payment's recorded ``caller_key`` — the same identity the
payment middleware binds ``X-Payment-ID`` replays to. A client-supplied
``X-User-Id`` header is spoofable and must NEVER select whose ledger is
returned (previously any paid caller could read any other customer's
settlement history + balance by setting ``X-User-Id: <victim>``).
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

CALLER_IP = "203.0.113.7"
VICTIM_IP = "198.51.100.9"


def _write_ledgers(tmp_path: Any) -> None:
    """Seed payments.jsonl + settlements.jsonl for two callers."""
    payments = [
        {
            "payment_id": "p-own",
            "tx_hash": "0xown",
            "caller_key": CALLER_IP,
            "verified": True,
            "amount_usd": 0.10,
            "network": "eip155:8453",
            "asset": "USDC",
        },
        {
            "payment_id": "p-victim",
            "tx_hash": "0xvictim",
            "caller_key": VICTIM_IP,
            "verified": True,
            "amount_usd": 9.99,
            "network": "eip155:8453",
            "asset": "USDC",
        },
    ]
    settlements = [
        {
            "payment_id": "p-own",
            "transaction": "0xs-own",
            "success": True,
            "amount_usd": 0.05,
            "network": "eip155:8453",
            "settled_at": "2026-09-06T10:00:00Z",
        },
        {
            "payment_id": "p-victim",
            "transaction": "0xs-victim",
            "success": True,
            "amount_usd": 5.00,
            "network": "eip155:8453",
            "settled_at": "2026-09-06T11:00:00Z",
        },
    ]
    (tmp_path / "payments.jsonl").write_text("\n".join(json.dumps(p) for p in payments) + "\n")
    (tmp_path / "settlements.jsonl").write_text("\n".join(json.dumps(s) for s in settlements) + "\n")


def _client_for(app: FastAPI, ip: str) -> httpx.AsyncClient:
    async def _scoped(scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") == "http":
            scope["client"] = (ip, 9999)  # caller identity lives on the IP
        await app(scope, receive, send)

    return httpx.AsyncClient(transport=httpx.ASGITransport(app=_scoped), base_url="http://testserver")


@pytest.fixture()
def billing_app(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> FastAPI:
    """FastAPI app exposing ONLY the billing router over a temp ledger."""
    monkeypatch.setattr("coffer._engine.billing.X402_DIR", tmp_path)
    _write_ledgers(tmp_path)
    from coffer._engine.billing import router as billing_router

    app = FastAPI()
    app.include_router(billing_router)
    return app


class TestBillingAttributionIgnoresXUserId:
    @pytest.mark.asyncio
    async def test_history_spoofed_x_user_id_does_not_read_victim(self, billing_app: FastAPI) -> None:
        async with _client_for(billing_app, CALLER_IP) as c:
            # Attacker sets X-User-Id to the victim's caller key — must be ignored.
            r = await c.get("/v1/billing/history", headers={"X-User-Id": VICTIM_IP})
        assert r.status_code == 200
        body = r.json()
        assert body["user_id"] == CALLER_IP  # attributed to the payment's caller
        ids = [t["id"] for t in body["transactions"]]
        assert "p-own" in ids
        assert "p-victim" not in ids  # victim's settlement is invisible
        assert body["total_spent"] == 0.05  # only the caller's own settlement

    @pytest.mark.asyncio
    async def test_balance_spoofed_x_user_id_does_not_read_victim(self, billing_app: FastAPI) -> None:
        async with _client_for(billing_app, CALLER_IP) as c:
            r = await c.get("/v1/billing/balance", headers={"X-User-Id": VICTIM_IP})
        assert r.status_code == 200
        body = r.json()
        assert body["user_id"] == CALLER_IP
        assert body["balance_usd"] == 0.05  # 0.10 credited - 0.05 spent (own only)
        # The victim's $9.99 credit never appears.
        assert round(body["balance_by_chain"].get("evm", 0.0), 4) == 0.10

    @pytest.mark.asyncio
    async def test_no_header_still_attributes_to_caller_key(self, billing_app: FastAPI) -> None:
        async with _client_for(billing_app, CALLER_IP) as c:
            r = await c.get("/v1/billing/history")
        assert r.status_code == 200
        assert r.json()["user_id"] == CALLER_IP

    @pytest.mark.asyncio
    async def test_history_reads_only_own_payments(self, billing_app: FastAPI) -> None:
        """A victim caller only ever sees their own rows, never the attacker's."""
        async with _client_for(billing_app, VICTIM_IP) as c:
            r = await c.get("/v1/billing/history", headers={"X-User-Id": CALLER_IP})
        body = r.json()
        assert body["user_id"] == VICTIM_IP
        ids = [t["id"] for t in body["transactions"]]
        assert "p-victim" in ids
        assert "p-own" not in ids
