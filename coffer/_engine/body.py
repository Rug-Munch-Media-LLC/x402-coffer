"""Spec-conformant 402 offer body + header builders for the x402 domain.

Builds the ``{x402Version:1, accepts:[...]}`` wire body (and the optional
multi-asset ``networks``/``supported_assets`` extension) plus the canonical
402 header set. The spec details are frozen here:

- ``maxAmountRequired`` is an atomic-unit integer STRING (never a float).
- ``network`` is a CAIP-2 id (never a bare "base"/"solana").
- ``asset`` carries the SYMBOL with the contract address in ``extra.contract``.
- ``payToAddress`` comes from the configured receiving wallet, not a placeholder.
"""

from __future__ import annotations

import base64
import json
from typing import Any

from .models import SUPPORTED_ASSETS, SUPPORTED_NETWORKS, flatten_supported_assets
from .service import (
    X402_NETWORK_WALLETS,
    X402_PRICING,
    X402_WALLET,
    asset_to_atomic,
    resolve_price_usd,
)

__all__ = [
    "HEADER_CONTENT_TYPE",
    "HEADER_PAYMENT_REQUIRED",
    "HEADER_X_PAYMENT_OPTIONS",
    "HEADER_X_PAYMENT_REQUIRED",
    "LEGACY_TO_CAIP2",
    "NETWORK_ASSETS",
    "X402_VERSION",
    "asset_to_atomic",
    "build_multi_asset_accepts",
    "build_payment_required",
    "normalize_network_id",
    "payment_required_body",
    "payment_required_headers",
    "resolve_price_usd",
]

# ── Spec headers on the 402 response ────────────────────────────────────────
HEADER_PAYMENT_REQUIRED = "PAYMENT-REQUIRED"
HEADER_X_PAYMENT_REQUIRED = "X-Payment-Required"
HEADER_X_PAYMENT_OPTIONS = "X-Payment-Options"
HEADER_CONTENT_TYPE = "Content-Type"

# x402 version this implementation emits (v1 wire; TODO(v2) additive).
X402_VERSION = 1

# ── Canonical acceptance surface (CAIP-2 ids, per X402-SPEC-CONTRACT §3) ────
# Legacy bare name -> CAIP-2 id for INPUT normalisation only; everything this
# module EMITS is CAIP-2 (canonical tables live in models.py).
LEGACY_TO_CAIP2: dict[str, str] = {
    "base": "eip155:8453",
    "ethereum": "eip155:1",
    "arbitrum": "eip155:42161",
    "optimism": "eip155:10",
    "polygon": "eip155:137",
    "solana": "solana:mainnet",
    "tron": "tron:mainnet",
}

# Which assets each CAIP-2 network accepts — derived from the canonical table.
NETWORK_ASSETS: dict[str, list[str]] = {
    network: list(assets) for network, assets in SUPPORTED_ASSETS.items()
}

# Stablecoin USD-per-unit is 1:1; volatile assets use a static fallback quote
# (a live oracle would refine this; contract §4). Mirrors x402-py
# ``usd_per_unit_for``. None => asset cannot be quoted honestly -> skipped.
_STABLECOIN_ASSETS = {"USDC", "USDT", "DAI", "POL", "MATIC", "TRX"}
_STATIC_USD_PER_UNIT = {"ETH": 3000.0, "WETH": 3000.0, "ARB": 1.0, "SOL": 150.0}


def normalize_network_id(network: str) -> str:
    """Map a legacy network name to its CAIP-2 id (spec §3)."""
    return LEGACY_TO_CAIP2.get(network.strip().lower(), network.strip())


def _pay_to_address(caip2: str) -> str:
    """Resolve the configured receiving wallet for a CAIP-2 network.

    Single payTo authority: the service-layer ``X402_NETWORK_WALLETS`` map
    (env ``X402_WALLET_<NETWORK>`` per CAIP-2 id, fallback ``X402_WALLET``).
    """
    return (X402_NETWORK_WALLETS.get(caip2) or X402_WALLET or "").strip()


