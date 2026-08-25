"""x402 — scheme/offer/pricing helpers.

Owns the x402 enums, the server-side pricing table, atomic-unit conversion,
Base64 header codecs, network/asset compatibility mapping, the
spec-compliant ``build_payment_required`` builder, and the legacy (pre-spec)
payment-request helpers. Config constants (``X402_WALLET``,
``X402_DEFAULT_ASSET``, …) are imported from the package root.
"""
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Rug Munch Media LLC
#
# Standalone x402 implementation — https://git.rugmunch.io/RugMunchMedia/x402-py
# Licensed under MIT. See LICENSE.

from __future__ import annotations

import base64
import json
import logging
import math
import time
import uuid
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, cast

import httpx

from x402 import (
    X402_DEFAULT_ASSET,
    X402_DEFAULT_NETWORK,
    X402_NETWORK_WALLETS,
    X402_WALLET,
    _env,
)

logger = logging.getLogger(__name__)

# x402 pricing per operation (USDC atomic units, 6 decimals).
#
# Each entry may optionally carry a ``prices`` sub-table keyed by CAIP-2
# network id -> asset -> USD price, allowing per-network per-asset prices to
# override the base ``price_usd`` (see ``resolve_price_usd``). Example:
#   "scrape": {
#       "price_usd": 0.001,
#       "description": "Single URL scrape",
#       "prices": {"eip155:137": {"USDC": 0.0008}},  # cheaper on Polygon
#   }
X402_PRICING: dict[str, dict[str, Any]] = {
    "scrape": {"price_usd": 0.001, "description": "Single URL scrape"},
    "crawl": {"price_usd": 0.01, "description": "Crawl up to 10 pages"},
    "extract": {"price_usd": 0.005, "description": "Structured extraction"},
    "monitor": {"price_usd": 0.02, "description": "Create scheduled monitor"},
    "llm_call": {"price_usd": 0.01, "description": "LLM extraction call"},
    "template_execute": {"price_usd": 0.002, "description": "Execute scraper template"},
    "template_batch_execute": {"price_usd": 0.01, "description": "Execute up to 20 template items"},
    "browser_automation": {"price_usd": 0.05, "description": "Browser automation"},
    "bulk_crawl": {"price_usd": 0.10, "description": "Crawl up to 1000 pages"},
    "pdf_extract": {"price_usd": 0.01, "description": "PDF table extraction"},
    "ocr_extract": {"price_usd": 0.005, "description": "Image OCR"},
    "graphql_query": {"price_usd": 0.003, "description": "GraphQL query execution"},
    "schema_extract": {"price_usd": 0.002, "description": "Schema.org/JSON-LD extraction"},
}


class X402Scheme(StrEnum):
    """Payment schemes supported by x402."""

    EXACT = "exact"
    UPTO = "upto"


class X402Network(StrEnum):
    """Blockchain networks supported by x402."""

    BASE = "base"
    SOLANA = "solana"
    ETHEREUM = "ethereum"
    POLYGON = "polygon"
    ARBITRUM = "arbitrum"
    OPTIMISM = "optimism"
    BNB = "bnb"
    AVALANCHE = "avalanche"
    BASE_SEPOLIA = "base-sepolia"
    TRON = "tron"
    BITCOIN = "bitcoin"
    LITECOIN = "litecoin"
    DOGECOIN = "dogecoin"


class X402Asset(StrEnum):
    """Token assets supported by x402."""

    USDC = "USDC"
    USDT = "USDT"
    DAI = "DAI"
    ETH = "ETH"
    WETH = "WETH"
    ARB = "ARB"
    SOL = "SOL"
    MATIC = "MATIC"
    POL = "POL"
    TRX = "TRX"
    BTC = "BTC"
    LTC = "LTC"
    DOGE = "DOGE"


# Native assets that settle via plain value transfers on the EIP-7702 RPCs.
_NATIVE_ASSETS = {X402Asset.ETH.value}

# Asset decimals for atomic conversion
ASSET_DECIMALS: dict[str, int] = {
    X402Asset.USDC.value: 6,
    X402Asset.USDT.value: 6,
    X402Asset.DAI.value: 18,
    X402Asset.ETH.value: 18,
    X402Asset.WETH.value: 18,
    X402Asset.ARB.value: 18,
    X402Asset.SOL.value: 9,
    X402Asset.MATIC.value: 18,
    X402Asset.POL.value: 18,
    X402Asset.TRX.value: 6,
    X402Asset.BTC.value: 8,
    X402Asset.LTC.value: 8,
    X402Asset.DOGE.value: 8,
}

# ── Canonical multi-chain / multi-asset acceptance table ────────────────────
#
# SUPPORTED_ASSETS is the single source of truth for which (network, asset)
# pairs the server accepts for x402 payments, keyed by CAIP-2 network id + asset
# symbol. `verification` names the authorization method:
#
#   eip3009 — ERC-20 transferWithAuthorization (USDC)
#   permit2 — ERC-20 Permit2 (USDT / ARB / WETH / …)
#   native  — plain value transfer of the chain's native asset
#   spl     — Solana SPL token transfer
#   trc20   — TRON TRC-20 transfer
#
# `kind` distinguishes contract tokens from native assets (contract == ""
# for native). Contract addresses are well-known mainnet addresses.

