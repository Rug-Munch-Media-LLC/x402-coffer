"""x402 — Solana transaction parsing helpers.

Pure functions that walk a ``getTransaction`` jsonParsed result and extract
parsed ``splToken`` ``transferChecked`` instructions, token-account
ownership, amounts and timestamps. No RPC, no I/O.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from .solana_constants import SPL_TOKEN_PROGRAM_ID, VERIFIABLE_TYPES


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
    decimals = max(decimals, 0)
    denom = 10**decimals
    return float(round(amount_atomic / denom * usdc_price_usd, 6)) if denom else 0.0


def _confirmed_at(result: dict[str, Any]) -> str:
    block_time = result.get("blockTime")
    if not isinstance(block_time, (int, float)):
        return ""
    try:
        return datetime.fromtimestamp(int(block_time), tz=UTC).isoformat()
    except (ValueError, OSError, OverflowError):
        return ""