def _usd_per_unit_for(asset: str) -> float | None:
    """USD price of ONE whole unit of ``asset`` (None => not quotable)."""
    asset = asset.upper()
    if asset in _STABLECOIN_ASSETS:
        return 1.0
    return _STATIC_USD_PER_UNIT.get(asset)


def _operation_description(operation: str) -> str:
    """Human-readable description for an operation from the pricing table."""
    info = X402_PRICING.get(operation) or X402_PRICING.get("api_access") or {}
    return str(info.get("description") or operation or "api_access")


def _option(
    *,
    operation: str,
    resource: str,
    description: str,
    caip2: str,
    asset: str,
    entry: dict[str, Any],
) -> dict[str, Any] | None:
    """One ``accepts[]`` entry (exact scheme, atomic amount, CAIP-2 id).

    Carries the asset SYMBOL in ``asset`` and the contract address in
    ``extra.contract`` (fleet v1 convention; the official v2 spec uses the
    address directly — see ``models.SUPPORTED_ASSETS``). Returns None when the
    option cannot be quoted honestly (no USD-per-unit or no atomic conversion).
    """
    unit_usd = _usd_per_unit_for(asset)
    if unit_usd is None:
        return None
    price_usd = resolve_price_usd(operation, caip2, asset)
    if price_usd <= 0:
        return None
    try:
        amount_atomic = asset_to_atomic(price_usd, asset, caip2)
    except ValueError:
        # Not verifiable/quotable on this network — never advertise it.
        return None
    return {
        "scheme": "exact",
        "network": caip2,
        "maxAmountRequired": str(amount_atomic),
        "resource": resource,
        "description": description,
        "mimeType": "application/json",
        "payToAddress": _pay_to_address(caip2),
        "asset": asset.upper(),
        "extra": {
            "name": str(entry.get("name") or asset),
            "version": "2",
            "operation": operation,
            "caip2": caip2,
            "verification": str(entry.get("verification") or ""),
            "contract": str(entry.get("contract") or ""),
            "decimals": int(entry.get("decimals") or 6),
            "priceUsd": price_usd,
            "usdPerUnit": unit_usd,
            "usdPerUnitSource": (
                "stablecoin" if asset.upper() in _STABLECOIN_ASSETS else "static-default"
            ),
        },
    }


def build_multi_asset_accepts(
    operation: str,
    resource: str = "",
    description: str = "",
) -> list[dict[str, Any]]:
    """Multi-asset ``accepts[]`` covering every quotable (network, asset).

    Only networks with a configured receiving wallet are advertised, and only
    assets that can be quoted + verified honestly (mirrors x402-py
    ``build_multi_asset_accepts`` skip rules).
    """
    if not description:
        description = _operation_description(operation)
    accepts: list[dict[str, Any]] = []
    for caip2, assets in SUPPORTED_ASSETS.items():
        if not _pay_to_address(caip2):
            # Only advertise networks we have a receiving wallet for.
            continue
        for asset, entry in assets.items():
            option = _option(
                operation=operation,
                resource=resource,
                description=description,
                caip2=caip2,
                asset=asset,
                entry=entry,
            )
            if option is not None:
                accepts.append(option)
    if not accepts:
        base = normalize_network_id("base")
        fallback = _option(
            operation=operation,
            resource=resource,
            description=description,
            caip2=base,
            asset="USDC",
            entry=SUPPORTED_ASSETS[base]["USDC"],
        )
        if fallback is not None:
            accepts.append(fallback)
    return accepts


