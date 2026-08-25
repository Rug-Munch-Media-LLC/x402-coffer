"""x402 — Solana SPL USDC payment verifier.

Verifies a Solana USDC payment on-chain by querying the Solana RPC
``getTransaction`` with ``encoding: "jsonParsed"`` and walking the parsed
``splToken`` ``transferChecked`` instructions — no Solana SDK dependency
required (httpx + stdlib only).

The verifier NEVER raises: every RPC error / not-found / malformed response
is converted into ``{"verified": False, "reason": ...}`` so callers can rely
on a stable dict shape.
"""
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Rug Munch Media LLC
#
# Standalone x402 implementation — https://git.rugmunch.io/RugMunchMedia/x402-py
# Licensed under MIT. See LICENSE.

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# Solana mainnet USDC mint (Circle).
SOLANA_USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"

# Default public Solana mainnet RPC (override per call / env in production).
DEFAULT_SOLANA_RPC_URL = "https://api.mainnet-beta.solana.com"

# RPC timeout — verify is a single round-trip, keep it short.
SOLANA_RPC_TIMEOUT_SECONDS = 15.0

# SPL Token program id — sanity-check the parsed instruction came from the
# canonical token program (not a custom program with a spoofed "parsed").
# Well-known public constant, NOT a secret: noqa S105 (bandit heuristically
# treats base58 ids beginning with "Token" as credentials).
SPL_TOKEN_PROGRAM_ID = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"  # noqa: S105

# Only transferChecked carries the mint in jsonParsed output; plain SPL
# ``transfer`` does not, so it cannot be attributed to USDC without extra
# account-resolution RPC calls. Verifiable instruction types:
VERIFIABLE_TYPES = ("transferChecked",)


def _parse_amount(info: dict[str, Any]) -> tuple[int, int] | None:
    """Extract (amount_atomic, decimals) from a parsed transferChecked info.

    ``tokenAmount.amount`` is the authoritative atomic amount string;
    ``info.amount`` is the legacy duplicate. Returns None when malformed.
    """
    token_amount = info.get("tokenAmount")
    if isinstance(token_amount, dict):
        raw = token_amount.get("amount")
        if raw is not None:
            try:
                amount = int(str(raw))
            except (ValueError, TypeError):
                amount = 0
            try:
                decimals = int(token_amount.get("decimals", 6))
            except (ValueError, TypeError):
                decimals = 6
            return amount, decimals
    raw = info.get("amount")
    if raw is not None:
        try:
            amount = int(str(raw))
        except (ValueError, TypeError):
            return None
        try:
            decimals = int(info.get("decimals", 6))
        except (ValueError, TypeError):
            decimals = 6
        return amount, decimals
    return None


def _iter_spl_instructions(result: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """Yield ``(instruction_type, parsed_info)`` for every parsed splToken
    transfer instruction in the transaction (top-level + inner).

    Returns a list so the caller can pick the best (highest-amount) match
    when a transaction contains several USDC transfers.
    """
    found: list[tuple[str, dict[str, Any]]] = []
    message = result.get("transaction", {}).get("message", {})
    instructions = message.get("instructions", [])
    meta = result.get("meta", {}) or {}
    inner_instructions = meta.get("innerInstructions", []) or []
    for container in (
        instructions,
        *(inner.get("instructions", []) or [] for inner in inner_instructions),
    ):
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
            inst_type = parsed.get("type")
            if inst_type not in VERIFIABLE_TYPES:
                continue
            info = parsed.get("info")
            if isinstance(info, dict):
                found.append((str(inst_type), info))
    return found


def _amount_usd_value(amount_atomic: int, decimals: int, usdc_price_usd: float) -> float:
    """USD value of an atomic amount, rounded to 6 decimals."""
    if decimals < 0:
        decimals = 0
    return float(round(amount_atomic / (10**decimals) * usdc_price_usd, 6))


def _failure(reason: str, **extra: Any) -> dict[str, Any]:
    """Build a never-raise failure dict with the stable result shape."""
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
    }


def _confirmed_at(result: dict[str, Any]) -> str:
    """ISO-8601 UTC timestamp from ``blockTime`` (unix seconds), or ""."""
    block_time = result.get("blockTime")
    if not isinstance(block_time, (int, float)):
        return ""
    try:
        return datetime.fromtimestamp(int(block_time), tz=UTC).isoformat()
    except (ValueError, OSError, OverflowError):
        return ""