# CAIP-2 network ids. Legacy x402 network names (X402Network enum values) map
# to these; both spellings are accepted everywhere.
CAIP2_NETWORK_IDS: dict[str, str] = {
    "base": "eip155:8453",
    "ethereum": "eip155:1",
    "arbitrum": "eip155:42161",
    "optimism": "eip155:10",
    "polygon": "eip155:137",
    "solana": "solana:mainnet",
    "tron": "tron:mainnet",
}

SUPPORTED_NETWORKS: list[dict[str, Any]] = [
    {"network": "eip155:8453", "name": "Base", "family": "evm", "chain": "eip155"},
    {"network": "eip155:1", "name": "Ethereum", "family": "evm", "chain": "eip155"},
    {"network": "eip155:42161", "name": "Arbitrum", "family": "evm", "chain": "eip155"},
    {"network": "eip155:10", "name": "Optimism", "family": "evm", "chain": "eip155"},
    {"network": "eip155:137", "name": "Polygon", "family": "evm", "chain": "eip155"},
    {"network": "solana:mainnet", "name": "Solana", "family": "solana", "chain": "solana"},
    {"network": "tron:mainnet", "name": "TRON", "family": "tron", "chain": "tron"},
]

SUPPORTED_ASSETS: dict[str, dict[str, dict[str, Any]]] = {
    "eip155:8453": {
        "USDC": {
            "asset": "USDC",
            "network": "eip155:8453",
            "name": "Base USDC",
            "contract": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
            "decimals": 6,
            "verification": "eip3009",
            "kind": "erc20",
        },
        "USDT": {
            "asset": "USDT",
            "network": "eip155:8453",
            "name": "Base USDT",
            # Canonical bridged Tether USD on Base. NOTE: the legacy V1
            # EIP7702_TOKEN_ADDRESSES table (x402/__init__.py) still lists
            # 0x50c5725949A6F0c72E6C4a641F24049A917DB0Cb for Base USDT — a
            # known discrepancy. The V1 table is intentionally NOT churned;
            # this entry is the authoritative address.
            "contract": "0xfde4C96c8593536E31F229EA8f37b2ADa2699bb2",
            "decimals": 6,
            "verification": "permit2",
            "kind": "erc20",
        },
        "ETH": {
            "asset": "ETH",
            "network": "eip155:8453",
            "name": "Base ETH",
            "contract": "",
            "decimals": 18,
            "verification": "native",
            "kind": "native",
        },
        "WETH": {
            "asset": "WETH",
            "network": "eip155:8453",
            "name": "Base WETH",
            "contract": "0x4200000000000000000000000000000000000006",
            "decimals": 18,
            "verification": "permit2",
            "kind": "erc20",
        },
    },
    "eip155:1": {
        "USDC": {
            "asset": "USDC",
            "network": "eip155:1",
            "name": "Ethereum USDC",
            "contract": "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
            "decimals": 6,
            "verification": "eip3009",
            "kind": "erc20",
        },
        "USDT": {
            "asset": "USDT",
            "network": "eip155:1",
            "name": "Ethereum USDT",
            "contract": "0xdAC17F958D2ee523a2206206994597C13D831ec7",
            "decimals": 6,
            "verification": "permit2",
            "kind": "erc20",
        },
        "ETH": {
            "asset": "ETH",
            "network": "eip155:1",
            "name": "Ethereum ETH",
            "contract": "",
            "decimals": 18,
            "verification": "native",
            "kind": "native",
        },
        "WETH": {
            "asset": "WETH",
            "network": "eip155:1",
            "name": "Ethereum WETH",
            "contract": "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2",
            "decimals": 18,
            "verification": "permit2",
            "kind": "erc20",
        },
    },
    "eip155:42161": {
        "USDC": {
            "asset": "USDC",
            "network": "eip155:42161",
            "name": "Arbitrum USDC",
            "contract": "0xaf88d065e77c8cC2239327C5EDb3A432268e5831",
            "decimals": 6,
            "verification": "eip3009",
            "kind": "erc20",
        },
        "USDT": {
            "asset": "USDT",
            "network": "eip155:42161",
            "name": "Arbitrum USDT",
            "contract": "0xFd086bC7CD5C481DCC9C85ebE478A1C0b69FCbb9",
            "decimals": 6,
            "verification": "permit2",
            "kind": "erc20",
        },
        "ARB": {
            "asset": "ARB",
            "network": "eip155:42161",
            "name": "Arbitrum ARB",
            "contract": "0x912CE59144191C1204E64559FE8253a0e49E6548",
            "decimals": 18,
            "verification": "permit2",
            "kind": "erc20",
        },
        "ETH": {
            "asset": "ETH",
            "network": "eip155:42161",
            "name": "Arbitrum ETH",
            "contract": "",
            "decimals": 18,
            "verification": "native",
            "kind": "native",
        },
        "WETH": {
            "asset": "WETH",
            "network": "eip155:42161",
            "name": "Arbitrum WETH",
            "contract": "0x82aF49447D8a07e3bd95BD0d56f35241523fBab1",
            "decimals": 18,
            "verification": "permit2",
            "kind": "erc20",
        },
    },
    "eip155:10": {
        "USDC": {
            "asset": "USDC",
            "network": "eip155:10",
            "name": "Optimism USDC",
            "contract": "0x0b2C639c533813f4Aa9D7837CAf62653d097F85F",
            "decimals": 6,
            "verification": "eip3009",
            "kind": "erc20",
        },
        "USDT": {
            "asset": "USDT",
            "network": "eip155:10",
            "name": "Optimism USDT",
            "contract": "0x94b008aA00579c1307B0EF2c499aD98a8ce58e58",
            "decimals": 6,
            "verification": "permit2",
            "kind": "erc20",
        },
        "ETH": {
            "asset": "ETH",
            "network": "eip155:10",
            "name": "Optimism ETH",
            "contract": "",
            "decimals": 18,
            "verification": "native",
            "kind": "native",
        },
        "WETH": {
            "asset": "WETH",
            "network": "eip155:10",
            "name": "Optimism WETH",
            "contract": "0x4200000000000000000000000000000000000006",
            "decimals": 18,
            "verification": "permit2",
            "kind": "erc20",
        },
    },
    "eip155:137": {
        "USDC": {
            "asset": "USDC",
            "network": "eip155:137",
            "name": "Polygon USDC",
            "contract": "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359",
            "decimals": 6,
            "verification": "eip3009",
            "kind": "erc20",
        },
        "USDT": {
            "asset": "USDT",
            "network": "eip155:137",
            "name": "Polygon USDT",
            "contract": "0xc2132D05D31c914a87C6611C10748AEb04B58e8F",
            "decimals": 6,
            "verification": "permit2",
            "kind": "erc20",
        },
        "POL": {
            "asset": "POL",
            "network": "eip155:137",
            "name": "Polygon POL",
            "contract": "",
            "decimals": 18,
            "verification": "native",
            "kind": "native",
        },
        "WETH": {
            "asset": "WETH",
            "network": "eip155:137",
            "name": "Polygon WETH",
            "contract": "0x7ceB23fD6bC0adD59E62ac25578270cFf1b9f619",
            "decimals": 18,
            "verification": "permit2",
            "kind": "erc20",
        },
    },
    "solana:mainnet": {
        "USDC": {
            "asset": "USDC",
            "network": "solana:mainnet",
            "name": "Solana USDC",
            "contract": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
            "decimals": 6,
            "verification": "spl",
            "kind": "spl",
        },
        "USDT": {
            "asset": "USDT",
            "network": "solana:mainnet",
            "name": "Solana USDT",
            "contract": "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",
            "decimals": 6,
            "verification": "spl",
            "kind": "spl",
        },
        "SOL": {
            "asset": "SOL",
            "network": "solana:mainnet",
            "name": "Solana SOL",
            "contract": "",
            "decimals": 9,
            "verification": "native",
            "kind": "native",
        },
    },
    "tron:mainnet": {
        "USDT": {
            "asset": "USDT",
            "network": "tron:mainnet",
            "name": "TRON USDT",
            "contract": "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t",
            "decimals": 6,
            "verification": "trc20",
            "kind": "trc20",
        },
        "TRX": {
            "asset": "TRX",
            "network": "tron:mainnet",
            "name": "TRON TRX",
            "contract": "",
            "decimals": 6,
            "verification": "native",
            "kind": "native",
        },
    },
}

