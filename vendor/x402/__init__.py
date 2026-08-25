"""x402 — HTTP 402 Payment Required protocol implementation.

Implements the x402 v1 spec from https://github.com/coinbase/x402

Standalone package: configuration is env-var-first with an optional
pluggable secret resolver — no external project modules required. Domain
code lives in:

- ``x402.scheme``      — scheme/offer/pricing helpers (+ legacy payment requests)
- ``x402.facilitator`` — FacilitatorRouter + EIP-7702 self-verify
- ``x402.handler``     — X402Handler (verify/settle orchestration)
- ``x402.batch``       — batch payment helpers
- ``x402.solana_verifier`` — Solana SPL USDC on-chain verifier (getTransaction
  jsonParsed, httpx + stdlib — no Solana SDK)
- ``x402.middleware``  — framework adapters (ASGI/WSGI/Flask + require_payment)

Configuration: every setting reads ``X402_*`` env vars (canonical, no
legacy aliases). Wallets and URLs may also come from a secret resolver
registered via ``set_secret_resolver`` (only consulted when the env var is
unset and ``X402_SECRET_RESOLVER`` is set). Payment records and nonce
persistence live under ``X402_DATA_DIR`` (default ``~/.x402``).

Constants are kept at the package root (not in a submodule) so
``importlib.reload(x402)`` recomputes them after tests patch the
environment. The public import surface is flat: ``from x402 import X``
works for every public name re-exported below.

Standards compliance:
- HTTP 402 status code with PaymentRequired body
- PAYMENT-REQUIRED header (Base64-encoded JSON)
- PAYMENT-SIGNATURE header (Base64-encoded JSON, signed)
- PAYMENT-RESPONSE header on success (Base64-encoded settlement receipt)
- Multi-chain support: base, solana, ethereum, polygon, arbitrum, optimism, bnb,
  avalanche, base-sepolia, tron, bitcoin, litecoin, dogecoin
- Multi-asset: USDC, USDT, native
- Canonical multi-asset acceptance table (``SUPPORTED_ASSETS``) keyed by
  CAIP-2 network id + asset: EVM ERC-20 (USDC/USDT/ETH/ARB/WETH), Solana SPL
  (USDC/USDT/SOL), TRON TRC-20 (USDT/TRX). EVM settlement verification is
  real (EIP-7702 self-verify + facilitators); Solana is real too (SPL RPC
  verifier — getTransaction jsonParsed USDC transferChecked); TRON is a
  [REVIEW] scaffold (structure/price/asset validation only — see
  VERIFICATION_STATUS).
- Multiple facilitators: coinbase, payai, cloudflare, eip-7702
- Smart facilitator router with auto-fallback
- Self-hosted facilitator (``x402.self_facilitator``): EIP-3009 verify +
  settlement broadcast on your own infra — no external facilitator /
  Coinbase CDP. Opt-in via ``X402_SELF_FACILITATE=1``
  (+ ``X402_FACILITATOR_KEY``, ``X402_FACILITATOR_RPC``).
"""
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Rug Munch Media LLC
#
# Standalone x402 implementation — https://git.rugmunch.io/RugMunchMedia/x402-py
# Licensed under MIT. See LICENSE.

from __future__ import annotations

import logging
import os
import pathlib
from collections.abc import Callable

__version__ = "1.0.0"

logger = logging.getLogger(__name__)

# Data directory for payment records / nonce persistence. Override with
# X402_DATA_DIR; defaults to ~/.x402 so the package works out of the box.
X402_DATA_DIR: pathlib.Path = pathlib.Path(os.getenv("X402_DATA_DIR", str(pathlib.Path.home() / ".x402")))
X402_DIR = X402_DATA_DIR
X402_DIR.mkdir(parents=True, exist_ok=True)


def _env_flag(name: str) -> bool:
    """Parse a common truthy env-var flag."""
    return os.getenv(name, "false").lower() in ("1", "true", "yes")


def _env(name: str, default: str = "") -> str:
    """Read a canonical ``X402_*`` env var; empty values fall through to the default."""
    return os.getenv(name) or default


_secret_resolver: Callable[[str], str] | None = None


def set_secret_resolver(resolver: Callable[[str], str] | None) -> None:
    """Register an optional pluggable secret resolver.

    The resolver is a callable invoked with the logical secret name
    (e.g. ``"x402_wallet"``) when the corresponding env var is unset AND
    ``X402_SECRET_RESOLVER`` is set (its value is advisory — e.g. the
    resolver backend's name). It must return ``""`` for unknown secrets.
    Exceptions are logged and treated as "unknown secret" so a broken
    resolver can never break package import. Pass ``None`` to unregister.
    """
    global _secret_resolver
    _secret_resolver = resolver


