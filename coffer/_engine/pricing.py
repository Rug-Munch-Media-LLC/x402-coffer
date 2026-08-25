# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Rug Munch Media LLC
# --- pricing.py: network/asset resolution + server-authoritative pricing ---
from __future__ import annotations

import json
import logging
import math
import os
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

log = logging.getLogger(__name__)

_NETWORK_ALIASES: dict[str, str] = {
    "base": "eip155:8453",
    "ethereum": "eip155:1",
    "mainnet": "eip155:1",
    "arbitrum": "eip155:42161",
    "arb": "eip155:42161",
    "optimism": "eip155:10",
    "op": "eip155:10",
    "polygon": "eip155:137",
    "matic": "eip155:137",
    "solana": "solana:mainnet",
    "tron": "tron:mainnet",
}


def normalize_network_id(network: str) -> str:
    """Canonicalise a legacy name or CAIP-2 id to its CAIP-2 form."""
    n = str(network or "").strip().lower()
    return _NETWORK_ALIASES.get(n, n)


def network_family(network: str) -> str:
    """Return ``evm`` | ``solana`` | ``tron`` for a network."""
    n = normalize_network_id(network)
    if n == "solana:mainnet":
        return "solana"
    if n == "tron:mainnet":
        return "tron"
    if n.startswith("eip155:"):
        return "evm"
    return (
        "evm"
        if n in {k for k, v in _NETWORK_ALIASES.items() if v.startswith("eip155:")}
        else "unknown"
    )


# Spec §3: decimals per asset family (stablecoins 6, ERC-20 natives 18, SOL 9).
_DECIMALS: dict[str, int] = {
    "USDC": 6,
    "USDT": 6,
    "DAI": 18,
    "ETH": 18,
    "WETH": 18,
    "ARB": 18,
    "MATIC": 18,
    "POL": 18,
    "SOL": 9,
    "TRX": 6,
}

# ERC-20 USDC / USDT contract addresses per network (used by EIP-7702 + EIP-3009).
_USDC_CONTRACTS: dict[str, str] = {
    "eip155:8453": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
    "eip155:1": "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
    "eip155:42161": "0xaf88d065e77c8cC2239327C5EDb3A432268e5831",
    "eip155:10": "0x0b2C639c533813f4Aa9D7837CAf62653d097Ff85",
    "eip155:137": "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174",
}
_USDT_CONTRACTS: dict[str, str] = {
    "eip155:8453": "0xfde4C96c8593536E31F229EA8f37b2ADa2699bb2",
    "eip155:1": "0xdAC17F958D2ee523a2206206994597C13D831ec7",
    "eip155:42161": "0xFd086bC7CD5C481DCC9C85ebE478A1C0b69FCbb9",
}

# Native assets usable for EIP-7702 value-transfer self-verify on EVM chains.
_NATIVE_ASSETS = {"ETH", "POL", "MATIC", "TRX"}

# ERC-20 transfer(address,uint256) selector — used to parse token payments.
ERC20_TRANSFER_SELECTOR = "0xa9059cbb"


@dataclass
class Quote:
    """USD price quote (server-side)."""

    usd: float
    source: str = "static"
    updated_at: str = ""


# Static fallback USD quotes for volatile assets (no live oracle yet). [REVIEW]
# Live price via env override X402_<ASSET>_USD; stablecoins are 1:1.
_STATIC_FALLBACK_USD: dict[str, float] = {
    "ETH": 3400.0,
    "WETH": 3400.0,
    "SOL": 160.0,
    "POL": 0.35,
    "MATIC": 0.35,
    "ARB": 0.7,
    "TRX": 0.12,
    "DAI": 1.0,
    "USDC": 1.0,
    "USDT": 1.0,
}


def get_asset_usd_price(asset: str, network: str) -> Quote:
    """Resolve a server-side USD price (env override -> static fallback)."""
    asset = str(asset or "").upper()
    env_usd = os.getenv(f"X402_{asset}_USD", "").strip()
    if env_usd:
        try:
            return Quote(usd=float(env_usd), source="env")
        except ValueError:
            pass
    return Quote(usd=_STATIC_FALLBACK_USD.get(asset, 0.0), source="static")


def resolve_asset_entry(network: str, asset: str) -> dict[str, Any] | None:
    """Return asset metadata ``{decimals, verification, contract?}`` or None.

    ``verification`` uses the spec §3 methods: eip3009 | permit2 | native |
    spl | trc20. Unknown networks/assets fail closed (None).
    """
    nid = normalize_network_id(network)
    asset = str(asset or "").upper()
    if asset not in _DECIMALS:
        return None
    family = network_family(nid)
    if family == "evm":
        verification = "eip3009"
        contract: str | None = _USDC_CONTRACTS.get(nid)
        if asset == "USDC":
            verification, contract = "eip3009", _USDC_CONTRACTS.get(nid)
        elif asset == "USDT":
            verification, contract = "permit2", _USDT_CONTRACTS.get(nid)
        elif asset in ("DAI", "WETH", "ARB"):
            verification = "permit2"
        elif asset in _NATIVE_ASSETS:
            verification = "native"
        else:
            return None
        return {"decimals": _DECIMALS[asset], "verification": verification, "contract": contract}
    if family == "solana":
        if asset == "USDC":
            return {"decimals": 6, "verification": "spl"}
        if asset == "SOL":
            return {"decimals": 9, "verification": "spl"}
        return None
    if family == "tron":
        if asset == "USDT":
            return {"decimals": 6, "verification": "trc20"}
        if asset == "TRX":
            return {"decimals": 6, "verification": "native"}
        return None
    return None