# Verification status per chain family — what is REAL vs scaffolded.
# ``status`` is the machine-readable label exposed by the health + pricing
# endpoints: "live" (EVM — EIP-7702 self-verify + facilitators; Solana —
# SPL RPC verifier, getTransaction jsonParsed USDC transferChecked) vs
# "scaffolded:needs_facilitator" (TRON — structure/price/asset validated
# only; on-chain settlement verification needs a facilitator impl or a
# TRC-20 RPC verifier). No fake security: scaffolded chains are never
# reported as live. [REVIEW] TRON needs a real verifier.
VERIFICATION_STATUS: dict[str, dict[str, Any]] = {
    "evm": {
        "status": "live",
        "real": True,
        "scaffolded": False,
        "note": "EIP-7702 self-verify (receipt + payTo/amount) and external facilitators",
    },
    "solana": {
        "status": "live",
        "real": True,
        "scaffolded": False,
        "note": "SPL RPC verifier — getTransaction jsonParsed USDC transferChecked to the configured Solana wallet",
    },
    "tron": {
        "status": "scaffolded:needs_facilitator",
        "real": False,
        "scaffolded": True,
        "note": (
            "[REVIEW] scaffold only — validates header/price/asset; settlement "
            "verification needs a facilitator or TRON Grid verifier"
        ),
    },
}

# USD value of ONE unit of each asset, used to quote USD-equivalent atomic
# amounts for non-1:1 assets (ETH/ARB/SOL/TRX/POL/…). Stablecoins are fixed
# at 1.0. Volatile assets use env-overridable STATIC defaults as the
# fallback; the live-oracle hook ``get_asset_usd_price`` quotes them from a
# free price oracle (CoinGecko ``/simple/price`` when
# ``X402_ORACLE_SOURCE=coingecko``) with an async cache + TTL, falling
# back to these static values with ``usdPerUnitSource:"static-default"`` only
# when the oracle is unreachable.
X402_ASSET_USD_PER_UNIT: dict[str, float] = {
    "USDC": 1.0,
    "USDT": 1.0,
    "DAI": 1.0,
    "ETH": float(_env("X402_USD_PER_UNIT_ETH", "2500")),
    "WETH": float(_env("X402_USD_PER_UNIT_WETH", "2500")),
    "ARB": float(_env("X402_USD_PER_UNIT_ARB", "0.6")),
    "SOL": float(_env("X402_USD_PER_UNIT_SOL", "140")),
    "MATIC": float(_env("X402_USD_PER_UNIT_MATIC", "0.4")),
    "POL": float(_env("X402_USD_PER_UNIT_POL", "0.4")),
    "TRX": float(_env("X402_USD_PER_UNIT_TRX", "0.15")),
}

