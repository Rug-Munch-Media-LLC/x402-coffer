"""Canonical supported network/asset tables for the x402 domain.

The bulk data lives here (``SUPPORTED_ASSETS``, ``SUPPORTED_NETWORKS``,
``CAIP2_NETWORK_IDS`` and the flatten helper) so :mod:`app.domain.x402.models`
can stay a thin Pydantic facade. Both modules mirror the reference
implementation ``x402-py:x402/scheme.py`` key-for-key; see
:mod:`app.domain.x402.models` for the wire-shape contract.
"""

from __future__ import annotations

import re
from typing import Any

__all__ = [
    "CAIP2_NETWORK_IDS",
    "SUPPORTED_ASSETS",
    "SUPPORTED_NETWORKS",
    "_ATOMIC_AMOUNT_RE",
    "_CAIP2_RE",
    "_entry",
    "_require_caip2",
    "_require_caip2_or_none",
    "flatten_supported_assets",
    "normalize_caip2_network",
]

#: Legacy bare network name -> CAIP-2 id (fleet contract §3; mirrors
#: x402-py ``CAIP2_NETWORK_IDS`` plus the spec's base-sepolia example).
#: CAIP-2 ids pass through unchanged.
CAIP2_NETWORK_IDS: dict[str, str] = {
    "base": "eip155:8453",
    "base-sepolia": "eip155:84532",
    "ethereum": "eip155:1",
    "arbitrum": "eip155:42161",
    "optimism": "eip155:10",
    "polygon": "eip155:137",
    "solana": "solana:mainnet",
    "tron": "tron:mainnet",
}

# CAIP-2 chain id: namespace ":" reference with namespace [-a-z0-9]{3,8}
# and reference [-_a-zA-Z0-9]{1,32}.
_CAIP2_RE = re.compile(r"^[-a-z0-9]{3,8}:[-_a-zA-Z0-9]{1,32}$")

#: Atomic token amounts are non-negative integer strings on the wire.
_ATOMIC_AMOUNT_RE = re.compile(r"^[0-9]+$")


def normalize_caip2_network(value: str) -> str:
    """Normalise a network id to CAIP-2, raising for unknown shapes.

    Known legacy bare names (:data:`CAIP2_NETWORK_IDS`) map to their CAIP-2
    id; anything else must already be valid CAIP-2.
    """
    candidate = value.strip()
    mapped = CAIP2_NETWORK_IDS.get(candidate.lower())
    if mapped is not None:
        return mapped
    if _CAIP2_RE.match(candidate):
        return candidate
    raise ValueError(
        f"network {candidate!r} is neither a known legacy name "
        f"({', '.join(sorted(CAIP2_NETWORK_IDS))}) nor a valid CAIP-2 id"
    )


def _require_caip2(value: Any) -> str:
    """Shared validator body for required ``network`` fields."""
    if not isinstance(value, str):
        raise ValueError("network must be a string")
    return normalize_caip2_network(value)


def _require_caip2_or_none(value: Any) -> str | None:
    """Shared validator body for optional ``network`` fields."""
    if value is None:
        return None
    return _require_caip2(value)


#: Canonical supported-networks table (CAIP-2 ids; fleet contract §3).
#: Mirrors ``x402-py:x402/scheme.py::SUPPORTED_NETWORKS`` key-for-key so the
#: multi-asset 402 surface is byte-identical to the reference implementation.
SUPPORTED_NETWORKS: list[dict[str, str]] = [
    {"network": "eip155:8453", "name": "Base", "family": "evm", "chain": "eip155"},
    {"network": "eip155:1", "name": "Ethereum", "family": "evm", "chain": "eip155"},
    {"network": "eip155:42161", "name": "Arbitrum", "family": "evm", "chain": "eip155"},
    {"network": "eip155:10", "name": "Optimism", "family": "evm", "chain": "eip155"},
    {"network": "eip155:137", "name": "Polygon", "family": "evm", "chain": "eip155"},
    {"network": "solana:mainnet", "name": "Solana", "family": "solana", "chain": "solana"},
    {"network": "tron:mainnet", "name": "TRON", "family": "tron", "chain": "tron"},
    # Testnets (W5 testnet-first onboarding): dev goes zero -> first paid 200
    # without funding mainnet. Circle deploys canonical testnet USDC at fixed
    # addresses; verification reuses the live EVM paths (EIP-3009/EIP-7702).
    {"network": "eip155:84532", "name": "Base Sepolia", "family": "evm", "chain": "eip155"},
    {"network": "eip155:11155111", "name": "Ethereum Sepolia", "family": "evm", "chain": "eip155"},
]


