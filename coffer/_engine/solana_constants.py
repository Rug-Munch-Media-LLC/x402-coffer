"""x402 — Solana verifier constants (mints, RPC defaults, program ids).

Public constants shared by the Solana SPL payment verifier modules.
"""

from __future__ import annotations

# Solana mainnet USDC mint (Circle) and USDT mint (Tether). Public constants.
SOLANA_USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
SOLANA_USDT_MINT = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"

#: Supported SPL assets -> mint (both 6-decimals stablecoins; the decimals are
#: still read from the transaction itself, never assumed).
SPL_ASSET_MINTS: dict[str, str] = {
    "USDC": SOLANA_USDC_MINT,
    "USDT": SOLANA_USDT_MINT,
}

# Default public Solana mainnet RPC (override per call / env in production).
DEFAULT_SOLANA_RPC_URL = "https://api.mainnet-beta.solana.com"

SOLANA_RPC_TIMEOUT_SECONDS = 15.0

# Explicit commitment for getTransaction. "confirmed" = supermajority stake
# voted — the standard acceptance level for payments. [REVIEW] Use "finalized"
# for high-value payments where slower acceptance is tolerable: with
# "confirmed" a rare fork rollback cannot be ruled out entirely.
SOLANA_RPC_COMMITMENT = "confirmed"

# SPL Token program id — sanity-check the parsed instruction came from the
# canonical token program, not a custom program with a spoofed "parsed".
# Public constant, not a secret.
SPL_TOKEN_PROGRAM_ID = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"

# Only transferChecked carries the mint in jsonParsed output; a plain SPL
# ``transfer`` cannot be attributed to USDC without extra account-resolution.
VERIFIABLE_TYPES = ("transferChecked",)