# Live price oracle. The fetcher is REAL and wired: with a provider
# configured it hits CoinGecko's free ``/simple/price`` (no API key) or a
# custom JSON endpoint and caches with TTL, falling back to the static table
# only on failure. The provider is opt-in (``X402_ORACLE_SOURCE``) so the
# existing default contract — static ``usdPerUnitSource:"static-default"``
# prices unless an operator configures a live source — is preserved.
# Re-read from env per call so tests can toggle providers without reloading
# the module.
X402_ORACLE_URL: str = _env("X402_ORACLE_URL")
X402_ORACLE_SOURCE: str = _env("X402_ORACLE_SOURCE")
X402_ORACLE_TTL_SECONDS: float = float(_env("X402_ORACLE_TTL", "60"))

COINGECKO_SIMPLE_PRICE_URL = "https://api.coingecko.com/api/v3/simple/price"

# Asset symbol -> CoinGecko coin id for /simple/price lookups. WETH tracks
# ETH; MATIC maps to the legacy id (POL is the polygon ecosystem token).
COINGECKO_ASSET_IDS: dict[str, str] = {
    "ETH": "ethereum",
    "WETH": "ethereum",
    "ARB": "arbitrum",
    "SOL": "solana",
    "MATIC": "matic-network",
    "POL": "polygon-ecosystem-token",
    "TRX": "tron",
    "BTC": "bitcoin",
    "LTC": "litecoin",
    "DOGE": "dogecoin",
}

_STABLECOIN_ASSETS = {X402Asset.USDC.value, X402Asset.USDT.value, X402Asset.DAI.value}

# Async price cache: (source_key, asset, network) -> (monotonic_ts, usd).
# Keying by source means a test/operator toggling providers can never read a
# stale quote from a different provider.
_oracle_cache: dict[tuple[str, str, str], tuple[float, float]] = {}


def clear_price_cache() -> None:
    """Drop all cached oracle quotes (tests, provider toggles, re-probe)."""
    _oracle_cache.clear()


def oracle_source_label() -> str:
    """Return the active oracle provider label.

    ``"custom"`` (``X402_ORACLE_URL``), ``"coingecko"`` /
    ``"dexscreener"`` (``X402_ORACLE_SOURCE``), or ``"static-default"``
    when no live provider is configured.
    """
    if _env("X402_ORACLE_URL") or X402_ORACLE_URL:
        return "custom"
    source = _env("X402_ORACLE_SOURCE", X402_ORACLE_SOURCE)
    if source in ("coingecko", "dexscreener"):
        return source
    return "static-default"


@dataclass(frozen=True)
class AssetUsdQuote:
    """USD price of one unit of an asset plus where it came from.

    ``source`` is ``"oracle"`` (live), ``"stablecoin"`` (fixed 1:1) or
    ``"static-default"`` (fallback static table).
    """

    usd: float
    source: str


def _extract_usd_from_json(data: Any, coingecko_id: str = "") -> float | None:
    """Pull a positive USD price out of an oracle response.

    Handles the custom endpoint shapes (``{"usd": ...}``, ``{"data":
    {"usd": ...}}``) and the CoinGecko shape (``{id: {"usd": ...}}``).
    """
    if not isinstance(data, dict):
        return None
    if coingecko_id and isinstance(data.get(coingecko_id), dict):
        value = data[coingecko_id].get("usd")
        if isinstance(value, (int, float)) and float(value) > 0:
            return float(value)
    for candidate in (data, data.get("data") if isinstance(data, dict) else None):
        if not isinstance(candidate, dict):
            continue
        for key in ("usd", "priceUsd", "price"):
            value = candidate.get(key)
            if isinstance(value, (int, float)) and float(value) > 0:
                return float(value)
    return None


async def _fetch_oracle_price(asset: str, network: str = "") -> float | None:
    """Fetch a live USD price from the configured oracle; None on any failure.

    Provider resolution order:
    1. ``X402_ORACLE_URL`` set → ``GET {url}/price/{ASSET}`` (custom
       endpoint, backward compatible with the pre-production hook);
    2. ``X402_ORACLE_SOURCE`` set to ``coingecko`` → CoinGecko
        ``/simple/price?ids={id}&vs_currencies=usd``;
    3. otherwise None — no live provider configured (static defaults).
    """
    oracle_url = _env("X402_ORACLE_URL") or X402_ORACLE_URL
    asset = asset.upper()
    if oracle_url:
        url = f"{oracle_url.rstrip('/')}/price/{asset}"
        if network:
            url += f"?network={network}"
    else:
        source = _env("X402_ORACLE_SOURCE", X402_ORACLE_SOURCE)
        if source not in ("coingecko", "dexscreener"):
            return None
        coingecko_id = COINGECKO_ASSET_IDS.get(asset)
        if not coingecko_id:
            return None
        url = f"{COINGECKO_SIMPLE_PRICE_URL}?ids={coingecko_id}&vs_currencies=usd"
    try:
        timeout = float(_env("X402_ORACLE_TIMEOUT", "5"))
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.get(url)
            response.raise_for_status()
            data = response.json()
    except (httpx.HTTPError, ValueError, OSError):
        logger.warning(
            "x402_oracle_unavailable_falling_back",
            extra={"asset": asset, "oracle_url": oracle_url or oracle_source_label()},
        )
        return None
    coingecko_id = COINGECKO_ASSET_IDS.get(asset) if not oracle_url else ""
    return _extract_usd_from_json(data, coingecko_id or "")


