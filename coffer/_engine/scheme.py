# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Rug Munch Media LLC
# --- scheme.py: backward-compat re-exports from coffer._engine ---
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

__all__ = [
    "CAIP2_NETWORK_IDS",
    "SCAFFOLDED_NETWORKS",
    "SUPPORTED_ASSETS",
    "SUPPORTED_NETWORKS",
    "VERIFICATION_STATUS",
    "X402_DEFAULT_ASSET",
    "X402_DEFAULT_NETWORK",
    "X402_PRICING",
    "_encode_payment_required_body",
    "asset_to_atomic",
    "build_multi_asset_accepts",
    "build_payment_required",
    "flatten_supported_assets",
    "get_asset_usd_price",
    "network_family",
    "network_verification_status",
    "normalize_network_id",
    "resolve_asset_entry",
    "resolve_price_usd",
    "validate_client_amount",
]