def _get_secret(name: str, env_var: str, default: str = "") -> str:
    """Resolve a secret: env var first, then optional resolver callback, then default."""
    val = os.getenv(env_var, "")
    if not val and os.getenv("X402_SECRET_RESOLVER"):
        resolver = _secret_resolver
        if resolver is not None:
            try:
                val = resolver(name) or ""
            except Exception:  # resolver must never break import/config
                logger.warning("x402_secret_resolver_failed", extra={"secret_name": name})
    return val or default


# Secrets resolve env-var first; a registered resolver only fills gaps.
#   X402_WALLET=0x...                    # canonical env var
#   set_secret_resolver(my_lookup)       # optional pluggable backend
#
# Kept at the package root (not in a submodule) so importlib.reload(x402)
# recomputes the values after tests patch the environment and reload.
# Sentinel payTo address used until an operator configures a real wallet.
# Verification fails closed while this placeholder is in effect.
X402_UNCONFIGURED_WALLET: str = "coffer-unconfigured-wallet"

X402_WALLET: str = _get_secret("x402_wallet", "X402_WALLET") or X402_UNCONFIGURED_WALLET
X402_FACILITATOR_URL: str = (
    _get_secret("x402_facilitator", "X402_FACILITATOR_URL") or "https://x402.org/facilitator"
)

# NOTE: literal defaults are equal to X402Network.BASE.value and
# X402Asset.USDC.value; the enums live in x402.scheme which is imported after
# this config block so the constants stay reload-recomputable.
X402_DEFAULT_NETWORK: str = _env("X402_NETWORK", "base")
X402_DEFAULT_ASSET: str = _env("X402_ASSET", "USDC")

# Per-network receiving wallets (CAIP-2 keyed). A Solana/TRON receiving
# address is NOT the same as the EVM wallet, so operators MUST configure
# these before accepting real payments on those chains. Falls back to
# X402_WALLET when unset — acceptable for EVM, [REVIEW] invalid for
# solana/tron until configured.
X402_NETWORK_WALLETS: dict[str, str] = {
    "eip155:8453": _env("X402_WALLET_BASE"),
    "eip155:1": _env("X402_WALLET_ETHEREUM"),
    "eip155:42161": _env("X402_WALLET_ARBITRUM"),
    "eip155:10": _env("X402_WALLET_OPTIMISM"),
    "eip155:137": _env("X402_WALLET_POLYGON"),
    "solana:mainnet": _env("X402_WALLET_SOLANA"),
    "tron:mainnet": _env("X402_WALLET_TRON"),
}

# Offline mode is a DEV-ONLY bypass: it makes every verify/settle succeed
# without touching a facilitator. It must NEVER be active in production, so
# it requires BOTH X402_OFFLINE=true AND a debug flag (X402_DEBUG / DEBUG).
# Without the debug gate a bare offline flag is ignored loudly.
X402_DEBUG_MODE: bool = _env_flag("X402_DEBUG") or _env_flag("DEBUG")
X402_OFFLINE_MODE: bool = _env_flag("X402_OFFLINE") and X402_DEBUG_MODE
if _env_flag("X402_OFFLINE") and not X402_DEBUG_MODE:
    logger.warning(
        "X402_OFFLINE=true IGNORED: offline mode requires X402_DEBUG=true (or DEBUG=true) "
        "and is intended for local development only."
    )
if X402_OFFLINE_MODE:
    logger.warning(
        "x402_offline_mode_active: ALL payment verification is BYPASSED — "
        "dev-only setting. DO NOT run with X402_OFFLINE=true in production."
    )
X402_TIMEOUT_SECONDS: float = float(_env("X402_TIMEOUT", "15"))

# How long a verified payment_id stays valid for replay (access reuse).
# After this window the client must pay again. Payments recorded before this
# setting existed (no expires_at) are treated as expired (fail closed).
PAYMENT_VALIDITY_SECONDS: int = int(_env("X402_PAYMENT_TTL", "3600"))

# EIP-7702 self-verify RPC endpoints (public defaults, override via env)
EIP7702_RPC: dict[str, str] = {
    "base": _env("X402_BASE_RPC", "https://mainnet.base.org"),
    "ethereum": _env("X402_ETH_RPC", "https://eth.llamarpc.com"),
    "polygon": _env("X402_POLYGON_RPC", "https://polygon.drpc.org"),
    "arbitrum": _env("X402_ARBITRUM_RPC", "https://arb1.arbitrum.io/rpc"),
    "optimism": _env("X402_OPTIMISM_RPC", "https://mainnet.optimism.io"),
    "bnb": _env("X402_BNB_RPC", "https://bsc-dataseed.binance.org"),
    "avalanche": _env("X402_AVALANCHE_RPC", "https://api.avax.network/ext/bc/C/rpc"),
    "base-sepolia": _env("X402_BASE_SEPOLIA_RPC", "https://sepolia.base.org"),
}