def _option_v2(
    *,
    operation: str,
    caip2: str,
    asset: str,
    entry: dict[str, Any],
) -> dict[str, Any] | None:
    """One v2 ``accepts[]`` entry (spec §5.1: amount/payTo/asset-address).

    Mirrors :func:`_option` skip rules — never advertise an option that cannot
    be quoted honestly. Native assets with no contract carry the symbol as
    ``asset`` (the spec allows an ISO-style identifier where no contract exists).
    """
    unit_usd = _usd_per_unit_for(asset)
    if unit_usd is None:
        return None
    price_usd = resolve_price_usd(operation, caip2, asset)
    if price_usd <= 0:
        return None
    try:
        amount_atomic = asset_to_atomic(price_usd, asset, caip2)
    except ValueError:
        return None
    contract = str(entry.get("contract") or "")
    return {
        "scheme": "exact",
        "network": caip2,
        "amount": str(amount_atomic),
        "asset": contract or asset.upper(),
        "payTo": _pay_to_address(caip2),
        "maxTimeoutSeconds": 60,
        "extra": {"name": str(entry.get("name") or asset), "version": "2"},
    }


def build_payment_required_v2(
    operation: str,
    resource_url: str = "",
    description: str = "",
    mime_type: str = "application/json",
) -> dict[str, Any]:
    """Spec-canonical **v2** PaymentRequired body (x402-specification-v2 §5.1).

    Differences from v1: ``x402Version: 2``, required top-level ``resource``
    ResourceInfo, and accepts entries keyed ``amount``/``payTo`` with the
    asset CONTRACT ADDRESS as ``asset`` plus required ``maxTimeoutSeconds``.
    """
    if not description:
        description = _operation_description(operation)
    accepts: list[dict[str, Any]] = []
    for caip2, assets in SUPPORTED_ASSETS.items():
        if not _pay_to_address(caip2):
            continue
        for asset, entry in assets.items():
            option = _option_v2(operation=operation, caip2=caip2, asset=asset, entry=entry)
            if option is not None:
                accepts.append(option)
    body: dict[str, Any] = {
        "x402Version": 2,
        "resource": {
            "url": resource_url,
            "description": description,
            "mimeType": mime_type,
        },
        "accepts": accepts,
        "extensions": {},
    }
    return body


def protocol_version() -> int:
    """Configured wire version (``X402_PROTOCOL_VERSION``, default 1)."""
    import os

    raw = os.getenv("X402_PROTOCOL_VERSION", "1").strip()
    return 2 if raw == "2" else 1


def build_payment_required(
    operation: str,
    resource: str = "",
    multi_asset: bool = True,
) -> dict[str, Any]:
    """Build the spec-compliant ``{x402Version:1, accepts:[...]}`` body.

    TODO(v2): additive — x402Version=2 restructures accepts entries
    (``amount``/``payTo``, top-level ResourceInfo); keep this v1 shape until
    the gateway migrates.
    """
    accepts = build_multi_asset_accepts(operation, resource=resource)
    body: dict[str, Any] = {"x402Version": X402_VERSION, "accepts": accepts}
    if multi_asset:
        # Additive multi-chain surface so agents see every network/asset
        # (canonical row shapes — models.NetworkInfo / models.SupportedAsset).
        body["networks"] = [dict(network) for network in SUPPORTED_NETWORKS]
        body["supported_assets"] = flatten_supported_assets()
    return body


def _encode_payment_required_body(body: dict[str, Any]) -> str:
    """Base64-encode the PaymentRequired body (canonical wire format)."""
    return base64.b64encode(json.dumps(body).encode("utf-8")).decode("ascii")


def payment_required_body(resource: str = "", operation: str = "api_access") -> dict[str, Any]:
    """The spec body for a generic 402 challenge (v1 or v2 per config)."""
    if protocol_version() == 2:
        return build_payment_required_v2(operation or "api_access", resource_url=resource)
    return build_payment_required(operation or "api_access", resource=resource, multi_asset=True)


def payment_required_headers(body: dict[str, Any]) -> dict[str, str]:
    """Spec header set for a 402 response carrying ``body``."""
    return {
        HEADER_PAYMENT_REQUIRED: _encode_payment_required_body(body),
        HEADER_X_PAYMENT_REQUIRED: "true",
        HEADER_X_PAYMENT_OPTIONS: "x402",
    }