async def verify_solana_payment(
    tx_signature: str,
    expected_recipient: str,
    amount_usd: float,
    *,
    rpc_url: str = DEFAULT_SOLANA_RPC_URL,
    usdc_price_usd: float = 1.0,
) -> dict[str, Any]:
    """Verify a Solana USDC payment for an x402 payment.

    Queries ``getTransaction`` with ``jsonParsed`` encoding and looks for a
    parsed ``splToken`` ``transferChecked`` instruction where:
      - ``info.mint`` is the Solana USDC mint
      - ``info.destination`` equals ``expected_recipient`` (base58)
      - ``amount / 10**decimals >= amount_usd / usdc_price_usd``

    Also confirms the transaction succeeded (``meta.err == null``) and was
    confirmed (``slot`` present).

    Never raises — every failure returns
    ``{"verified": False, "reason": ...}``.
    """
    if not tx_signature:
        return _failure("missing tx signature")
    if not expected_recipient:
        return _failure("missing expected recipient")
    if amount_usd is None or amount_usd < 0:
        return _failure("invalid (negative) required amount")

    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getTransaction",
        "params": [
            tx_signature,
            {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0},
        ],
    }
    try:
        async with httpx.AsyncClient(timeout=SOLANA_RPC_TIMEOUT_SECONDS) as client:
            response = await client.post(rpc_url, json=payload)
            response.raise_for_status()
            data = response.json()
    except httpx.HTTPStatusError as e:
        return _failure(f"rpc_http_error: {e.response.status_code}")
    except httpx.RequestError as e:
        return _failure(f"rpc_error: {e}")
    except ValueError as e:
        return _failure(f"rpc_invalid_json: {e}")

    if not isinstance(data, dict):
        return _failure("rpc_malformed_response")

    rpc_error = data.get("error")
    if rpc_error is not None:
        if isinstance(rpc_error, dict):
            code = rpc_error.get("code", "")
            message = rpc_error.get("message", "")
            return _failure(f"rpc_error_response: {code} {message}".strip())
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

    tx_err = meta.get("err")
    if tx_err is not None:
        return _failure(f"transaction_failed: {tx_err}", slot=slot)

    if not isinstance(slot, int):
        # No slot means the RPC has not confirmed the tx into a block yet.
        return _failure("transaction_not_confirmed")

    instructions = _iter_spl_instructions(result)
    if not instructions:
        return _failure(
            "no_usdc_transfer_found: no parsed splToken transferChecked instruction",
            slot=slot,
        )

    # Track the best candidates so failure reasons are precise.
    usdc_to_recipient: list[tuple[int, int]] = []  # (amount_atomic, decimals)
    mint_mismatch = False
    recipient_mismatch_dest: str | None = None
    for _inst_type, info in instructions:
        mint = info.get("mint")
        destination = info.get("destination")
        parsed = _parse_amount(info)
        if parsed is None:
            continue
        amount_atomic, decimals = parsed
        if mint == SOLANA_USDC_MINT and destination == expected_recipient:
            usdc_to_recipient.append((amount_atomic, decimals))
        elif mint != SOLANA_USDC_MINT and destination == expected_recipient:
            mint_mismatch = True
        elif mint == SOLANA_USDC_MINT and destination != expected_recipient:
            recipient_mismatch_dest = destination

    if not usdc_to_recipient:
        if mint_mismatch:
            return _failure(
                "wrong_mint: transfer destination matches but mint is not Solana USDC",
                slot=slot,
            )
        if recipient_mismatch_dest:
            return _failure(
                f"wrong_recipient: USDC transfer to {recipient_mismatch_dest}, expected {expected_recipient}",
                slot=slot,
            )
        return _failure(
            "no_usdc_transfer_found: no Solana USDC transferChecked to recipient",
            slot=slot,
        )

    # Prefer the largest USDC transfer to the expected recipient.
    paid_atomic, decimals = max(usdc_to_recipient, key=lambda pair: pair[0])
    paid_usd = paid_atomic / (10**decimals) if decimals >= 0 else float(paid_atomic)
    required_usd = amount_usd / usdc_price_usd if usdc_price_usd > 0 else float(amount_usd)
    if paid_usd + 1e-9 < required_usd:
        return _failure(
            f"underpayment: tx paid {paid_usd:.6f} USDC < required {required_usd:.6f} USDC",
            recipient=expected_recipient,
            amount_raw=paid_atomic,
            amount_decimals=decimals,
            amount_usd=_amount_usd_value(paid_atomic, decimals, usdc_price_usd),
            mint=SOLANA_USDC_MINT,
            confirmed_at=_confirmed_at(result),
            slot=slot,
        )

    return _success(
        recipient=expected_recipient,
        amount_raw=paid_atomic,
        amount_decimals=decimals,
        amount_usd=_amount_usd_value(paid_atomic, decimals, usdc_price_usd),
        mint=SOLANA_USDC_MINT,
        confirmed_at=_confirmed_at(result),
        slot=slot,
    )
