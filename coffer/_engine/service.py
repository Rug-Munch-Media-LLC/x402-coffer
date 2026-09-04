"""x402 — facilitator router, self-hosted verifiers, server-authoritative pricing.

This module is now a thin re-export FACADE. The security-critical payment
verification layer has been split into submodules (each ≤500 lines):

- ``replay.py``            — ReplayStore (Redis SETNX nonce burn) + deps.
- ``pricing.py``           — network/asset tables, server-authoritative pricing,
                             honest VERIFICATION_STATUS.
- ``facilitator_config.py`` — environment configuration, FacilitatorConfig,
                             DEFAULT_FACILITATORS.
- ``self_facilitator.py``   — self-hosted EIP-3009 SelfFacilitator.
- ``evm_verify.py``        — EIP-7702 receipt fallback (verify/matches/rpc_call).
- ``external_verify.py``   — external-facilitator verify + settlement
                             normalization (harbours the H3/M2 honesty checks).
- ``facilitator_router.py`` — FacilitatorRouter orchestration + global accessors.

``service.py`` keeps filename so every existing
``from coffer._engine.service import ...`` import stays valid.
"""

# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Rug Munch Media LLC

from .facilitator_config import (
    DEFAULT_FACILITATORS,
    X402_NETWORK_WALLETS,
    X402_WALLET,
    FacilitatorConfig,
)
from .facilitator_router import (
    FacilitatorRouter,
    get_facilitator_router,
    register_facilitator,
    set_facilitator_router,
)
from .models import VerifyResult
from .pricing import (
    VERIFICATION_STATUS,
    X402_PRICING,
    asset_to_atomic,
    normalize_network_id,
    resolve_price_usd,
    validate_client_amount,
)
from .replay import REPLAY_HTTP_STATUS, ReplayStore
from .self_facilitator import SelfFacilitator

__all__ = [
    "DEFAULT_FACILITATORS",
    "REPLAY_HTTP_STATUS",
    "VERIFICATION_STATUS",
    "X402_NETWORK_WALLETS",
    "X402_PRICING",
    "X402_WALLET",
    "FacilitatorConfig",
    "FacilitatorRouter",
    "ReplayStore",
    "SelfFacilitator",
    "VerifyResult",
    "asset_to_atomic",
    "get_facilitator_router",
    "normalize_network_id",
    "register_facilitator",
    "resolve_price_usd",
    "set_facilitator_router",
    "validate_client_amount",
]
