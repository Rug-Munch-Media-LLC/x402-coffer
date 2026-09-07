"""Wallet management — configure, validate, and inspect receiving payment wallets.

This module provides the tools for operators to manage their per-chain receiving
addresses: validation, masking, status reporting, and an interactive setup wizard.

Usage::

    from coffer._engine.wallets import load_wallets, validate_wallet, mask_wallet

    config = load_wallets()
    print(mask_wallet(config.wallet_base))  # 0x1234...5678
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any

__all__ = [
    "CHAIN_INFO",
    "WalletConfig",
    "get_configured_chains",
    "get_unconfigured_chains",
    "load_wallets",
    "mask_wallet",
    "validate_wallet",
    "wallet_status",
]

# ── Chain registry ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ChainInfo:
    """Metadata for a supported payment chain."""

    key: str  # config field key (e.g. "wallet_base")
    name: str  # display name (e.g. "Base")
    env_var: str  # primary env var (e.g. "X402_WALLET_BASE")
    chain_type: str  # "evm" | "solana" | "tron"
    caip2: str  # CAIP-2 chain id (e.g. "eip155:8453")


CHAIN_INFO: dict[str, ChainInfo] = {
    "base": ChainInfo("wallet_base", "Base", "X402_WALLET_BASE", "evm", "eip155:8453"),
    "ethereum": ChainInfo("wallet_eth", "Ethereum", "X402_WALLET_ETH", "evm", "eip155:1"),
    "arbitrum": ChainInfo("wallet_arbitrum", "Arbitrum", "X402_WALLET_ARBITRUM", "evm", "eip155:42161"),
    "optimism": ChainInfo("wallet_optimism", "Optimism", "X402_WALLET_OPTIMISM", "evm", "eip155:10"),
    "polygon": ChainInfo("wallet_polygon", "Polygon", "X402_WALLET_POLYGON", "evm", "eip155:137"),
    "solana": ChainInfo("wallet_solana", "Solana", "X402_WALLET_SOLANA", "solana", "solana:mainnet"),
    "tron": ChainInfo("wallet_tron", "TRON", "X402_WALLET_TRON", "tron", "tron:mainnet"),
}

_UNCONFIGURED_MARKERS = {"", "coffer-unconfigured-wallet", "pry-default-wallet", "pry-default"}

# ── Wallet config ─────────────────────────────────────────────────────────────


@dataclass
class WalletConfig:
    """Per-chain receiving wallet addresses loaded from environment."""

    wallet_base: str = ""
    wallet_eth: str = ""
    wallet_arbitrum: str = ""
    wallet_optimism: str = ""
    wallet_polygon: str = ""
    wallet_solana: str = ""
    wallet_tron: str = ""

    def get(self, chain: str) -> str:
        """Get wallet address for a chain key."""
        info = CHAIN_INFO.get(chain)
        if not info:
            return ""
        return getattr(self, info.key, "")

    def is_configured(self, chain: str) -> bool:
        """Check if a chain has a real (non-sentinel) wallet configured."""
        addr = self.get(chain)
        return bool(addr) and addr not in _UNCONFIGURED_MARKERS


def load_wallets() -> WalletConfig:
    """Load wallet addresses from environment variables."""

    def _env(key: str, *fallbacks: str) -> str:
        for k in (key, *fallbacks):
            v = os.environ.get(k, "").strip()
            if v:
                return v
        return ""

    return WalletConfig(
        wallet_base=_env("X402_WALLET_BASE").lower(),
        wallet_eth=_env("X402_WALLET_ETH").lower(),
        wallet_arbitrum=_env("X402_WALLET_ARBITRUM").lower(),
        wallet_optimism=_env("X402_WALLET_OPTIMISM").lower(),
        wallet_polygon=_env("X402_WALLET_POLYGON").lower(),
        wallet_solana=_env("X402_WALLET_SOLANA", "X402_SOL_PAY_TO", "X402_SOLANA_WALLET"),
        wallet_tron=_env("X402_WALLET_TRON", "X402_TRON_PAY_TO"),
    )


# ── Validation ─────────────────────────────────────────────────────────────────

_EVM_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
_TRON_RE = re.compile(r"^T[A-Za-z0-9]{33}$")
# Solana base58: 32-44 chars, no 0/O/I/l
_BASE58_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")


def validate_wallet(address: str, chain: str) -> dict[str, Any]:
    """Validate a wallet address for a given chain.

    Returns: {"valid": bool, "chain": str, "address": str, "error": str | None}
    """
    info = CHAIN_INFO.get(chain)
    if not info:
        return {"valid": False, "chain": chain, "address": address, "error": f"unknown chain: {chain}"}

    addr = address.strip()
    if not addr or addr in _UNCONFIGURED_MARKERS:
        return {"valid": False, "chain": chain, "address": address, "error": "empty or sentinel address"}

    if info.chain_type == "evm":
        if not _EVM_RE.match(addr):
            return {
                "valid": False,
                "chain": chain,
                "address": address,
                "error": "EVM address must be 0x + 40 hex chars",
            }
        # Checksum validation (best-effort)
        try:
            from eth_utils import is_checksum_address  # type: ignore

            # Not all addresses are checksummed — only flag if mixed case
            if not is_checksum_address(addr) and addr != addr.lower() and addr != addr.upper():
                return {
                    "valid": False,
                    "chain": chain,
                    "address": address,
                    "error": "invalid EIP-55 checksum",
                }
        except ImportError:
            pass  # eth_utils not installed — format check is sufficient
        return {"valid": True, "chain": chain, "address": addr, "error": None}

    elif info.chain_type == "solana":
        if not _BASE58_RE.match(addr):
            return {
                "valid": False,
                "chain": chain,
                "address": address,
                "error": "Solana address must be 32-44 base58 chars",
            }
        return {"valid": True, "chain": chain, "address": addr, "error": None}

    elif info.chain_type == "tron":
        if not _TRON_RE.match(addr):
            return {
                "valid": False,
                "chain": chain,
                "address": address,
                "error": "TRON address must start with T + 33 chars",
            }
        return {"valid": True, "chain": chain, "address": addr, "error": None}

    return {"valid": False, "chain": chain, "address": address, "error": "unhandled chain type"}


# ── Masking ────────────────────────────────────────────────────────────────────


def mask_wallet(address: str) -> str:
    """Mask a wallet address: 0x1234...5678."""
    if not address or address in _UNCONFIGURED_MARKERS:
        return "(unconfigured)"
    if len(address) <= 12:
        return address[:4] + "..." + address[-4:]
    return address[:6] + "..." + address[-4:]


# ── Status ──────────────────────────────────────────────────────────────────────


def get_configured_chains(config: WalletConfig | None = None) -> list[str]:
    """Return list of chain keys with configured wallets."""
    if config is None:
        config = load_wallets()
    return [chain for chain in CHAIN_INFO if config.is_configured(chain)]


def get_unconfigured_chains(config: WalletConfig | None = None) -> list[str]:
    """Return list of chain keys WITHOUT configured wallets."""
    if config is None:
        config = load_wallets()
    return [chain for chain in CHAIN_INFO if not config.is_configured(chain)]


def wallet_status(config: WalletConfig | None = None) -> dict[str, Any]:
    """Return honest wallet configuration status for all chains."""
    if config is None:
        config = load_wallets()
    configured = get_configured_chains(config)
    unconfigured = get_unconfigured_chains(config)
    return {
        "configured": {
            chain: {
                "name": CHAIN_INFO[chain].name,
                "caip2": CHAIN_INFO[chain].caip2,
                "address_masked": mask_wallet(config.get(chain)),
                "valid": validate_wallet(config.get(chain), chain)["valid"],
            }
            for chain in configured
        },
        "unconfigured": {
            chain: {
                "name": CHAIN_INFO[chain].name,
                "caip2": CHAIN_INFO[chain].caip2,
                "env_var": CHAIN_INFO[chain].env_var,
            }
            for chain in unconfigured
        },
        "total_chains": len(CHAIN_INFO),
        "configured_count": len(configured),
        "unconfigured_count": len(unconfigured),
        "ready": len(configured) > 0,
    }
