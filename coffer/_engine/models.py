"""Pydantic v2 models for the x402 domain — spec-conformant.

Wire shape matches the official x402 specification (see
``docs/x402-official-spec/x402-specification-v1.md`` §5.1/§5.3 and
``transports-v1__http.md``) and the canonical Python reference builder
``x402-py:x402/scheme.py::build_payment_required``:

    {
        "x402Version": 1,
        "accepts": [
            {
                "scheme": "exact",
                "network": "eip155:8453",        # CAIP-2 id, never bare "base"
                "maxAmountRequired": "1000",      # atomic units, STRING
                "resource": "https://host/api/op",
                "description": "Token risk score",
                "mimeType": "application/json",
                "payToAddress": "0x...",
                "asset": "USDC",
                "extra": {"name": "Base USDC", "version": "2", "operation": "..."},
            }
        ],
        # optional multi-asset extension (build_payment_required(multi_asset=True))
        "networks": [...],
        "supported_assets": [...]
    }

``PaymentRequirement.model_dump(by_alias=True, exclude_none=True)`` reproduces
that builder output key-for-key. This module is a thin facade: it keeps the
Pydantic models and re-exports the canonical network/asset tables from
:mod:`app.domain.x402.assets` (``SUPPORTED_ASSETS``, ``SUPPORTED_NETWORKS``,
``CAIP2_NETWORK_IDS``, ``flatten_supported_assets``) so
``from coffer._engine.models import PaymentRequirement, SUPPORTED_ASSETS``
keeps resolving.

Notes for other agents
----------------------
- Construct via snake_case field names (``populate_by_name=True``) or the
  camelCase wire aliases — both work. Serialize with ``by_alias=True``.
- ``network`` fields accept the known legacy bare names ("base", "solana",
  ...) and normalise them to CAIP-2; anything else must already be valid
  CAIP-2 (``namespace:reference``) or construction fails.
- The pre-spec ``PaymentRequest{tool, chain, user_address}`` shape is gone.
  ``PaymentReceipt`` survives only as an alias of :class:`PaymentResponse`
  so legacy imports keep working.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

# Public tables/helpers live in assets.py; re-export here as the canonical
# ``models`` surface so existing ``from coffer._engine.models import ...``
# imports (incl. __init__.py) keep resolving unchanged.
from .assets import (
    _ATOMIC_AMOUNT_RE,
    CAIP2_NETWORK_IDS,
    SUPPORTED_ASSETS,
    SUPPORTED_NETWORKS,
    _require_caip2,
    _require_caip2_or_none,
    flatten_supported_assets,
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

#: Protocol version carried on the wire. The fleet contract
#: (docs/X402-SPEC-CONTRACT.md §2.2) and x402-py ``build_payment_required``
#: both emit version 1. Official spec v2 uses x402Version=2 with a
#: restructured body (``amount``/``payTo`` accept fields, top-level
#: ResourceInfo, ``PAYMENT-SIGNATURE`` client header). The fleet wire
#: contract stays v1 until the gateway migrates.
#: TODO(v2): additive — emit x402Version=2 bodies behind a flag once the
#: gateway + clients move; v1 replay stays ``X-Payment-ID``.
X402_VERSION = 1


class X402Tier(StrEnum):
    """x402 payment tier — maps to scanner tiers."""

    FREE = "free"
    PRO = "pro"
    ELITE = "elite"
    INTERNAL = "internal"


class Scheme(StrEnum):
    """x402 payment schemes (spec §6; ``specs/schemes/exact``, ``upto``)."""

    EXACT = "exact"
    UPTO = "upto"


class PaymentOption(BaseModel):
    """One entry of ``PaymentRequired.accepts[]`` (v1 spec §5.1.2).

    ``maxAmountRequired`` is the amount in ATOMIC token units and is a
    STRING on the wire; ``network`` is a CAIP-2 id. Serialize with
    ``model_dump(by_alias=True)`` to get the wire keys.
    """

    model_config = ConfigDict(str_strip_whitespace=True, populate_by_name=True)

    scheme: Scheme = Field(default=Scheme.EXACT, description="Payment scheme identifier")
    network: str = Field(description="CAIP-2 network id, e.g. 'eip155:8453'")
    max_amount_required: str = Field(
        alias="maxAmountRequired",
        description="Required payment amount in atomic token units (string)",
    )
    resource: str = Field(description="Full URL of the protected resource")
    description: str = Field(description="Human-readable description of the resource")
    mime_type: str = Field(
        default="application/json",
        alias="mimeType",
        description="MIME type of the expected response",
    )
    pay_to_address: str = Field(
        alias="payToAddress",
        description="Server receiving wallet address",
    )
    asset: str = Field(description="Asset symbol (e.g. 'USDC') or contract address")
    extra: dict[str, Any] = Field(
        default_factory=dict,
        description="Scheme-specific extras (name, version, operation, ...)",
    )

    @field_validator("network", mode="before")
    @classmethod
    def _network_caip2(cls, value: Any) -> str:
        return _require_caip2(value)

    @field_validator("max_amount_required")
    @classmethod
    def _atomic_amount(cls, value: str) -> str:
        if not _ATOMIC_AMOUNT_RE.match(value):
            raise ValueError(f"maxAmountRequired must be an atomic-unit integer string, got {value!r}")
        return value


class NetworkInfo(BaseModel):
    """Row of the optional top-level ``networks`` table (SUPPORTED_NETWORKS).

    Mirrors ``x402.scheme.SUPPORTED_NETWORKS`` entries so agents can see
    every network the server accepts.
    """

    model_config = ConfigDict(str_strip_whitespace=True, populate_by_name=True)

    network: str = Field(description="CAIP-2 network id")
    name: str = ""
    family: str = ""
    chain: str = ""

    @field_validator("network", mode="before")
    @classmethod
    def _network_caip2(cls, value: Any) -> str:
        return _require_caip2(value)


class SupportedAsset(BaseModel):
    """Row of the optional top-level ``supported_assets`` table.

    Mirrors ``x402.scheme.flatten_supported_assets()`` entries: one row per
    accepted (network, asset) pair with its verification method.
    """

    model_config = ConfigDict(str_strip_whitespace=True, populate_by_name=True)

    asset: str
    network: str = Field(description="CAIP-2 network id")
    name: str = ""
    contract: str = ""
    decimals: int = 6
    verification: str = Field(
        default="",
        description="Authorization method: eip3009 | permit2 | native | spl | trc20",
    )
    kind: str = ""

    @field_validator("network", mode="before")
    @classmethod
    def _network_caip2(cls, value: Any) -> str:
        return _require_caip2(value)


class PaymentRequirement(BaseModel):
    """The 402 response body (``PaymentRequirementsResponse``, v1 spec §5.1).

    Serialize with ``model_dump(by_alias=True, exclude_none=True)`` — the
    result matches ``x402.scheme.build_payment_required`` key-for-key
    (``networks``/``supported_assets`` appear only in multi-asset mode).
    """

    model_config = ConfigDict(str_strip_whitespace=True, populate_by_name=True)

    x402_version: int = Field(
        default=X402_VERSION,
        alias="x402Version",
        ge=1,
        description="Protocol version identifier",
    )
    error: str | None = Field(
        default=None,
        description="Human-readable reason payment is required",
    )
    accepts: list[PaymentOption] = Field(
        min_length=1,
        description="Acceptable payment methods; never empty",
    )
    networks: list[NetworkInfo] | None = Field(
        default=None,
        description="Multi-asset extension: full supported-networks table",
    )
    supported_assets: list[SupportedAsset] | None = Field(
        default=None,
        description="Multi-asset extension: flattened (network, asset) table",
    )


class PaymentResponse(BaseModel):
    """Settlement receipt (v1 spec §5.3 SettlementResponse + contract §5.2).

    Extends the spec core ``{success, transaction, network, payer}`` with the
    fleet facilitator metadata (``payment_id``, ``facilitator``, ``settled_at``,
    block confirmation detail). ``transaction`` is the broadcast tx hash and
    MUST be non-empty whenever settlement was broadcast.
    """

    model_config = ConfigDict(str_strip_whitespace=True, populate_by_name=True)

    success: bool
    transaction: str = Field(description="Blockchain transaction hash ('' only if nothing was broadcast)")
    network: str = Field(description="CAIP-2 network id")
    payment_id: str = ""
    facilitator: str = ""
    settled_at: str = Field(default="", description="ISO-8601 UTC settlement timestamp")
    payer: str | None = Field(default=None, description="Payer wallet address")
    error_reason: str | None = Field(
        default=None,
        alias="errorReason",
        description="Spec error code when settlement failed",
    )
    block_number: int | None = None
    block_hash: str | None = None
    confirmations: int | None = None

    @field_validator("network", mode="before")
    @classmethod
    def _network_caip2(cls, value: Any) -> str:
        return _require_caip2(value)


#: Backward-compatible alias: the pre-spec ``PaymentReceipt`` name now points
#: at the spec-conformant settlement receipt. Prefer ``PaymentResponse``.
PaymentReceipt = PaymentResponse


class VerifyResult(BaseModel):
    """Facilitator verification result (fleet contract §5.1 ``verify``).

    ``reason`` is always present (empty when verified); ``settlement`` carries
    the settlement detail dict when the facilitator returned one.
    """

    model_config = ConfigDict(str_strip_whitespace=True, populate_by_name=True)

    verified: bool
    reason: str = ""
    settlement: dict[str, Any] | None = None
    facilitator: str = ""
    signer: str | None = None
    amount_atomic: int | None = None
    network: str | None = Field(default=None, description="CAIP-2 network id")
    asset: str | None = None

    @field_validator("network", mode="before")
    @classmethod
    def _network_caip2(cls, value: Any) -> str | None:
        return _require_caip2_or_none(value)


class ToolPricing(BaseModel):
    """Per-tool pricing in x402 (server-authoritative — never trust clients).

    Feeds ``accepts[]`` construction: ``tool`` → ``extra.operation`` and the
    resource path, ``description`` → ``description``, ``mime_type`` →
    ``mimeType``. Atomic conversion of ``price_usd`` belongs to the pricing
    layer (x402-py ``asset_to_atomic``), not to clients.
    """

    model_config = ConfigDict(str_strip_whitespace=True, populate_by_name=True)

    tool: str
    price_usd: float = Field(default=0.0, ge=0)
    price_crypto: dict[str, str] = Field(
        default_factory=dict,
        description="Per-unit crypto amounts: {'sol': '0.01', 'eth': '0.001'}",
    )
    description: str = ""
    mime_type: str = "application/json"
    tier_required: X402Tier = X402Tier.FREE

    def to_payment_option(
        self,
        *,
        network: str,
        asset: str,
        max_amount_required: str,
        pay_to_address: str,
        resource: str,
        extra: dict[str, Any] | None = None,
    ) -> PaymentOption:
        """Build this tool's ``accepts[]`` entry from server-side inputs.

        ``max_amount_required`` must be the atomic-unit STRING computed by the
        pricing layer for (network, asset) — never a client-supplied value.
        """
        merged: dict[str, Any] = {
            "name": asset,
            "version": "2",
            "operation": self.tool,
        }
        merged.update(extra or {})
        # Construct via wire aliases so this type-checks with or without the
        # pydantic mypy plugin (populate_by_name covers snake_case at runtime).
        return PaymentOption(
            scheme=Scheme.EXACT,
            network=network,
            maxAmountRequired=max_amount_required,
            resource=resource,
            description=self.description or self.tool,
            mimeType=self.mime_type,
            payToAddress=pay_to_address,
            asset=asset,
            extra=merged,
        )


class ToolCatalogEntry(BaseModel):
    """A single tool in the x402 catalog.

    Maps into ``accepts[]`` as: ``name`` → ``extra.name``, ``description`` →
    ``description``, ``endpoint`` → ``resource``, ``price_usd`` →
    ``maxAmountRequired`` (after atomic conversion).
    """

    model_config = ConfigDict(str_strip_whitespace=True, populate_by_name=True)

    name: str
    description: str = ""
    category: str = ""
    tier_required: X402Tier = X402Tier.FREE
    price_usd: float = Field(default=0.0, ge=0)
    endpoint: str = ""
    enabled: bool = True


class ToolCatalog(BaseModel):
    """Full x402 tool catalog."""

    model_config = ConfigDict(str_strip_whitespace=True, populate_by_name=True)

    tools: list[ToolCatalogEntry] = Field(default_factory=list)
    total: int = 0
    categories: list[str] = Field(default_factory=list)
    fetched_at: str = ""


class PaymentFacilitator(BaseModel):
    """x402 payment facilitator info (verify/settle endpoint, spec §7).

    ``chains`` should carry CAIP-2 network ids going forward (legacy bare
    names are tolerated until the service layer is migrated).
    """

    model_config = ConfigDict(str_strip_whitespace=True, populate_by_name=True)

    name: str
    url: str
    enabled: bool = True
    chains: list[str] = Field(default_factory=list)
    health: str = "unknown"