def _entry(
    asset: str, network: str, name: str, contract: str, decimals: int, verification: str, kind: str
) -> dict[str, Any]:
    """One canonical (network, asset) metadata row."""
    return {
        "asset": asset,
        "network": network,
        "name": name,
        "contract": contract,
        "decimals": decimals,
        "verification": verification,
        "kind": kind,
    }


#: Canonical supported-assets table keyed by CAIP-2 network id, then asset
#: symbol. Mirrors ``x402-py:x402/scheme.py::SUPPORTED_ASSETS`` exactly:
#: ``accepts[].asset`` carries the SYMBOL and ``accepts[].extra.contract``
#: carries the contract address (the official v2 spec uses the address as the
#: asset id; the fleet v1 wire keeps symbol + ``extra.contract``).
#: NOTE: the verification layer (``service.resolve_asset_entry``) keeps the
#: legacy bridged Polygon USDC address for its EIP-7702 match table, same
#: intentional V1/V2 split as the reference implementation.
SUPPORTED_ASSETS: dict[str, dict[str, dict[str, Any]]] = {
    "eip155:8453": {
        "USDC": _entry(
            "USDC",
            "eip155:8453",
            "Base USDC",
            "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
            6,
            "eip3009",
            "erc20",
        ),
        "USDT": _entry(
            "USDT",
            "eip155:8453",
            "Base USDT",
            "0xfde4C96c8593536E31F229EA8f37b2ADa2699bb2",
            6,
            "permit2",
            "erc20",
        ),
        "ETH": _entry("ETH", "eip155:8453", "Base ETH", "", 18, "native", "native"),
        "WETH": _entry(
            "WETH",
            "eip155:8453",
            "Base WETH",
            "0x4200000000000000000000000000000000000006",
            18,
            "permit2",
            "erc20",
        ),
    },
    "eip155:1": {
        "USDC": _entry(
            "USDC",
            "eip155:1",
            "Ethereum USDC",
            "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
            6,
            "eip3009",
            "erc20",
        ),
        "USDT": _entry(
            "USDT",
            "eip155:1",
            "Ethereum USDT",
            "0xdAC17F958D2ee523a2206206994597C13D831ec7",
            6,
            "permit2",
            "erc20",
        ),
        "ETH": _entry("ETH", "eip155:1", "Ethereum ETH", "", 18, "native", "native"),
        "WETH": _entry(
            "WETH",
            "eip155:1",
            "Ethereum WETH",
            "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2",
            18,
            "permit2",
            "erc20",
        ),
    },
    "eip155:42161": {
        "USDC": _entry(
            "USDC",
            "eip155:42161",
            "Arbitrum USDC",
            "0xaf88d065e77c8cC2239327C5EDb3A432268e5831",
            6,
            "eip3009",
            "erc20",
        ),
        "USDT": _entry(
            "USDT",
            "eip155:42161",
            "Arbitrum USDT",
            "0xFd086bC7CD5C481DCC9C85ebE478A1C0b69FCbb9",
            6,
            "permit2",
            "erc20",
        ),
        "ARB": _entry(
            "ARB",
            "eip155:42161",
            "Arbitrum ARB",
            "0x912CE59144191C1204E64559FE8253a0e49E6548",
            18,
            "permit2",
            "erc20",
        ),
        "ETH": _entry("ETH", "eip155:42161", "Arbitrum ETH", "", 18, "native", "native"),
        "WETH": _entry(
            "WETH",
            "eip155:42161",
            "Arbitrum WETH",
            "0x82aF49447D8a07e3bd95BD0d56f35241523fBab1",
            18,
            "permit2",
            "erc20",
        ),
    },
    "eip155:10": {
        "USDC": _entry(
            "USDC",
            "eip155:10",
            "Optimism USDC",
            "0x0b2C639c533813f4Aa9D7837CAf62653d097F85F",
            6,
            "eip3009",
            "erc20",
        ),
        "USDT": _entry(
            "USDT",
            "eip155:10",
            "Optimism USDT",
            "0x94b008aA00579c1307B0EF2c499aD98a8ce58e58",
            6,
            "permit2",
            "erc20",
        ),
        "ETH": _entry("ETH", "eip155:10", "Optimism ETH", "", 18, "native", "native"),
        "WETH": _entry(
            "WETH",
            "eip155:10",
            "Optimism WETH",
            "0x4200000000000000000000000000000000000006",
            18,
            "permit2",
            "erc20",
        ),
    },
    "eip155:137": {
        "USDC": _entry(
            "USDC",
            "eip155:137",
            "Polygon USDC",
            "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359",
            6,
            "eip3009",
            "erc20",
        ),
        "USDT": _entry(
            "USDT",
            "eip155:137",
            "Polygon USDT",
            "0xc2132D05D31c914a87C6611C10748AEb04B58e8F",
            6,
            "permit2",
            "erc20",
        ),
        "POL": _entry("POL", "eip155:137", "Polygon POL", "", 18, "native", "native"),
        "WETH": _entry(
            "WETH",
            "eip155:137",
            "Polygon WETH",
            "0x7ceB23fD6bC0adD59E62ac25578270cFf1b9f619",
            18,
            "permit2",
            "erc20",
        ),
    },
    "solana:mainnet": {
        "USDC": _entry(
            "USDC",
            "solana:mainnet",
            "Solana USDC",
            "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
            6,
            "spl",
            "spl",
        ),
        "USDT": _entry(
            "USDT",
            "solana:mainnet",
            "Solana USDT",
            "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",
            6,
            "spl",
            "spl",
        ),
        "SOL": _entry("SOL", "solana:mainnet", "Solana SOL", "", 9, "native", "native"),
    },
    "eip155:84532": {
        "USDC": _entry(
            "USDC",
            "eip155:84532",
            "Base Sepolia USDC",
            "0x036CbD53842c5426634e7929541eC2318f3dCF7e",
            6,
            "eip3009",
            "erc20",
        ),
    },
    "eip155:11155111": {
        "USDC": _entry(
            "USDC",
            "eip155:11155111",
            "Ethereum Sepolia USDC",
            "0x1c7D4B196Cb0C7B01d743Fbc6116a902379C7238",
            6,
            "eip3009",
            "erc20",
        ),
    },
    "tron:mainnet": {
        "USDT": _entry(
            "USDT",
            "tron:mainnet",
            "TRON USDT",
            "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t",
            6,
            "trc20",
            "trc20",
        ),
        "TRX": _entry("TRX", "tron:mainnet", "TRON TRX", "", 6, "native", "native"),
    },
}


def flatten_supported_assets() -> list[dict[str, Any]]:
    """Flatten :data:`SUPPORTED_ASSETS` into the ``supported_assets`` rows.

    Wire shape of the optional multi-asset extension (contract §2.2); matches
    ``x402-py:x402/scheme.py::flatten_supported_assets`` key-for-key.
    """
    return [dict(entry) for assets in SUPPORTED_ASSETS.values() for entry in assets.values()]
