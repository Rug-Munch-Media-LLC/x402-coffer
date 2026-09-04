# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Rug Munch Media LLC
# --- facilitator_config.py: facilitator/environment configuration ---
from __future__ import annotations

import os
import pathlib
from dataclasses import dataclass, field


# Production MUST set X402_WALLET (canonical EVM payee). Legacy names
# X402_PAY_TO / X402_EVM_PAY_TO are honored as read-time aliases. There is NO
# hardcoded wallet default: unset resolves to "" and create_app()/config.py
# fails closed on an unconfigured wallet.
def _env_first(*names: str) -> str:
    """Canonical-first env alias resolution (first non-empty value wins)."""
    for name in names:
        val = os.getenv(name, "")
        if val:
            return val
    return ""


PAY_TO = _env_first("X402_WALLET", "X402_PAY_TO", "X402_EVM_PAY_TO").lower()
X402_TIMEOUT_SECONDS = float(os.getenv("X402_TIMEOUT_SECONDS", "15"))
X402_WALLET = os.getenv("X402_WALLET", PAY_TO)
X402_NETWORK_WALLETS = {
    "eip155:8453": os.getenv("X402_WALLET_BASE", X402_WALLET),
    "eip155:1": os.getenv("X402_WALLET_ETH", X402_WALLET),
    "eip155:42161": os.getenv("X402_WALLET_ARBITRUM", X402_WALLET),
    "eip155:10": os.getenv("X402_WALLET_OPTIMISM", X402_WALLET),
    "eip155:137": os.getenv("X402_WALLET_POLYGON", X402_WALLET),
    "eip155:84532": os.getenv("X402_WALLET_BASE_SEPOLIA", X402_WALLET),
    "eip155:11155111": os.getenv("X402_WALLET_ETH_SEPOLIA", X402_WALLET),
    "solana:mainnet": _env_first("X402_WALLET_SOLANA", "X402_SOL_PAY_TO", "X402_SOLANA_WALLET"),
    "tron:mainnet": _env_first("X402_WALLET_TRON", "X402_TRON_PAY_TO"),
}
X402_SELF_FACILITATE = os.getenv("X402_SELF_FACILITATE", "1").lower() in ("1", "true")
X402_SELF_FACILITATOR_KEY = os.getenv("X402_SELF_FACILITATOR_KEY", "")
X402_SELF_FACILITATOR_RPC = os.getenv("X402_SELF_FACILITATOR_RPC", "")
X402_OFFLINE_MODE = os.getenv("X402_OFFLINE_MODE", "0").lower() in ("1", "true")

# TTL for verified payment ids (mirrors x402-py ``PAYMENT_VALIDITY_SECONDS``).
PAYMENT_VALIDITY_SECONDS: int = int(os.getenv("X402_PAYMENT_TTL", "300"))

# Default asset/network for payments when none is supplied by the caller.
X402_DEFAULT_ASSET: str = os.getenv("X402_DEFAULT_ASSET", "usdc")
X402_DEFAULT_NETWORK: str = os.getenv("X402_DEFAULT_NETWORK", "base")

# Data directory for payment/settlement record persistence. Override with
# X402_DATA_DIR; defaults to ~/.x402 so the package works out of the box.
X402_DIR: pathlib.Path = pathlib.Path(os.getenv("X402_DATA_DIR", str(pathlib.Path.home() / ".x402")))
X402_DIR.mkdir(parents=True, exist_ok=True)

# Default facilitator URL (empty = use the configured facilitator router).
X402_FACILITATOR_URL: str = os.getenv("X402_FACILITATOR_URL", "")


@dataclass
class FacilitatorConfig:
    """Configuration for a single x402 facilitator."""

    name: str
    verify_url: str
    settle_url: str
    priority: int = 0
    enabled: bool = True
    api_key: str = field(default="", repr=False)
    timeout_seconds: float = X402_TIMEOUT_SECONDS


def _env_bool(name: str, default: str) -> bool:
    return os.getenv(name, default).lower() in ("1", "true")


def _check_secret_permissions() -> None:
    """Warn if secret files are world-readable."""
    import os, stat

    wallet_file = os.getenv("X402_WALLET_FILE", "")
    if wallet_file and os.path.exists(wallet_file):
        mode = os.stat(wallet_file).st_mode
        if mode & stat.S_IWGRP or mode & stat.S_IWOTH:
            import logging

            logging.getLogger(__name__).warning(
                "x402_secret_file_world_readable", extra={"file": wallet_file}
            )


_check_secret_permissions()


# Default external facilitators (priority order, env-overridable).
DEFAULT_FACILITATORS: list[FacilitatorConfig] = [
    FacilitatorConfig(
        name="coinbase",
        verify_url=os.getenv("X402_COINBASE_VERIFY", "https://x402.org/facilitator/verify"),
        settle_url=os.getenv("X402_COINBASE_SETTLE", "https://x402.org/facilitator/settle"),
        priority=int(os.getenv("X402_COINBASE_PRIORITY", "1")),
        enabled=_env_bool("X402_COINBASE_ENABLED", "true"),
        api_key=os.getenv("X402_COINBASE_API_KEY", ""),
    ),
    FacilitatorConfig(
        name="payai",
        verify_url=os.getenv("X402_PAYAI_VERIFY", "https://facilitator.payai.network/verify"),
        settle_url=os.getenv("X402_PAYAI_SETTLE", "https://facilitator.payai.network/settle"),
        priority=int(os.getenv("X402_PAYAI_PRIORITY", "2")),
        enabled=_env_bool("X402_PAYAI_ENABLED", "true"),
    ),
    FacilitatorConfig(
        name="cloudflare",
        verify_url=os.getenv("X402_CF_VERIFY", ""),
        settle_url=os.getenv("X402_CF_SETTLE", ""),
        priority=int(os.getenv("X402_CF_PRIORITY", "3")),
        enabled=_env_bool("X402_CF_ENABLED", "false"),
    ),
]
