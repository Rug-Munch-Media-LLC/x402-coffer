"""x402 — Solana SPL stablecoin payment verifier (USDC / USDT).

Thin re-export facade. The implementation lives in focused modules:

- ``solana_constants`` — mints, RPC defaults, program ids
- ``solana_parse``     — jsonParsed transaction parsing helpers
- ``solana_result``    — stable success/failure result builders
- ``solana_transfer``  — RPC transport + ``verify_solana_payment``

Reference implementation: x402-py solana_verifier (MIT, Rug Munch
Media LLC), extended for production completeness:

- SPL ``transferChecked`` destinations are TOKEN ACCOUNTS (associated token
  accounts), not wallet addresses. Comparing ``info.destination`` against the
  merchant wallet alone would reject every genuine payment, so the merchant's
  token accounts are resolved from ``meta.pre/postTokenBalances`` (which carry
  the account ``owner``) and accepted as destinations too.
- Explicit ``commitment`` on ``getTransaction`` (default ``confirmed``) — the
  node default is not relied upon.
- Ordered RPC fallback for transport-level failures. An authoritative JSON-RPC
  answer (including ``error`` / null ``result``) is final and never retried
  against another RPC.
"""

from __future__ import annotations

from .solana_constants import (
    DEFAULT_SOLANA_RPC_URL,
    SOLANA_RPC_COMMITMENT,
    SOLANA_RPC_TIMEOUT_SECONDS,
    SOLANA_USDC_MINT,
    SOLANA_USDT_MINT,
    SPL_ASSET_MINTS,
    SPL_TOKEN_PROGRAM_ID,
    VERIFIABLE_TYPES,
)
from .solana_parse import (  # noqa: F401
    _amount_usd_value,
    _balance_address,
    _confirmed_at,
    _iter_spl_instructions,
    _owner_token_accounts,
    _parse_amount,
)
from .solana_result import _failure, _success  # noqa: F401
from .solana_transfer import _post_rpc, verify_solana_payment  # noqa: F401

__all__ = [
    "DEFAULT_SOLANA_RPC_URL",
    "SOLANA_RPC_COMMITMENT",
    "SOLANA_RPC_TIMEOUT_SECONDS",
    "SOLANA_USDC_MINT",
    "SOLANA_USDT_MINT",
    "SPL_ASSET_MINTS",
    "SPL_TOKEN_PROGRAM_ID",
    "VERIFIABLE_TYPES",
    "verify_solana_payment",
]