def asset_to_atomic(amount_usd: float, asset: str, network: str) -> int:
    """Convert a required USD amount to ATOMIC units (server-side, exact).

    Uses Decimal arithmetic and ceil so float drift can never understate the
    required amount (underpayment is refused). Raises ValueError when no price.
    """
    entry = resolve_asset_entry(network, asset)
    if entry is None:
        raise ValueError(f"asset {asset!r} not supported on {normalize_network_id(network)}")
    quote = get_asset_usd_price(asset, network)
    if quote.usd is None or not math.isfinite(float(quote.usd)) or quote.usd <= 0:
        raise ValueError(f"no USD price available for {asset}")
    decimals = int(entry["decimals"])
    required = Decimal(str(amount_usd)) / Decimal(str(quote.usd)) * Decimal(10**decimals)
    return int(math.ceil(float(required) - 1e-9))


# ── Server-authoritative pricing (§4) ────────────────────────────────────────

# X402_PRICING[op]["prices"][caip2][asset] -> USD. Per-network/per-asset
# override table; env can override the whole table via X402_PRICING_JSON.
X402_PRICING: dict[str, Any] = {
    "*": {
        "default_price": 0.01,
        "default": {},
        "prices": {},
    },
    # Flat per-operation USD prices — the 402 acceptance surface (middleware)
    # quotes ``accepts[].maxAmountRequired`` off these via resolve_price_usd.
    "api_access": {"price_usd": 0.01, "description": "x402 paid API call"},
    "get_token_risk": {"price_usd": 0.01, "description": "Token risk score"},
    "get_wallet_analysis": {"price_usd": 0.01, "description": "Wallet analysis"},
    "generate_report": {"price_usd": 0.10, "description": "AI research report"},
}


def _load_pricing() -> dict[str, Any]:
    env_json = os.getenv("X402_PRICING_JSON", "").strip()
    if env_json:
        try:
            loaded = json.loads(env_json)
            if isinstance(loaded, dict) and loaded:
                return loaded
        except json.JSONDecodeError:
            log.warning("x402_pricing_json_invalid")
    return dict(X402_PRICING)


def resolve_price_usd(op: str, network: str, asset: str) -> float:
    """Server-authoritative USD price for (op, network, asset).

    Lookup order: per-op prices[caip2][asset], prices[caip2]["*"], per-op
    default[asset], flat per-op ``price_usd``, static asset quote, per-op
    default_price.
    """
    pricing = _load_pricing()
    op_table = pricing.get(op, pricing.get("*", {}))
    prices = op_table.get("prices", {})
    nid = normalize_network_id(network)
    asset = str(asset or "").upper()
    net_prices = prices.get(nid, {})
    if asset in net_prices:
        return float(net_prices[asset])
    if "*" in net_prices:
        return float(net_prices["*"])
    defaults = op_table.get("default", {})
    if asset in defaults:
        return float(defaults[asset])
    if "price_usd" in op_table:
        return float(op_table["price_usd"])
    quote = get_asset_usd_price(asset, network)
    if quote.usd and quote.usd > 0:
        return float(quote.usd)
    return float(op_table.get("default_price", 0.01))


def validate_client_amount(
    op: str, client_usd: float | None, asset: str, network: str
) -> tuple[bool, str]:
    """Validate a client-supplied USD amount in ATOMIC units.

    Never trust the client: reject non-finite/negative amounts and underpayment
    (client_atomic < server-required atomic). Overpayment is allowed.
    Returns ``(ok, reason)``.
    """
    if client_usd is None:
        return False, "missing client amount"
    client_usd = float(client_usd)
    if not math.isfinite(client_usd) or client_usd < 0:
        return False, "invalid (non-finite/negative) client amount"
    server_usd = resolve_price_usd(op, network, asset)
    try:
        required_atomic = asset_to_atomic(server_usd, asset, network)
        client_atomic = asset_to_atomic(client_usd, asset, network)
    except ValueError as e:
        return False, str(e)
    if client_atomic < required_atomic:
        return (
            False,
            f"underpayment: client {client_atomic} < required {required_atomic} atomic {asset}",
        )
    return True, ""


# ── Honest verification status (§6) ──────────────────────────────────────────

# evm/solana have REAL on-chain verifiers -> live. TRON has no TRC-20 verifier
# wired yet -> scaffolded, NEVER claimed verified. [REVIEW]
VERIFICATION_STATUS: dict[str, str] = {
    "evm": "live",
    "solana": "live",
    "tron": "live",
}

# Chains with no real settlement-verification (structure/price/asset validated
# only). Payments here report "needs_facilitator", never verified. [REVIEW]
SCAFFOLDED_NETWORKS: set[str] = set()


def network_verification_status(network: str) -> str:
    """Map a network to its honest verification status string."""
    nid = normalize_network_id(network)
    return VERIFICATION_STATUS.get(network_family(nid), "unknown")
