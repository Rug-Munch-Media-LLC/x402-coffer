"""Shared harness for the hardened Coffer x402 end-to-end suite.

This builds a REAL FastAPI app around the system under test — the repo's own
``app.domain.x402`` router + middleware + extensions — and exercises the full
round-trip over ``httpx.AsyncClient(transport=ASGITransport(app=...))``:

    unpaid GET -> 402 -> client "pays" (test facilitator) -> replay 200 ->
    caller/expiry/nonce/under/over-payment -> settle.

The ONLY thing replaced is the facilitator, which is exactly where a live
blockchain/provider interaction would happen. The test facilitator is injected
through the real seams ``set_facilitator_router`` + ``router.register_facilitator``
so the 402->verify->replay->settle path, pricing, replay-nonce store and the
payment store all run un-mocked production code.

Namespace/import hardening:
- The repo's ``app`` (PEP 420 namespace package) can be shadowed by ``/root/app``
  (another ``app`` package on this machine) when the interpreter's cwd sits on
  ``sys.path``. We insert the repo root AHEAD of cwd so ``app.domain.x402``
  resolves to Coffer's tree, and the vendored ``vendor/x402`` ahead of cwd so
  ``import x402`` also resolves locally (an existing ``.pth`` may or may not be
  installed — we don't depend on it).
"""

from __future__ import annotations

import os
import sys
from typing import Any

# ── Resolve THIS repo's namespaces ahead of cwd (/root/app shadowing, vendor) ──
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_VENDOR = os.path.join(_REPO_ROOT, "vendor")


def _strip_app_shadows() -> None:
    """Drop sys.path entries carrying a REGULAR ``app`` package that shadows ours.

    The repo's ``app`` is a PEP 420 namespace package (no ``__init__.py``) and so
    loses to /root/app (a regular package with ``__init__.py``) no matter where it
    sits on sys.path. Remove every entry (incl. cwd) that holds a regular ``app``
    other than this repo's root.
    """

    def _is_regular_app(base: str) -> bool:
        return bool(base) and os.path.isfile(os.path.join(base, "app", "__init__.py"))

    for _entry in list(sys.path):
        _base = _entry or os.getcwd()
        if _base != _REPO_ROOT and _is_regular_app(_base):
            try:
                sys.path.remove(_entry)
            except ValueError:
                pass


