"""x402 — Solana verifier result builders.

Stable result dicts returned by the Solana payment verifier. The verifier
NEVER raises: every outcome is one of these shapes.
"""

from __future__ import annotations

from typing import Any


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
    expected_payer: str = "",
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
        "expected_payer": expected_payer,
        "actual_payer": payer,
    }