async def get_asset_usd_price(asset: str, network: str = "") -> AssetUsdQuote:
    """Live-oracle hook: USD price of one unit of ``asset``.

    Stablecoins (USDC/USDT/DAI) are fixed 1:1 and never hit the network.
    Volatile assets are quoted from the live oracle with an async cache + TTL
    (``X402_ORACLE_TTL``, default 60s — at most one call per asset per
    TTL). Any failure falls back to the static ``X402_ASSET_USD_PER_UNIT``
    table, reporting ``source="static-default"`` (stablecoins report
    ``"stablecoin"``).

    This is the canonical quote path for async verification
    (``SelfFacilitator.verify``); the sync pricing helpers
    (``usd_per_unit_for`` / ``asset_to_atomic`` / ``build_multi_asset_accepts``)
    keep using the static table so accept-list construction stays sync.
    """
    asset = asset.upper()
    if asset in _STABLECOIN_ASSETS:
        static = X402_ASSET_USD_PER_UNIT.get(asset)
        return AssetUsdQuote(usd=static if static and static > 0 else 1.0, source="stablecoin")
    source_key = oracle_source_label()
    if source_key == "static-default":
        # No live provider configured — the existing default contract is
        # static prices (no network traffic, no rate-limit exposure).
        static = X402_ASSET_USD_PER_UNIT.get(asset)
        return AssetUsdQuote(usd=static if static and static > 0 else 1.0, source="static-default")
    cache_key = (source_key, asset, network)
    now = time.monotonic()
    ttl = float(_env("X402_ORACLE_TTL", str(X402_ORACLE_TTL_SECONDS)))
    entry = _oracle_cache.get(cache_key)
    if entry is not None and now - entry[0] < ttl:
        return AssetUsdQuote(usd=entry[1], source="oracle")
    live = await _fetch_oracle_price(asset, network)
    if live is not None:
        _oracle_cache[cache_key] = (now, live)
        return AssetUsdQuote(usd=live, source="oracle")
    static = X402_ASSET_USD_PER_UNIT.get(asset)
    if static is None or static <= 0:
        static = 1.0
    return AssetUsdQuote(usd=static, source="static-default")


async def probe_oracle() -> tuple[bool, str]:
    """Probe the configured price oracle with a single live call.

    Returns ``(reachable, source)``. ``reachable`` is True only when a live
    (``"oracle"``-sourced) quote was obtained; ``source`` is the provider
    label from :func:`oracle_source_label` (``"static-default"`` when no live
    provider is configured). Used by ``/v1/x402/health``.
    """
    asset = _env("X402_ORACLE_PROBE_ASSET", "ETH")
    quote = await get_asset_usd_price(asset)
    return quote.source == "oracle", oracle_source_label()


def normalize_network_id(network: str) -> str:
    """Map a legacy x402 network name (or CAIP-2 id) to the CAIP-2 id.

    Unknown values pass through unchanged so callers can decide how to treat
    them; empty input stays empty.
    """
    if not network:
        return network
    return CAIP2_NETWORK_IDS.get(network.lower(), network)


def network_family(network: str) -> str:
    """Return the chain family for a network: evm | solana | tron | unknown."""
    network_id = normalize_network_id(network)
    if ":" not in network_id:
        return "unknown"
    prefix = network_id.split(":", 1)[0]
    if prefix == "eip155":
        return "evm"
    if prefix == "solana":
        return "solana"
    if prefix == "tron":
        return "tron"
    return "unknown"


def network_verification_status(network: str) -> str:
    """Return the verification-status label for a network.

    ``"live"`` for EVM chains (EIP-7702 self-verify + facilitators) and
    Solana (SPL RPC verifier — getTransaction jsonParsed USDC
    transferChecked); ``"scaffolded:needs_facilitator"`` for TRON
    (structure/price/asset validated; on-chain settlement NOT verified until
    a facilitator impl or TRC-20 verifier is registered); ``"unknown"``
    otherwise. Exposed by the health + pricing endpoints so operators never
    mistake a scaffold for real settlement verification.
    """
    status = VERIFICATION_STATUS.get(network_family(network), {})
    return str(status.get("status", "unknown"))


def resolve_asset_entry(network: str, asset: str) -> dict[str, Any] | None:
    """Look up the canonical SUPPORTED_ASSETS entry for (network, asset).

    Accepts legacy network names or CAIP-2 ids. Returns None when the pair is
    not in the acceptance table.
    """
    network_id = normalize_network_id(network)
    if not network_id or not asset:
        return None
    return SUPPORTED_ASSETS.get(network_id, {}).get(asset.upper())


