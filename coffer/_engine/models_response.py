"""Multi-asset catalog row models for the x402 domain.

Rows of the optional top-level ``networks`` / ``supported_assets`` tables
emitted by ``build_payment_required(multi_asset=True)`` (v1 spec §5.1
multi-asset extension). Canonical tables live in :mod:`.assets`.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .assets import _require_caip2


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
