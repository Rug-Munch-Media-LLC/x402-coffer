"""Regression tests for the H3 free-path hardening fix.

``X402Settings.free_paths`` must free ONLY the x402 protocol endpoints a payer
needs before paying (plus liveness/readiness + self-description). The broad
``/v1/x402`` prefix is gone and the revenue/ops surfaces — agent registry
(``/v1/x402/agents``), the ops dashboard (``/dashboard``), and BOTH metrics
endpoints (``/metrics`` + ``/v1/x402/metrics``) — are now behind the payment
gate.
"""

from __future__ import annotations

import os

import httpx
import pytest

EXPECTED_FREE = {
    "/v1/x402/payment",
    "/v1/x402/verify",
    "/v1/x402/settle",
    "/v1/x402/health",
    "/health",
    "/healthz",
    "/readyz",
    "/docs",
    "/openapi",
    "/version",
    "/supported",
}


@pytest.fixture()
def app():  # type: ignore[no-untyped-def]
    os.environ.setdefault("X402_WALLET", "0x742d35Cc6634C0532925a3b844Bc9e7595f2bD18")
    os.environ.setdefault("X402_PAY_TO", "0x742d35Cc6634C0532925a3b844Bc9e7595f2bD18")
    from coffer._engine.config import create_app

    return create_app()


class TestFreePathsNarrowed:
    def test_default_free_paths_are_protocol_only(self) -> None:
        from coffer._engine.config import X402Settings

        free = set(X402Settings().free_paths)
        assert free == EXPECTED_FREE
        # The audit footguns are gone:
        assert "/v1/x402" not in free  # broad prefix freed agents + metrics
        assert "/dashboard" not in free  # revenue dashboard no longer public
        assert "/metrics" not in free  # metrics no longer public

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "path",
        [
            "/v1/x402/agents",  # agent registry + payer ledger
            "/dashboard",  # revenue totals + recent tx hashes
            "/metrics",  # static ops metrics
            "/v1/x402/metrics",  # real ledger/prometheus metrics
        ],
        ids=["agents", "dashboard", "metrics", "x402-metrics"],
    )
    async def test_revenue_and_ops_surfaces_are_payment_gated(self, app, path: str) -> None:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
            r = await c.get(path)
        assert r.status_code == 402, f"{path} must require payment"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "path",
        [
            "/v1/x402/health",
            "/supported",
            "/healthz",
            "/readyz",
            "/docs",
        ],
        ids=["x402-health", "supported", "healthz", "readyz", "docs"],
    )
    async def test_protocol_and_probe_routes_stay_free(self, app, path: str) -> None:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
            r = await c.get(path)
        assert r.status_code != 402, f"{path} must stay reachable pre-payment"

    @pytest.mark.asyncio
    async def test_payment_challenge_route_reachable_pre_payment(self, app) -> None:
        """The challenge endpoint is free (it IS the 402 offer, not a gate)."""
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
            r = await c.get("/v1/x402/payment")
        assert r.status_code in (200, 402)
        body = r.json()
        assert body["x402Version"] in (1, 2)
        assert body["accepts"]
