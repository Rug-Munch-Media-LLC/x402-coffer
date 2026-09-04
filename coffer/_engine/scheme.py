# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Rug Munch Media LLC
# --- scheme.py: backward-compat re-exports from coffer._engine ---
import dataclasses
from enum import Enum

from .assets import (
    CAIP2_NETWORK_IDS,
    SUPPORTED_ASSETS,
    SUPPORTED_NETWORKS,
    flatten_supported_assets,
)
from .body import (
    _encode_payment_required_body,
    build_multi_asset_accepts,
    build_payment_required,
)
from .facilitator_config import (
    X402_DEFAULT_ASSET,
    X402_DEFAULT_NETWORK,
)
from .pricing import (
    SCAFFOLDED_NETWORKS,
    VERIFICATION_STATUS,
    X402_PRICING,
    asset_to_atomic,
    get_asset_usd_price,
    network_family,
    network_verification_status,
    normalize_network_id,
    resolve_asset_entry,
    resolve_price_usd,
    validate_client_amount,
)


class X402Network(str, Enum):
    """Supported x402 networks (CAIP-2 ids)."""

    BASE = "eip155:8453"
    ETHEREUM = "eip155:1"
    ARBITRUM = "eip155:42161"
    OPTIMISM = "eip155:10"
    POLYGON = "eip155:137"
    SOLANA = "solana:mainnet"
    TRON = "tron:mainnet"
    BASE_SEPOLIA = "eip155:84532"
    ETHEREUM_SEPOLIA = "eip155:11155111"


class X402Asset(str, Enum):
    """Supported x402 assets (uppercase symbols)."""

    USDC = "USDC"
    USDT = "USDT"
    ETH = "ETH"
    ARB = "ARB"
    WETH = "WETH"
    SOL = "SOL"
    TRX = "TRX"


class X402Scheme(str, Enum):
    """x402 payment schemes (spec §6)."""

    EXACT = "exact"
    UPTO = "upto"
    BATCH_SETTLEMENT = "batch-settlement"


def negotiate_scheme(client_schemes: list[str] | None = None, network: str = "", asset: str = "") -> str:
    """Select best payment scheme based on client capabilities and network.

    Priority: exact-match > network-native > fallback to 'exact'.
    """
    if not client_schemes:
        return "exact"

    # Network-native scheme preferences
    _NATIVE = {"eip155": "exact", "solana": "exact", "tron": "exact"}

    native = _NATIVE.get(network.split(":")[0], "exact") if network else "exact"

    # Prefer native scheme if client supports it
    if native in client_schemes:
        return native

    # Fall back to exact (always supported)
    if "exact" in client_schemes:
        return "exact"

    # Use first client scheme as last resort
    return client_schemes[0] if client_schemes else "exact"


_IMMUTABLE_FIELDS = {"scheme", "network", "asset", "max_timeout_seconds"}
_RESERVED_EXTRA_KEYS = {"paymentFlow", "assetTransferMethod"}


def validate_scheme_extension(baseline: dict, enriched: dict, extension_key: str = "") -> str | None:
    """Validate that a scheme extension only enriches, never weakens. Returns None if OK, error string if violated."""
    if len(baseline.get("accepts", [])) != len(enriched.get("accepts", [])):
        return f"extension {extension_key} changed accepts length"
    for field in _IMMUTABLE_FIELDS:
        if baseline.get(field) != enriched.get(field):
            return f"extension {extension_key} modified immutable field: {field}"
    for key in _RESERVED_EXTRA_KEYS:
        if key in baseline.get("extra", {}) and key not in enriched.get("extra", {}):
            return f"extension {extension_key} removed reserved key: {key}"
    return None


@dataclasses.dataclass
class PaymentInfo:
    """EIP-712 PaymentInfo for auth-capture escrow scheme."""

    payer: str
    payee: str
    amount_usd: float
    asset: str
    network: str
    pre_approval_expiry: int  # unix timestamp
    authorization_expiry: int  # unix timestamp
    refund_expiry: int  # unix timestamp
    min_fee_bps: int = 0
    max_fee_bps: int = 100  # 1%
    capture_authorizer: str = ""

    def is_expired(self, ts: int = 0) -> bool:
        import time

        now = ts or int(time.time())
        return now > self.authorization_expiry

    def can_refund(self, ts: int = 0) -> bool:
        import time

        now = ts or int(time.time())
        return now > self.refund_expiry

    def validate_fee(self, fee_bps: int) -> bool:
        return self.min_fee_bps <= fee_bps <= self.max_fee_bps


def build_multi_payment_required(
    operation: str,
    resource: str,
    networks: list[dict] | None = None,
    amount_usd: float = 0.01,
) -> list[dict]:
    """Build payment requirements for multiple networks so payer can choose chain.

    Each network dict: {"network": "eip155:8453", "asset": "USDC", "address": "0x..."}
    """
    if not networks:
        networks = [
            {"network": "eip155:8453", "asset": "USDC", "address": ""},
            {"network": "solana:mainnet", "asset": "USDC", "address": ""},
        ]
    return [
        {
            "scheme": "exact",
            "network": net["network"],
            "asset": net["asset"],
            "amount": str(amount_usd),
            "payToAddress": net.get("address", ""),
            "resource": resource,
            "description": operation,
        }
        for net in networks
    ]


__all__ = [
    "CAIP2_NETWORK_IDS",
    "SCAFFOLDED_NETWORKS",
    "SUPPORTED_ASSETS",
    "SUPPORTED_NETWORKS",
    "VERIFICATION_STATUS",
    "X402_DEFAULT_ASSET",
    "X402_DEFAULT_NETWORK",
    "X402_PRICING",
    "PaymentInfo",
    "X402Asset",
    "X402Network",
    "X402Scheme",
    "_encode_payment_required_body",
    "asset_to_atomic",
    "build_multi_asset_accepts",
    "build_multi_payment_required",
    "build_payment_required",
    "flatten_supported_assets",
    "get_asset_usd_price",
    "negotiate_scheme",
    "network_family",
    "network_verification_status",
    "normalize_network_id",
    "resolve_asset_entry",
    "resolve_price_usd",
    "validate_client_amount",
    "validate_scheme_extension",
]
