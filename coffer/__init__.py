"""Coffer — the self-hosted x402 payment facilitator.

Your HTTP treasury, sovereign and open.

``coffer`` is the public facade of the Coffer product: one import surface
that proxies to the layered implementation without leaking the internal
layout (see ``docs/COFFER-ARCHITECTURE.md`` for the full layering)::

    import coffer

    @coffer.require_payment()
    async def paid_endpoint(request):
        return {"data": "you paid!"}

Layers proxied here (precedence on duplicate names: domain > spec):

- **Core domain** (``app/domain/x402/``) — Coffer's product layer: the
  spec-canonical 402 offer/verify/settle flow, wire models, pricing and
  facilitator routing. Every domain symbol is re-exported at this level.
- **Vendored spec** (``vendor/x402/``) — the vendored x402-py reference
  implementation (``x402Version``, ``X-PAYMENT``, ``accepts``,
  ``PAYMENT-REQUIRED``, ``FacilitatorRouter`` are x402 SPEC terms, not
  Coffer branding). Spec-only primitives (``X402Handler``,
  ``SelfFacilitator``, scheme/network/asset enums, batch + Solana
  verification helpers) are re-exported here; the full vendored module is
  available as :data:`coffer.spec` (its middleware decorator as
  ``coffer.spec.middleware.require_payment``).
- **Extensions** (``app/domain/x402/extensions/``) — optional spec
  extensions (payment identifier, gas sponsoring, ``upto``/batch schemes),
  available as :data:`coffer.extensions`.
- **MCP tool manifest** — see :mod:`coffer.mcp` (typed, described tool
  manifests for agent discovery).

[REVIEW] Spec import path: :mod:`coffer._spec` resolves the vendored spec
and may register it as top-level ``sys.modules["x402"]`` (shadowing the
official Coinbase x402 SDK process-wide) when the venv ``.pth`` activation
is absent. See ``coffer._spec.SPEC_SOURCE`` for which path was taken.
"""

from __future__ import annotations

from typing import Any

# ── Brand ────────────────────────────────────────────────────────────────────
__product__ = "Coffer"
__tagline__ = "Your HTTP treasury, sovereign and open."
__version__ = "5.0.0"

# ── Core domain surface (app/domain/x402) — wins on name collisions ─────────
from coffer._engine import (  # noqa: E402
    SUPPORTED_ASSETS,
    SUPPORTED_NETWORKS,
    VERIFICATION_STATUS,
    X402_PRICING,
    X402_VERSION,
    FacilitatorRouter,
    PaymentFacilitator,
    PaymentOption,
    PaymentReceipt,
    PaymentRequirement,
    PaymentResponse,
    ToolCatalog,
    ToolCatalogEntry,
    ToolPricing,
    VerifyResult,
    X402Tier,
    asset_to_atomic,
    build_multi_asset_accepts,
    build_payment_required,
    extensions,  # noqa: E402,F401  (module alias)
    flatten_supported_assets,
    get_facilitator_router,
    register_facilitator,
    resolve_price_usd,
    validate_client_amount,
)
from coffer._engine import router as x402_router  # noqa: E402  (/v1/x402 surface)
from coffer._engine.middleware import (  # noqa: E402
    HEADER_PAYMENT_REQUIRED,
    HEADER_X_PAYMENT_ID,
    HEADER_X_PAYMENT_OPTIONS,
    HEADER_X_PAYMENT_REQUIRED,
    PAYMENT_VALIDITY_SECONDS,
    PaymentStore,
    X402Middleware,
    caller_key_from_request,
    get_default_store,
    payment_required_body,
    payment_required_headers,
    require_payment,
    set_default_store,
)

# ── Vendored spec surface (vendor/x402) — spec-only names ───────────────────
from coffer import _spec as _spec_resolver  # noqa: E402

spec: Any = _spec_resolver.resolve_spec_module()
SPEC_SOURCE: str = _spec_resolver.SPEC_SOURCE

import importlib as _importlib  # noqa: E402

# Spec middleware is not re-exported by the vendored package root; bind it.
_spec_middleware: Any = _importlib.import_module("x402.middleware")

# Spec primitives with no domain counterpart.
X402Handler: Any = spec.X402Handler
SelfFacilitator: Any = spec.SelfFacilitator
NonceStore: Any = spec.NonceStore
SettleErrorCode: Any = spec.SettleErrorCode
SettleOutcome: Any = spec.SettleOutcome
X402Scheme: Any = spec.X402Scheme
X402Network: Any = spec.X402Network
X402Asset: Any = spec.X402Asset
X402_UNCONFIGURED_WALLET: str = spec.X402_UNCONFIGURED_WALLET
set_secret_resolver: Any = spec.set_secret_resolver
create_batch_payment: Any = spec.create_batch_payment
is_batch_paid: Any = spec.is_batch_paid
verify_batch_payment: Any = spec.verify_batch_payment
verify_solana_payment: Any = spec.verify_solana_payment
SOLANA_USDC_MINT: str = spec.SOLANA_USDC_MINT
DEFAULT_SOLANA_RPC_URL: str = spec.DEFAULT_SOLANA_RPC_URL

# Spec exception raised when no framework adapter can render a 402.
PaymentRequired: Any = _spec_middleware.PaymentRequired

__all__ = [
    # brand / meta
    "__product__",
    "__tagline__",
    "__version__",
    "extensions",
    "spec",
    "SPEC_SOURCE",
    "x402_router",
    # core domain — 402 offer builders
    "build_payment_required",
    "build_multi_asset_accepts",
    "payment_required_body",
    "payment_required_headers",
    # core domain — facilitator routing
    "FacilitatorRouter",
    "get_facilitator_router",
    "register_facilitator",
    # core domain — wire models
    "PaymentRequirement",
    "PaymentOption",
    "PaymentReceipt",
    "PaymentResponse",
    "PaymentFacilitator",
    "VerifyResult",
    # core domain — tables & pricing
    "SUPPORTED_ASSETS",
    "SUPPORTED_NETWORKS",
    "VERIFICATION_STATUS",
    "X402_PRICING",
    "X402_VERSION",
    "asset_to_atomic",
    "flatten_supported_assets",
    "resolve_price_usd",
    "validate_client_amount",
    # core domain — MCP tool catalog models
    "ToolCatalog",
    "ToolCatalogEntry",
    "ToolPricing",
    "X402Tier",
    # core domain — middleware / payment gating
    "require_payment",
    "PaymentStore",
    "X402Middleware",
    "get_default_store",
    "set_default_store",
    "caller_key_from_request",
    "PAYMENT_VALIDITY_SECONDS",
    "HEADER_PAYMENT_REQUIRED",
    "HEADER_X_PAYMENT_ID",
    "HEADER_X_PAYMENT_OPTIONS",
    "HEADER_X_PAYMENT_REQUIRED",
    # vendored spec — spec-only primitives
    "X402Handler",
    "SelfFacilitator",
    "NonceStore",
    "SettleErrorCode",
    "SettleOutcome",
    "X402Scheme",
    "X402Network",
    "X402Asset",
    "X402_UNCONFIGURED_WALLET",
    "set_secret_resolver",
    "create_batch_payment",
    "is_batch_paid",
    "verify_batch_payment",
    "verify_solana_payment",
    "SOLANA_USDC_MINT",
    "DEFAULT_SOLANA_RPC_URL",
    "PaymentRequired",
]
