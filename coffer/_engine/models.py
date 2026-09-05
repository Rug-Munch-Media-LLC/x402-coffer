"""Pydantic v2 models for the x402 domain — thin re-export facade.

Wire shape matches the official x402 specification (see
``docs/x402-official-spec/x402-specification-v1.md`` §5.1/§5.3 and
``transports-v1__http.md``) and the canonical Python reference builder
``x402-py:x402/scheme.py::build_payment_required``.

The concrete models live in focused modules:

- :mod:`.models_payment` — payment request/requirement models
  (``PaymentOption``, ``PaymentRequirement``, tool pricing/catalog,
  facilitator info, ``X402_VERSION``).
- :mod:`.models_settlement` — settlement/verification response models
  (``PaymentResponse``/``PaymentReceipt``, ``VerifyResult``).
- :mod:`.models_response` — multi-asset catalog row models
  (``NetworkInfo``, ``SupportedAsset``).

This module keeps the canonical ``models`` surface so
``from coffer._engine.models import PaymentRequirement, SUPPORTED_ASSETS``
keeps resolving.
"""

from __future__ import annotations

# Public tables/helpers live in assets.py; re-export here as the canonical
# ``models`` surface so existing ``from coffer._engine.models import ...``
# imports (incl. __init__.py) keep resolving unchanged.
from .assets import (  # noqa: F401
    _ATOMIC_AMOUNT_RE,
    CAIP2_NETWORK_IDS,
    SUPPORTED_ASSETS,
    SUPPORTED_NETWORKS,
    _require_caip2,
    _require_caip2_or_none,
    flatten_supported_assets,
)
from .models_payment import (
    X402_VERSION,
    PaymentFacilitator,
    PaymentOption,
    PaymentRequirement,
    Scheme,
    ToolCatalog,
    ToolCatalogEntry,
    ToolPricing,
    X402Tier,
)
from .models_response import (
    NetworkInfo,
    SupportedAsset,
)
from .models_settlement import (
    PaymentReceipt,
    PaymentResponse,
    VerifyResult,
)

__all__ = [
    "CAIP2_NETWORK_IDS",
    "SUPPORTED_ASSETS",
    "SUPPORTED_NETWORKS",
    "X402_VERSION",
    "NetworkInfo",
    "PaymentFacilitator",
    "PaymentOption",
    "PaymentReceipt",
    "PaymentRequirement",
    "PaymentResponse",
    "Scheme",
    "SupportedAsset",
    "ToolCatalog",
    "ToolCatalogEntry",
    "ToolPricing",
    "VerifyResult",
    "X402Tier",
    "flatten_supported_assets",
]
