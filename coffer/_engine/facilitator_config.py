# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Rug Munch Media LLC
# --- facilitator_config.py: facilitator/environment configuration ---
from __future__ import annotations

import os
from dataclasses import dataclass, field

# Production MUST set X402_PAY_TO / X402_WALLET. The default below is a
# legacy placeholder kept for backward-compat with existing tests; create_app()
# fails closed if the wallet is still this sentinel.
PAY_TO = os.getenv("X402_PAY_TO", "0x1E3AC01d0fdb976179790BDD02823196A92705C9").lower()
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
    "solana:mainnet": os.getenv("X402_WALLET_SOLANA", ""),
    "tron:mainnet": os.getenv("X402_WALLET_TRON", ""),
}
X402_SELF_FACILITATE = os.getenv("X402_SELF_FACILITATE", "1").lower() in ("1", "true")
X402_SELF_FACILITATOR_KEY = os.getenv("X402_SELF_FACILITATOR_KEY", "")
X402_SELF_FACILITATOR_RPC = os.getenv("X402_SELF_FACILITATOR_RPC", "")
X402_OFFLINE_MODE = os.getenv("X402_OFFLINE_MODE", "0").lower() in ("1", "true")


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