def is_asset_supported(network: str, asset: str) -> bool:
    """Return True when (network, asset) is in the canonical acceptance table."""
    return resolve_asset_entry(network, asset) is not None


def usd_per_unit_for(asset: str) -> float | None:
    """Return the USD value of one unit of ``asset`` (None when unknown)."""
    value = X402_ASSET_USD_PER_UNIT.get(asset.upper())
    if value is None or value <= 0:
        return None
    return value


def asset_to_atomic(
    amount_usd: float,
    asset: str = X402_DEFAULT_ASSET,
    network: str = "",
) -> int:
    """Convert a USD amount to atomic units for (network, asset).

    Uses the canonical SUPPORTED_ASSETS table when the pair is known so
    decimals and USD-per-unit come from one place; falls back to the legacy
    ASSET_DECIMALS table with 1:1 USD pricing for unknown pairs.
    """
    entry = resolve_asset_entry(network, asset)
    if entry:
        decimals = int(entry.get("decimals", ASSET_DECIMALS.get(asset.upper(), 6)))
    else:
        decimals = ASSET_DECIMALS.get(asset.upper(), 6)
    unit_usd = usd_per_unit_for(asset) or 1.0
    return int(round(amount_usd / unit_usd * (10**decimals)))


def flatten_supported_assets() -> list[dict[str, Any]]:
    """Flatten SUPPORTED_ASSETS into a single list of asset entries."""
    flat: list[dict[str, Any]] = []
    for network_id, assets in SUPPORTED_ASSETS.items():
        for asset, entry in assets.items():
            flat.append(
                {
                    "asset": asset,
                    "network": network_id,
                    "name": entry.get("name", asset),
                    "contract": entry.get("contract", ""),
                    "decimals": entry.get("decimals", 6),
                    "verification": entry.get("verification", ""),
                    "kind": entry.get("kind", ""),
                }
            )
    return flat


def _to_atomic(amount_usd: float, asset: str = X402_DEFAULT_ASSET) -> int:
    """Convert USD amount to atomic units for the given asset."""
    decimals = ASSET_DECIMALS.get(asset, 6)
    return int(amount_usd * (10**decimals))


def _from_atomic(amount_atomic: int, asset: str = X402_DEFAULT_ASSET) -> float:
    """Convert atomic units back to USD-normalized float."""
    decimals = ASSET_DECIMALS.get(asset, 6)
    return float(round(amount_atomic / (10**decimals), decimals))


def _encode_payment_required_body(body: dict[str, Any]) -> str:
    """Base64-encode the PaymentRequired body per x402 spec."""
    return base64.b64encode(json.dumps(body).encode()).decode()


def _decode_payment_signature(header: str) -> dict[str, Any] | None:
    """Decode the PAYMENT-SIGNATURE header per x402 spec."""
    try:
        return cast("dict[str, Any]", json.loads(base64.b64decode(header).decode()))
    except (ValueError, OSError):
        return None


def _encode_payment_response(settlement: dict[str, Any]) -> str:
    """Base64-encode the PaymentResponse (settlement receipt) per x402 spec."""
    return base64.b64encode(json.dumps(settlement).encode()).decode()


def _default_networks_for_asset(asset: str) -> list[str]:
    """Return sensible default networks for an asset."""
    asset = asset.upper()
    mapping: dict[str, list[str]] = {
        X402Asset.USDC.value: [
            X402Network.BASE.value,
            X402Network.ARBITRUM.value,
            X402Network.OPTIMISM.value,
            X402Network.POLYGON.value,
            X402Network.SOLANA.value,
            X402Network.ETHEREUM.value,
            X402Network.BNB.value,
            X402Network.AVALANCHE.value,
        ],
        X402Asset.USDT.value: [
            X402Network.ETHEREUM.value,
            X402Network.BNB.value,
            X402Network.TRON.value,
            X402Network.SOLANA.value,
            X402Network.ARBITRUM.value,
            X402Network.OPTIMISM.value,
            X402Network.POLYGON.value,
            X402Network.AVALANCHE.value,
        ],
        X402Asset.DAI.value: [
            X402Network.ETHEREUM.value,
            X402Network.POLYGON.value,
            X402Network.ARBITRUM.value,
            X402Network.OPTIMISM.value,
            X402Network.BASE.value,
        ],
        X402Asset.ETH.value: [
            X402Network.ETHEREUM.value,
            X402Network.BASE.value,
            X402Network.ARBITRUM.value,
            X402Network.OPTIMISM.value,
            X402Network.POLYGON.value,
        ],
        X402Asset.WETH.value: [
            X402Network.ETHEREUM.value,
            X402Network.BASE.value,
            X402Network.ARBITRUM.value,
            X402Network.OPTIMISM.value,
            X402Network.POLYGON.value,
        ],
        X402Asset.ARB.value: [X402Network.ARBITRUM.value],
        X402Asset.SOL.value: [X402Network.SOLANA.value],
        X402Asset.MATIC.value: [X402Network.POLYGON.value],
        X402Asset.POL.value: [X402Network.POLYGON.value],
        X402Asset.TRX.value: [X402Network.TRON.value],
        X402Asset.BTC.value: [X402Network.BITCOIN.value],
        X402Asset.LTC.value: [X402Network.LITECOIN.value],
        X402Asset.DOGE.value: [X402Network.DOGECOIN.value],
    }
    return mapping.get(asset, [X402Network.BASE.value])