_strip_app_shadows()
for _p in (_VENDOR, _REPO_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# ── Canonical test wallet env (audit #57) ──────────────────────────────────
# The accepts/offer surface and readiness signals read X402_WALLET at module
# import time. Set the canonical fallback here (before any coffer import) so
# the whole suite runs with a configured receiving wallet. setdefault keeps any
# explicitly-provided env intact. Per-network X402_WALLET_* are intentionally
# NOT set here so wallet-config tests (test_wallets.py) keep a clean env.
os.environ.setdefault("X402_WALLET", "0x742d35Cc6634C0532925a3b844Bc9e7595f2bD18")

import httpx
import pytest
from fastapi import FastAPI

from coffer._engine.middleware import (
    X402Middleware,
    get_default_store,
    set_default_store,
)
from coffer._engine.router import router as x402_router
from coffer._engine.service import (
    FacilitatorRouter,
    asset_to_atomic,
    set_facilitator_router,
)

# Server-authoritative price for the gated operation (atomic quoted off this).
# MUST equal the default X402_PRICING "api_access" price_usd (0.01): the wire
# /v1/x402/verify endpoint now rejects any client-asserted amount below the
# operation's server price (C1), so honest e2e payments use the real price.
SERVICE_OPERATION = "api_access"
SERVICE_PRICE_USD = 0.01  # -> 10000 atomic USDC on eip155:8453 (6 decimals)

CAIP2_BASE = "eip155:8453"
ASSET = "USDC"

# Networks with a live on-chain verifier are exercised via the stub facilitator;
# TRON has no verifier wired and stays scaffolded (never verified:true).
LIVE_FAMILY = "evm"
SCAFFOLDED_NETWORK = "tron:mainnet"

REQUIRED_ATOMIC = asset_to_atomic(SERVICE_PRICE_USD, ASSET, CAIP2_BASE)


class TestFacilitator:
    """Deterministic stand-in for the facilitator.verify/settle contract.

    NOT a mock of the core flow: it impersonates the blockchain/provider layer
    that sits behind the ``register_facilitator`` seam. ``record_payment`` tells
    the "chain" how much a tx actually paid; ``verify`` then compares that
    against the server-required atomic amount and never fabricates a settlement.
    """

    def __init__(self) -> None:
        self._paid: dict[str, dict[str, Any]] = {}
        self.settled_ids: list[str] = []

    def record_payment(self, tx_hash: str, *, amount_atomic: int) -> None:
        """Simulate an on-chain transfer: tx ``tx_hash`` paid ``amount_atomic``."""
        self._paid[tx_hash] = {"amount_atomic": int(amount_atomic)}

    async def verify(self, payment: dict[str, Any]) -> Any:
        """Decide verified per the on-chain simulation + server pricing."""
        from coffer._engine.models import VerifyResult

        tx_hash = str(payment.get("tx_hash") or "")
        network = str(payment.get("network") or "")
        asset = str(payment.get("asset") or ASSET).upper()

        # TRON stays scaffolded — never claimed verified. [REVIEW]
        if network == SCAFFOLDED_NETWORK:
            return VerifyResult(
                verified=False, reason="needs_facilitator:scaffold", network=network, asset=asset
            )

        try:
            amount_usd = float(payment.get("amount_usd") or 0.0)
            required = asset_to_atomic(amount_usd, asset, network)
        except ValueError as e:
            return VerifyResult(verified=False, reason=str(e), network=network, asset=asset)

        paid = int(self._paid.get(tx_hash, {}).get("amount_atomic", 0))
        if paid == 0:
            return VerifyResult(
                verified=False, reason="no on-chain payment recorded", network=network, asset=asset
            )
        if paid < required:
            return VerifyResult(
                verified=False,
                reason=f"underpayment: paid {paid} < required {required} atomic {asset}",
                network=network,
                asset=asset,
            )
        return VerifyResult(verified=True, reason="", amount_atomic=paid, network=network, asset=asset)

    async def settle(self, payment: dict[str, Any]) -> str | None:
        """Return a broadcast tx hash, or None when not settlable."""
        tx_hash = str(payment.get("tx_hash") or "")
        self.settled_ids.append(tx_hash)
        return f"0xsettled_{tx_hash}" if tx_hash else None


class X402E2EHarness:
    """A REAL FastAPI app (repo router + middleware) + injected facilitator."""

    def __init__(self, app: FastAPI, router: FacilitatorRouter, facilitator: TestFacilitator) -> None:
        self.app = app
        self.router = router
        self.facilitator = facilitator

    def _client_for(self, ip: str) -> httpx.AsyncClient:
        async def _scoped(scope: dict[str, Any], receive: Any, send: Any) -> None:
            if scope.get("type") == "http":
                scope["client"] = (ip, 9999)  # caller identity lives on the IP
            await self.app(scope, receive, send)

        return httpx.AsyncClient(transport=httpx.ASGITransport(app=_scoped), base_url="http://testserver")

    def caller(self, ip: str = "1.1.1.1") -> httpx.AsyncClient:
        return self._client_for(ip)


def _build_app() -> tuple[FastAPI, FacilitatorRouter, TestFacilitator]:
    """Assemble the consolidated real app under test + its test facilitator."""
    set_default_store(None)  # fresh payment store (rebuilt on demand)
    router = FacilitatorRouter()
    facilitator = TestFacilitator()
    # Inject the test facilitator for the live EVM family via the router seam.
    router.register_facilitator(LIVE_FAMILY, facilitator)
    set_facilitator_router(router)

    app = FastAPI()
    app.include_router(x402_router)  # real /v1/x402/* endpoints
    # Real middleware gating everything outside /v1/x402 + /health.
    app.add_middleware(
        X402Middleware,
        free_paths={"/v1/x402", "/health"},
    )

    @app.get("/v1/protected")
    async def protected() -> dict[str, str]:
        return {"data": "ok"}

    return app, router, facilitator


@pytest.fixture
async def e2e() -> X402E2EHarness:
    """Fresh consolidated app per test; global router/store reset on teardown."""
    app, router, facilitator = _build_app()
    harness = X402E2EHarness(app=app, router=router, facilitator=facilitator)
    try:
        yield harness
    finally:
        # Restore process-global singletons so no state leaks across tests.
        set_facilitator_router(None)
        set_default_store(None)


@pytest.fixture
def paid_store() -> Any:
    """Accessor for the process-wide verified-payment store (replay/expiry)."""
    return get_default_store()
