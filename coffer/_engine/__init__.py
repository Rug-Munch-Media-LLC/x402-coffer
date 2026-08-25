"""x402 domain — spec-canonical payment layer (402 flow, verify/settle).

Re-exports the canonical interface the rest of the fleet imports. ONE
convention everywhere (see ``docs/X402-SPEC-CONTRACT.md`` §2/§5):

- 402 offer  : ``PAYMENT-REQUIRED`` (base64 body) + ``X-Payment-Required``
  + ``X-Payment-Options: x402``; body ``{x402Version:1, accepts:[...]}``.
- replay     : ``X-Payment-ID: <verified id>`` (caller-bound, TTL'd).
- verify     : ``FacilitatorRouter.verify(payment_id, tx_hash, network,
  asset, amount_usd) -> {"verified": bool, "reason", ...}``.
- settle     : ``FacilitatorRouter.settle(...) -> {"success": bool,
  "transaction", ...}``.
- networks   : CAIP-2 ids (``eip155:8453``, ``solana:mainnet``,
  ``tron:mainnet``); accepts carry the asset SYMBOL + ``extra.contract``.
"""

from __future__ import annotations

# 402 offer builder + middleware (imports the service pricing/wallet tables).
from .middleware import (  # noqa: F401  (re-export)
    build_multi_asset_accepts,
    build_payment_required,
)

# Spec wire models (PaymentRequired / accepts[] / settlement receipt /
# verification result) + the canonical network & asset tables.
from .models import (  # noqa: F401  (re-export)
    SUPPORTED_ASSETS,
    SUPPORTED_NETWORKS,
    X402_VERSION,
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
    flatten_supported_assets,
)

# The /v1/x402 HTTP surface (payment challenge, verify, settle, health).
from .router import router  # noqa: F401  (re-export)

# Spec-conformant facilitator surface + pricing + honest verification status.
from .service import (  # noqa: F401  (re-export)
    VERIFICATION_STATUS,
    X402_PRICING,
    FacilitatorRouter,
    asset_to_atomic,
    get_facilitator_router,
    register_facilitator,
    resolve_price_usd,
    validate_client_amount,
)

# Core health registration is guarded: app.core.health may not exist yet in a
# worktree mid-refactor (it is also referenced by app/mcp/server.py). [REVIEW]
try:
    from app.core import health as _health_mod  # type: ignore
    from app.core.health import DomainHealth  # type: ignore

    async def _health_check() -> DomainHealth:
        """x402 health: priced ops + middleware available + facilitator readiness."""
        try:
            _router = get_facilitator_router()
            return DomainHealth(
                name="x402",
                healthy=True,
                details={
                    "priced_operations": len(X402_PRICING),
                    "middleware": "available",
                    "x402_version": X402_VERSION,
                    "verification_status": dict(VERIFICATION_STATUS),
                    "facilitator_mode": _router.readiness().get("mode"),
                    "facilitator_ready": _router.readiness().get("ready"),
                },
            )
        except Exception as e:
            return DomainHealth(name="x402", healthy=False, error=str(e))

    _health_mod.register_health_check("x402", _health_check)
except (ModuleNotFoundError, ImportError):  # pragma: no cover - app.core.health not present yet
    pass

__all__ = [
    "FacilitatorRouter",
    "PaymentFacilitator",
    "PaymentOption",
    "PaymentReceipt",
    "PaymentRequirement",
    "PaymentResponse",
    "SUPPORTED_ASSETS",
    "SUPPORTED_NETWORKS",
    "ToolCatalog",
    "ToolCatalogEntry",
    "ToolPricing",
    "VERIFICATION_STATUS",
    "VerifyResult",
    "X402_PRICING",
    "X402_VERSION",
    "X402Tier",
    "asset_to_atomic",
    "build_multi_asset_accepts",
    "build_payment_required",
    "flatten_supported_assets",
    "get_facilitator_router",
    "register_facilitator",
    "resolve_price_usd",
    "router",
    "validate_client_amount",
]