def _asset_on_network(asset: str, network: str) -> bool:
    """Check whether an asset is generally supported on a network."""
    return network in _default_networks_for_asset(asset)


def resolve_price_usd(
    operation: str,
    network: str = "",
    asset: str = "",
) -> float:
    """Return the SERVER-side authoritative price (USD) for an operation.

    Never trust a client-supplied ``amount_usd``: this is the number the
    server enforces. Returns 0.0 for unknown operations (callers must decide
    how to treat those).

    When ``network`` (legacy name or CAIP-2 id) and ``asset`` are supplied, a
    per-network/per-asset override in ``X402_PRICING[operation]["prices"]``
    takes precedence; otherwise the operation's base ``price_usd`` applies.
    """
    info = X402_PRICING.get(operation) or {}
    if network and asset:
        network_id = normalize_network_id(network)
        raw_prices = info.get("prices")
        prices = raw_prices if isinstance(raw_prices, dict) else {}
        raw_network_prices = prices.get(network_id)
        network_prices = raw_network_prices if isinstance(raw_network_prices, dict) else {}
        override = network_prices.get(asset.upper())
        if override is not None:
            return float(override)
    return float(info.get("price_usd", 0.0))


def validate_client_amount(
    operation: str,
    client_amount_usd: float,
    asset: str = X402_DEFAULT_ASSET,
    network: str = "",
) -> tuple[bool, str]:
    """Reject client-supplied underpayment against the server-side price.

    Comparison happens in atomic units so float drift cannot be exploited.
    Returns ``(ok, reason)``. ``ok=False`` means the client offered LESS than
    the authoritative price (or a non-finite/negative amount) — the payment
    must be refused. Overpayment is allowed (client may pay more).

    When ``network`` is given and the (network, asset) pair is in the
    canonical SUPPORTED_ASSETS table, the table's decimals and USD-per-unit
    conversion are used (so non-1:1 assets like ETH/ARB quote correctly);
    otherwise the legacy 1:1 ASSET_DECIMALS path is used.
    """
    if client_amount_usd is None or not math.isfinite(float(client_amount_usd)):
        return False, "invalid (non-finite) client amount"
    if client_amount_usd < 0:
        return False, f"negative client amount {client_amount_usd}"
    required = resolve_price_usd(operation, network=network, asset=asset)
    if required <= 0:
        # No server price configured for this operation — nothing to enforce.
        return True, ""
    try:
        if resolve_asset_entry(network, asset) is not None:
            required_atomic = asset_to_atomic(required, asset, network)
            client_atomic = asset_to_atomic(client_amount_usd, asset, network)
        else:
            required_atomic = _to_atomic(required, asset)
            client_atomic = _to_atomic(client_amount_usd, asset)
    except (ValueError, TypeError):
        return False, "invalid amount/asset for pricing check"
    if client_atomic < required_atomic:
        return (
            False,
            f"underpayment: client supplied {client_amount_usd} USD, server requires {required} USD for '{operation}'",
        )
    return True, ""


def build_multi_asset_accepts(
    operation: str,
    resource: str = "",
    description: str = "",
    mime_type: str = "application/json",
) -> list[dict[str, Any]]:
    """Build the accepts list covering every quotable asset in SUPPORTED_ASSETS.

    Each option is keyed by CAIP-2 network id and quotes the USD-equivalent
    atomic amount for the operation's server-side price (per-network
    ``prices`` overrides in X402_PRICING apply). Assets without a configured
    USD-per-unit conversion are skipped — they cannot be quoted honestly.
    """
    price_info = X402_PRICING.get(operation, {"price_usd": 0.001, "description": operation})
    resource_url = resource or f"https://coffer.example/api/{operation}"
    accepts: list[dict[str, Any]] = []
    for network_id, assets in SUPPORTED_ASSETS.items():
        for asset, entry in assets.items():
            price_usd = resolve_price_usd(operation, network=network_id, asset=asset)
            unit_usd = usd_per_unit_for(asset)
            if not unit_usd or price_usd <= 0:
                continue
            amount = asset_to_atomic(price_usd, asset, network_id)
            pay_to = X402_NETWORK_WALLETS.get(network_id) or X402_WALLET
            accepts.append(
                {
                    "scheme": X402Scheme.EXACT.value,
                    "network": network_id,
                    "maxAmountRequired": str(amount),
                    "resource": resource_url,
                    "description": description or price_info.get("description", operation),
                    "mimeType": mime_type,
                    "payToAddress": pay_to,
                    "asset": asset,
                    "extra": {
                        "name": entry.get("name", asset),
                        "version": "2",
                        "operation": operation,
                        "caip2": network_id,
                        "verification": entry.get("verification", ""),
                        "contract": entry.get("contract", ""),
                        "decimals": entry.get("decimals", 6),
                        "priceUsd": price_usd,
                        "usdPerUnit": unit_usd,
                        "usdPerUnitSource": ("stablecoin" if asset in _STABLECOIN_ASSETS else "static-default"),
                    },
                }
            )
    return accepts