# Canonical ERC-20 token addresses used to validate EIP-7702 self-verify txs.
# A successful receipt alone is NOT proof of payment — the tx must have been
# sent TO one of these token contracts with a transfer() to our wallet for at
# least the server-side price. Addresses are well-known mainnet addresses.
#
# NOTE — Base USDT discrepancy: this V1 table lists
# 0x50c5725949A6F0c72E6C4a641F24049A917DB0Cb, while the canonical
# SUPPORTED_ASSETS table (x402/scheme.py) uses the official bridged Tether USD
# 0xfde4C96c8593536E31F229EA8f37b2ADa2699bb2. The V1 table is intentionally
# NOT churned for compatibility; new code should read
# SUPPORTED_ASSETS["eip155:8453"]["USDT"]["contract"].
EIP7702_TOKEN_ADDRESSES: dict[str, dict[str, str]] = {
    "base": {
        "USDC": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
        "USDT": "0x50c5725949A6F0c72E6C4a641F24049A917DB0Cb",
    },
    "ethereum": {
        "USDC": "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
        "USDT": "0xdAC17F958D2ee523a2206206994597C13D831ec7",
    },
    "polygon": {
        "USDC": "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359",
        "USDT": "0xc2132D05D31c914a87C6611C10748AEb04B58e8F",
    },
    "arbitrum": {
        "USDC": "0xaf88d065e77c8cC2239327C5EDb3A432268e5831",
        "USDT": "0xFd086bC7CD5C481DCC9C85ebE478A1C0b69FCbb9",
    },
    "optimism": {
        "USDC": "0x0b2C639c533813f4Aa9D7837CAf62653d097F85F",
        "USDT": "0x94b008aA00579c1307B0EF2c499aD98a8ce58e58",
    },
    "bnb": {
        "USDC": "0x8AC76a51cc950d9822D68b83fE1Ad97B32Cd580d",
        "USDT": "0x55d398326f99059fF775485246999027B3197955",
    },
    "avalanche": {
        "USDC": "0xB97EF9Ef8734C71904D8002F8b6Bc66Dd9c48a6E",
        "USDT": "0x9702230A8Ea53601f5cD2dc00fDBc13d4dF4A8c7",
    },
    "base-sepolia": {
        "USDC": "0x036CbD53842c5426634e7929541eC2318f3dCF7e",
    },
}

# Self-hosted x402 facilitator (no external facilitator / Coinbase CDP).
# Opt-in: X402_SELF_FACILITATE=1 switches the FacilitatorRouter to the
# local SelfFacilitator. X402_FACILITATOR_KEY is the EVM private key used
# to pay gas when broadcasting settlement; X402_FACILITATOR_RPC is the
# JSON-RPC endpoint. See x402/self_facilitator.py.
X402_SELF_FACILITATE: bool = _env_flag("X402_SELF_FACILITATE")
X402_SELF_FACILITATOR_KEY: str = _get_secret("x402_facilitator_key", "X402_FACILITATOR_KEY")
X402_SELF_FACILITATOR_RPC: str = _env("X402_FACILITATOR_RPC")
if X402_SELF_FACILITATE and not (X402_SELF_FACILITATOR_KEY and X402_SELF_FACILITATOR_RPC):
    logger.warning(
        "X402_SELF_FACILITATE=1 but X402_FACILITATOR_KEY and/or "
        "X402_FACILITATOR_RPC are missing: verify works, settle will be unavailable."
    )

