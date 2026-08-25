"""x402 — Solana SPL stablecoin payment verifier (USDC / USDT).

Mirror of ``/srv/work/repos/x402-py/x402/solana_verifier.py`` (MIT, Rug Munch
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

Verifies a Solana SPL payment on-chain by querying the Solana RPC
``getTransaction`` with ``encoding: "jsonParsed"`` and walking the parsed
``splToken`` ``transferChecked`` instructions — httpx + stdlib only, no Solana
SDK. The verifier NEVER raises; every failure returns a stable dict.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

import httpx

log = logging.getLogger(__name__)

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
SPL_TOKEN_PROGRAM_ID = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"  # noqa: S105

# Only transferChecked carries the mint in jsonParsed output; a plain SPL
# ``transfer`` cannot be attributed to USDC without extra account-resolution.
VERIFIABLE_TYPES = ("transferChecked",)


def _parse_amount(info: dict[str, Any]) -> tuple[int, int] | None:
    """Extract ``(amount_atomic, decimals)`` from a parsed transferChecked."""
    token_amount = info.get("tokenAmount")
    if isinstance(token_amount, dict):
        raw = token_amount.get("amount")
        try:
            amount = int(str(raw)) if raw is not None else 0
        except (ValueError, TypeError):
            amount = 0
        try:
            decimals = int(token_amount.get("decimals", 6))
        except (ValueError, TypeError):
            decimals = 6
        return amount, decimals
    raw = info.get("amount")
    if raw is None:
        return None
    try:
        amount = int(str(raw))
    except (ValueError, TypeError):
        return None
    try:
        decimals = int(info.get("decimals", 6))
    except (ValueError, TypeError):
        decimals = 6
    return amount, decimals


def _iter_spl_instructions(result: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """Collect every parsed ``splToken`` transferChecked (top-level + inner)."""
    found: list[tuple[str, dict[str, Any]]] = []
    message = result.get("transaction", {}).get("message", {})
    instructions = message.get("instructions", [])
    meta = result.get("meta", {}) or {}
    inner_instructions = meta.get("innerInstructions", []) or []
    containers = [
        instructions,
        *(inner.get("instructions", []) or [] for inner in inner_instructions),
    ]
    for container in containers:
        if not isinstance(container, list):
            continue
        for inst in container:
            if not isinstance(inst, dict):
                continue
            if inst.get("program") != "splToken":
                continue
            program_id = inst.get("programId")
            if program_id is not None and program_id != SPL_TOKEN_PROGRAM_ID:
                continue
            parsed = inst.get("parsed")
            if not isinstance(parsed, dict):
                continue
            if parsed.get("type") not in VERIFIABLE_TYPES:
                continue
            info = parsed.get("info")
            if isinstance(info, dict):
                found.append((str(parsed.get("type")), info))
    return found


def _balance_address(entry: dict[str, Any], account_keys: list[Any]) -> str:
    """Resolve the token-account address of a (pre/post)TokenBalances entry."""
    address = entry.get("address")
    if isinstance(address, str) and address:
        return address
    # Legacy shape: no ``address`` field, only ``accountIndex`` into
    # ``message.accountKeys`` (dict entries on jsonParsed, or bare strings).
    index = entry.get("accountIndex")
    if isinstance(index, int) and 0 <= index < len(account_keys):
        key = account_keys[index]
        if isinstance(key, dict):
            pubkey = key.get("pubkey")
            return pubkey if isinstance(pubkey, str) else ""
        if isinstance(key, str):
            return key
    return ""


def _owner_token_accounts(result: dict[str, Any], owner: str) -> dict[str, set[str]]:
    """Map ``mint -> {token-account addresses}`` owned by ``owner``.

    Built from ``meta.preTokenBalances`` + ``meta.postTokenBalances`` (each
    entry carries ``owner``, ``mint`` and the account address), covering both
    pre-existing accounts and accounts created by the transaction itself.
    """
    accounts: dict[str, set[str]] = {}
    if not owner:
        return accounts
    meta = result.get("meta")
    if not isinstance(meta, dict):
        return accounts
    message = result.get("transaction", {}).get("message", {})
    account_keys = message.get("accountKeys") or []
    if not isinstance(account_keys, list):
        account_keys = []
    for key in ("preTokenBalances", "postTokenBalances"):
        entries = meta.get(key)
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict) or entry.get("owner") != owner:
                continue
            mint = entry.get("mint")
            address = _balance_address(entry, account_keys)
            if not isinstance(mint, str) or not mint or not address:
                continue
            accounts.setdefault(mint, set()).add(address)
    return accounts


def _amount_usd_value(amount_atomic: int, decimals: int, usdc_price_usd: float) -> float:
    if decimals < 0:
        decimals = 0
    denom = 10**decimals
    return float(round(amount_atomic / denom * usdc_price_usd, 6)) if denom else 0.0


def _failure(reason: str, **extra: Any) -> dict[str, Any]:
    result: dict[str, Any] = {
        "verified": False,
        "recipient": "",
        "amount_raw": 0,
        "amount_decimals": 6,
        "amount_usd": 0.0,
        "mint": "",
        "confirmed_at": "",
        "reason": reason,
        "slot": None,
    }
    result.update(extra)
    return result


def _success(
    *,
    recipient: str,
    amount_raw: int,
    amount_decimals: int,
    amount_usd: float,
    mint: str,
    confirmed_at: str,
    slot: int | None,
    payer: str = "",
    reason: str = "",
) -> dict[str, Any]:
    return {
        "verified": True,
        "recipient": recipient,
        "amount_raw": amount_raw,
        "amount_decimals": amount_decimals,
        "amount_usd": amount_usd,
        "mint": mint,
        "confirmed_at": confirmed_at,
        "reason": reason,
        "slot": slot,
        "payer": payer,
    }


def _confirmed_at(result: dict[str, Any]) -> str:
    block_time = result.get("blockTime")
    if not isinstance(block_time, (int, float)):
        return ""
    try:
        return datetime.fromtimestamp(int(block_time), tz=UTC).isoformat()
    except (ValueError, OSError, OverflowError):
        return ""


async def _post_rpc(
    url: str, payload: dict[str, Any]
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """POST one JSON-RPC request to ``url``.

    Returns ``(data, None)`` when the RPC produced an authoritative JSON
    envelope — including JSON-RPC ``error`` and null ``result`` answers: the
    chain's answer must not be overridden by a second RPC. Returns
    ``(None, failure)`` for transport-level failures (HTTP error, connection
    error, invalid JSON, non-object body) that justify trying a fallback RPC.
    """
    try:
        async with httpx.AsyncClient(timeout=SOLANA_RPC_TIMEOUT_SECONDS) as client:
            response = await client.post(url, json=payload)
            response.raise_for_status()
            data = response.json()
    except httpx.HTTPStatusError as e:
        log.warning("x402_solana_rpc_http_error", extra={"status": e.response.status_code})
        return None, _failure(f"rpc_http_error: {e.response.status_code}")
    except httpx.RequestError as e:
        log.warning("x402_solana_rpc_error", extra={"error": str(e)})
        return None, _failure(f"rpc_error: {e}")
    except ValueError as e:
        log.warning("x402_solana_rpc_invalid_json", extra={"error": str(e)})
        return None, _failure(f"rpc_invalid_json: {e}")
    if not isinstance(data, dict):
        return None, _failure("rpc_malformed_response")
    return data, None


async def verify_solana_payment(
    tx_signature: str,
    expected_recipient: str,
    amount_usd: float,
    *,
    rpc_url: str = DEFAULT_SOLANA_RPC_URL,
    fallback_rpc_urls: Sequence[str] = (),
    expected_mint: str = SOLANA_USDC_MINT,
    commitment: str = SOLANA_RPC_COMMITMENT,
    usdc_price_usd: float = 1.0,
) -> dict[str, Any]:
    """Verify a Solana SPL (USDC by default) x402 payment on-chain.

    Queries ``getTransaction`` (jsonParsed, explicit ``commitment``) and looks
    for a parsed ``splToken`` ``transferChecked`` where ``info.mint`` is
    ``expected_mint``, ``info.destination`` is ``expected_recipient`` itself OR
    a token account owned by ``expected_recipient`` (the normal case — SPL
    destinations are associated token accounts, not wallets), and the atomic
    amount >= the required amount. Confirms the tx succeeded
    (``meta.err is None``) and is confirmed (``slot`` present).

    Transport-level RPC failures fall through ``fallback_rpc_urls`` in order;
    an authoritative JSON-RPC answer from any RPC is final. Never raises —
    every failure returns a stable ``{"verified": False, "reason": ...}`` dict.
    """
    if not tx_signature:
        return _failure("missing tx signature")
    if not expected_recipient:
        return _failure("missing expected recipient")
    if amount_usd is None or amount_usd < 0:
        return _failure("invalid (negative) required amount")
    if not expected_mint:
        return _failure("missing expected mint")

    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getTransaction",
        "params": [
            tx_signature,
            {
                "encoding": "jsonParsed",
                "maxSupportedTransactionVersion": 0,
                "commitment": commitment,
            },
        ],
    }

    rpc_urls = [rpc_url, *(u for u in fallback_rpc_urls if u and u != rpc_url)]
    data: dict[str, Any] | None = None
    last_failure: dict[str, Any] | None = None
    for url in rpc_urls:
        data, last_failure = await _post_rpc(url, payload)
        if data is not None:
            break
    if data is None:
        return last_failure or _failure("rpc_error: no RPC endpoints available")

    rpc_error = data.get("error")
    if rpc_error is not None:
        if isinstance(rpc_error, dict):
            return _failure(
                f"rpc_error_response: {rpc_error.get('code', '')} "
                f"{rpc_error.get('message', '')}".strip()
            )
        return _failure(f"rpc_error_response: {rpc_error}")

    result = data.get("result")
    if result is None:
        return _failure("transaction_not_found")
    if not isinstance(result, dict):
        return _failure("rpc_malformed_result")

    slot = result.get("slot")
    meta = result.get("meta") or {}
    if not isinstance(meta, dict):
        return _failure("rpc_malformed_meta")

    if meta.get("err") is not None:
        return _failure(f"transaction_failed: {meta.get('err')}", slot=slot)

    if not isinstance(slot, int):
        return _failure("transaction_not_confirmed")

    instructions = _iter_spl_instructions(result)
    if not instructions:
        return _failure(
            "no_spl_transfer_found: no parsed splToken transferChecked instruction", slot=slot
        )

    # Token accounts owned by the recipient: a transferChecked ``destination``
    # is normally the recipient's associated token account, not the wallet.
    owner_accounts = _owner_token_accounts(result, expected_recipient)
    mint_accounts = owner_accounts.get(expected_mint, set())
    all_owner_accounts = set().union(*owner_accounts.values()) if owner_accounts else set()

    matches: list[tuple[int, int, str]] = []  # (amount_atomic, decimals, authority)
    mint_mismatch = False
    recipient_mismatch_dest: str | None = None
    for _inst_type, info in instructions:
        mint = info.get("mint")
        destination = info.get("destination")
        parsed = _parse_amount(info)
        if parsed is None or not isinstance(destination, str) or not destination:
            continue
        amount_atomic, decimals = parsed
        to_expected = destination == expected_recipient or destination in mint_accounts
        if mint == expected_mint and to_expected:
            authority = info.get("authority")
            matches.append((amount_atomic, decimals, str(authority) if authority else ""))
        elif mint != expected_mint and (to_expected or destination in all_owner_accounts):
            mint_mismatch = True
        elif mint == expected_mint and not to_expected:
            recipient_mismatch_dest = destination

    if not matches:
        if mint_mismatch:
            return _failure(
                f"wrong_mint: transfer destination belongs to recipient "
                f"but mint is not {expected_mint}",
                slot=slot,
            )
        if recipient_mismatch_dest:
            return _failure(
                f"wrong_recipient: SPL transfer to {recipient_mismatch_dest}, "
                f"expected {expected_recipient} (or one of its token accounts)",
                slot=slot,
            )
        return _failure(
            f"no_payment_found: no {expected_mint} transferChecked to recipient", slot=slot
        )

    # Prefer the largest matching transfer (one x402 payment = one transfer).
    paid_atomic, decimals, payer = max(matches, key=lambda item: item[0])
    denom = 10**decimals if decimals >= 0 else 1
    paid_usd = paid_atomic / denom if denom else float(paid_atomic)
    required_usd = amount_usd / usdc_price_usd if usdc_price_usd > 0 else float(amount_usd)
    if paid_usd + 1e-9 < required_usd:
        return _failure(
            f"underpayment: tx paid {paid_usd:.6f} < required {required_usd:.6f}",
            recipient=expected_recipient,
            amount_raw=paid_atomic,
            amount_decimals=decimals,
            amount_usd=_amount_usd_value(paid_atomic, decimals, usdc_price_usd),
            mint=expected_mint,
            confirmed_at=_confirmed_at(result),
            slot=slot,
        )

    return _success(
        recipient=expected_recipient,
        amount_raw=paid_atomic,
        amount_decimals=decimals,
        amount_usd=_amount_usd_value(paid_atomic, decimals, usdc_price_usd),
        mint=expected_mint,
        confirmed_at=_confirmed_at(result),
        slot=slot,
        payer=payer,
    )