def build_payment_required(
    operation: str,
    resource: str = "",
    accepts: list[dict[str, Any]] | None = None,
    description: str = "",
    mime_type: str = "application/json",
    preferred_networks: list[str] | None = None,
    preferred_assets: list[str] | None = None,
    multi_asset: bool = False,
) -> dict[str, Any]:
    """Build a spec-compliant x402 PaymentRequired response.

    Returns a dict that should be:
    1. Returned as the body of the 402 response (JSON)
    2. Base64-encoded and put in the PAYMENT-REQUIRED header

    Args:
        operation: The operation being paid for (e.g., "scrape")
        resource: Full URL of the protected resource
        accepts: List of payment options (if None, generates defaults)
        description: Human-readable description
        mime_type: Expected response MIME type
        preferred_networks: Networks to prioritize in accepts
        preferred_assets: Assets to prioritize in accepts
        multi_asset: When True (and ``accepts`` is None) build options from
            the canonical SUPPORTED_ASSETS table — every quotable
            network/asset keyed by CAIP-2 network id — and attach the
            supported_assets/networks tables to the body so clients see the
            full multi-chain acceptance surface. Default False keeps the V1
            legacy single-asset shape.
    """
    price_info = X402_PRICING.get(operation, {"price_usd": 0.001, "description": operation})
    amount_atomic = _to_atomic(price_info["price_usd"], X402_DEFAULT_ASSET)
    resource_url = resource or f"https://coffer.example/api/{operation}"

    if accepts is None:
        if multi_asset:
            accepts = build_multi_asset_accepts(
                operation,
                resource=resource_url,
                description=description,
                mime_type=mime_type,
            )
        else:
            networks = preferred_networks or _default_networks_for_asset(X402_DEFAULT_ASSET)
            assets = preferred_assets or [X402_DEFAULT_ASSET]
            accepts = []
            for network in networks:
                for asset in assets:
                    if not _asset_on_network(asset, network):
                        continue
                    amount = _to_atomic(price_info["price_usd"], asset)
                    accepts.append(
                        {
                            "scheme": X402Scheme.EXACT.value,
                            "network": network,
                            "maxAmountRequired": str(amount),
                            "resource": resource_url,
                            "description": description or price_info.get("description", operation),
                            "mimeType": mime_type,
                            "payToAddress": X402_WALLET,
                            "asset": asset,
                            "extra": {"name": asset, "version": "2", "operation": operation},
                        }
                    )
        if not accepts:
            # Final fallback so we never return an empty accepts array
            accepts.append(
                {
                    "scheme": X402Scheme.EXACT.value,
                    "network": X402Network.BASE.value,
                    "maxAmountRequired": str(amount_atomic),
                    "resource": resource_url,
                    "description": description or price_info.get("description", operation),
                    "mimeType": mime_type,
                    "payToAddress": X402_WALLET,
                    "asset": X402_DEFAULT_ASSET,
                    "extra": {"name": X402_DEFAULT_ASSET, "version": "2", "operation": operation},
                }
            )

    body: dict[str, Any] = {
        "x402Version": 1,
        "accepts": accepts,
    }
    if multi_asset:
        # Multi-chain acceptance surface so agents can see every network/asset
        # they may pay with (additive — spec clients ignore unknown keys).
        body["networks"] = SUPPORTED_NETWORKS
        body["supported_assets"] = flatten_supported_assets()
    return body


# These return the pre-spec response format. Prefer the spec-compliant methods
# above in new code.


def _legacy_payment_request(
    operation: str,
    metadata: dict[str, Any] | None = None,
    price_usd: float = 0.001,
    payment_id: str = "",
) -> dict[str, Any]:
    price_info = X402_PRICING.get(operation, {"price_usd": price_usd, "description": operation})
    return {
        "x402Version": 1,
        "payment_id": payment_id or uuid.uuid4().hex[:12],
        "scheme": X402Scheme.EXACT.value,
        "network": X402_DEFAULT_NETWORK,
        "resource": f"https://coffer.example/api/{operation}",
        "operation": operation,
        "description": price_info.get("description", operation),
        "maxAmountRequired": str(int(price_info["price_usd"] * 1_000_000)),
        "asset": X402Asset.USDC.value,
        "payTo": X402_WALLET,
        "maxTimeoutSeconds": 60,
        "extra": metadata or {},
    }


def create_payment_request(operation: str, metadata: dict[str, Any] | None = None) -> dict[str, Any]:
    """Legacy: build a single-payment payment request dict.

    Returns the pre-spec shape used by /v1/x402/payment.
    """
    if operation not in X402_PRICING:
        return {"error": f"Unknown operation: {operation}"}
    req = _legacy_payment_request(operation, metadata=metadata)
    logger.info(
        "x402_payment_request_created",
        extra={"payment_id": req["payment_id"], "operation": operation},
    )
    return req


def require_payment_legacy(operation: str, metadata: dict[str, Any] | None = None) -> dict[str, Any]:
    """Legacy: build the pre-spec 402 response with X-Payment-Required header."""
    if operation not in X402_PRICING:
        return {"error": f"Unknown operation: {operation}"}
    return {
        "status_code": 402,
        "headers": {
            "X-Payment-Required": "true",
            "X-Payment-Options": "x402",
        },
        "body": {
            "error": "Payment Required",
            "x402": _legacy_payment_request(operation, metadata=metadata),
            "instructions": (
                "Send USDC payment to the wallet address, then call the API again with the X-Payment-Hash header."
            ),
        },
    }