# Re-export the full public surface. Submodules import their config from this
# package root (constants above), so these imports must come AFTER the config
# block to avoid circular initialization.
from .batch import (  # noqa: E402
    _batch_lock,
    _batch_payments,
    create_batch_payment,
    is_batch_paid,
    verify_batch_payment,
)
from .facilitator import (  # noqa: E402
    DEFAULT_FACILITATORS,
    ERC20_TRANSFER_SELECTOR,
    FacilitatorConfig,
    FacilitatorRouter,
    _facilitator_router,
    get_facilitator_router,
    register_facilitator,
    set_facilitator_router,
)
from .handler import X402Handler  # noqa: E402
from .scheme import (  # noqa: E402
    _NATIVE_ASSETS,
    ASSET_DECIMALS,
    CAIP2_NETWORK_IDS,
    COINGECKO_ASSET_IDS,
    SUPPORTED_ASSETS,
    SUPPORTED_NETWORKS,
    VERIFICATION_STATUS,
    X402_ASSET_USD_PER_UNIT,
    X402_ORACLE_SOURCE,
    X402_ORACLE_TTL_SECONDS,
    X402_ORACLE_URL,
    X402_PRICING,
    AssetUsdQuote,
    X402Asset,
    X402Network,
    X402Scheme,
    _asset_on_network,
    _decode_payment_signature,
    _default_networks_for_asset,
    _encode_payment_required_body,
    _encode_payment_response,
    _from_atomic,
    _legacy_payment_request,
    _to_atomic,
    asset_to_atomic,
    build_multi_asset_accepts,
    build_payment_required,
    clear_price_cache,
    create_payment_request,
    flatten_supported_assets,
    get_asset_usd_price,
    is_asset_supported,
    network_family,
    network_verification_status,
    normalize_network_id,
    oracle_source_label,
    probe_oracle,
    require_payment_legacy,
    resolve_asset_entry,
    resolve_price_usd,
    usd_per_unit_for,
    validate_client_amount,
)
from .self_facilitator import (  # noqa: E402
    CHAIN_ID_BY_NETWORK,
    NonceStore,
    SelfFacilitator,
    SettleErrorCode,
    SettleOutcome,
    VerifyResult,
)
from .solana_verifier import (  # noqa: E402
    DEFAULT_SOLANA_RPC_URL,
    SOLANA_RPC_TIMEOUT_SECONDS,
    SOLANA_USDC_MINT,
    verify_solana_payment,
)

__all__ = [
    "ASSET_DECIMALS",
    "CAIP2_NETWORK_IDS",
    "CHAIN_ID_BY_NETWORK",
    "COINGECKO_ASSET_IDS",
    "DEFAULT_FACILITATORS",
    "DEFAULT_SOLANA_RPC_URL",
    "EIP7702_RPC",
    "EIP7702_TOKEN_ADDRESSES",
    "ERC20_TRANSFER_SELECTOR",
    "PAYMENT_VALIDITY_SECONDS",
    "SOLANA_RPC_TIMEOUT_SECONDS",
    "SOLANA_USDC_MINT",
    "SUPPORTED_ASSETS",
    "SUPPORTED_NETWORKS",
    "VERIFICATION_STATUS",
    "X402_ASSET_USD_PER_UNIT",
    "X402_DATA_DIR",
    "X402_DEBUG_MODE",
    "X402_DEFAULT_ASSET",
    "X402_DEFAULT_NETWORK",
    "X402_DIR",
    "X402_FACILITATOR_URL",
    "X402_NETWORK_WALLETS",
    "X402_OFFLINE_MODE",
    "X402_ORACLE_SOURCE",
    "X402_ORACLE_TTL_SECONDS",
    "X402_ORACLE_URL",
    "X402_PRICING",
    "X402_SELF_FACILITATE",
    "X402_SELF_FACILITATOR_KEY",
    "X402_SELF_FACILITATOR_RPC",
    "X402_TIMEOUT_SECONDS",
    "X402_UNCONFIGURED_WALLET",
    "X402_WALLET",
    "_NATIVE_ASSETS",
    "AssetUsdQuote",
    "FacilitatorConfig",
    "FacilitatorRouter",
    "NonceStore",
    "SelfFacilitator",
    "SettleErrorCode",
    "SettleOutcome",
    "VerifyResult",
    "X402Asset",
    "X402Handler",
    "X402Network",
    "X402Scheme",
    "_asset_on_network",
    "_batch_lock",
    "_batch_payments",
    "_decode_payment_signature",
    "_default_networks_for_asset",
    "_encode_payment_required_body",
    "_encode_payment_response",
    "_env_flag",
    "_env",
    "_facilitator_router",
    "_from_atomic",
    "_get_secret",
    "_legacy_payment_request",
    "_to_atomic",
    "asset_to_atomic",
    "build_multi_asset_accepts",
    "build_payment_required",
    "clear_price_cache",
    "create_batch_payment",
    "create_payment_request",
    "flatten_supported_assets",
    "get_asset_usd_price",
    "get_facilitator_router",
    "is_asset_supported",
    "is_batch_paid",
    "logger",
    "network_family",
    "network_verification_status",
    "normalize_network_id",
    "oracle_source_label",
    "probe_oracle",
    "register_facilitator",
    "require_payment_legacy",
    "resolve_asset_entry",
    "resolve_price_usd",
    "set_facilitator_router",
    "set_secret_resolver",
    "usd_per_unit_for",
    "validate_client_amount",
    "verify_batch_payment",
    "verify_solana_payment",
]
