"""Coffer — customer billing surface (settlement history + balance).

Brings coffer to parity with pry's customer settlement tracking
(``pry.api.routers.user.account``) using coffer's JSONL audit ledger
(``settlements.jsonl`` + ``payments.jsonl``) instead of a SQL database.

Endpoints
---------
- ``GET /v1/billing/history`` — settlement history for the authenticated
  caller: ``{"transactions": [...], "total_spent": ...}``.
- ``GET /v1/billing/balance`` — prepaid balance view:
  ``{"balance_usd", "balance_by_chain", "last_settlement"}``.

Caller identity
---------------
Coffer has no first-party API-key auth; the caller identity is the same
best-effort ``caller_key`` used by the payment middleware (client IP / API
key subject recorded at verify time). A client-supplied ``X-User-Id`` header
is NEVER trusted for attribution — it is trivially spoofable and would turn
``/v1/billing/*`` into an IDOR oracle for any other customer's ledger. The
billing view is therefore attributed exclusively to the verified payment's
recorded ``caller_key`` (the identity the payment gate itself bound the
``X-Payment-ID`` to). Deployments that front coffer with an auth proxy should
terminate caller identity at the proxy and rely on the caller key / proxy IP
binding; there is no first-party customer-id merge.
"""

# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Rug Munch Media LLC
# Part of Coffer — https://git.rugmunch.io/RugMunchMedia/x402-coffer

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request

from .adapters import caller_key_from_request
from .facilitator_config import X402_DIR
from .pricing import network_family

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/billing", tags=["billing"])

__all__ = ["billing_balance", "billing_history", "router"]


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    """Read a JSONL audit file, skipping corrupt lines (fail-open, read-only)."""
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    try:
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                rows.append(row)
    except OSError as e:
        logger.warning("x402_billing_ledger_read_failed", extra={"error": str(e)})
    return rows


def _user_id_from_request(request: Request) -> str:
    """Caller identity for billing attribution — the verified payment's caller.

    Security (H2): the client-supplied ``X-User-Id`` header is NEVER consulted.
    It is unauthenticated and spoofable, and honouring it would let any paid
    caller read another customer's settlement history/balance. Attribution is
    exclusively the same ``caller_key`` the payment middleware binds verified
    ``X-Payment-ID`` replays to, so a caller can only ever see the ledger rows
    recorded against their own verified payments.
    """
    return caller_key_from_request(request)


def _ledger_for_user(user_id: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return (verified_payments, successful_settlements) attributed to user_id.

    Payments are matched on ``caller_key``; settlements are joined to their
    payment record by ``payment_id`` (fallback: ``tx_hash``) so the settlement
    inherits the caller attribution recorded at verify time.
    """
    payments = [
        p
        for p in _read_jsonl(X402_DIR / "payments.jsonl")
        if p.get("verified") is True and str(p.get("caller_key") or "") == user_id
    ]
    by_payment_id = {str(p.get("payment_id") or ""): p for p in payments if p.get("payment_id")}
    by_tx_hash = {str(p.get("tx_hash") or ""): p for p in payments if p.get("tx_hash")}

    settlements: list[dict[str, Any]] = []
    for s in _read_jsonl(X402_DIR / "settlements.jsonl"):
        if s.get("success") is not True:
            continue
        # Explicit user_id on the receipt wins; else join via the payment record.
        if str(s.get("user_id") or "") == user_id:
            settlements.append(s)
            continue
        payment = by_payment_id.get(str(s.get("payment_id") or "")) or by_tx_hash.get(
            str(s.get("transaction") or "")
        )
        if payment is not None:
            merged = {**payment, **s}
            settlements.append(merged)
    return payments, settlements


def _chain_of(network: str) -> str:
    """Coarse chain family for per-chain balance attribution."""
    try:
        return network_family(network)
    except Exception:  # noqa: BLE001 — attribution must never break billing
        return "evm"


@router.get("/history", summary="Settlement history for the authenticated user")
async def billing_history(request: Request) -> dict[str, Any]:
    """Settlements attributed to the caller, newest first.

    Each transaction: ``{id, date, amount_usd, chain, tx_hash, status,
    tool_name}``. ``total_spent`` sums successful settlements.
    """
    user_id = _user_id_from_request(request)
    _payments, settlements = _ledger_for_user(user_id)

    transactions: list[dict[str, Any]] = []
    total_spent = 0.0
    for i, s in enumerate(settlements):
        amount = float(s.get("amount_usd") or 0.0)
        total_spent += amount
        transactions.append(
            {
                "id": str(s.get("payment_id") or f"settlement:{i}"),
                "date": str(s.get("settled_at") or s.get("verified_at") or ""),
                "amount_usd": round(amount, 4),
                "chain": str(s.get("network") or ""),
                "tx_hash": str(s.get("transaction") or s.get("tx_hash") or ""),
                "status": "settled",
                "tool_name": str(s.get("operation") or ""),
            }
        )

    transactions.sort(key=lambda t: str(t["date"]), reverse=True)
    return {
        "user_id": user_id,
        "transactions": transactions,
        "total_spent": round(total_spent, 4),
    }


@router.get("/balance", summary="Prepaid balance for the authenticated user")
async def billing_balance(request: Request) -> dict[str, Any]:
    """Prepaid USD balance with per-chain breakdown and last settlement.

    ``balance_usd`` = verified payments (credits) − successful settlements
    (debits). ``balance_by_chain`` attributes the credit side to a chain
    family (evm / solana / tron / ...).
    """
    user_id = _user_id_from_request(request)
    payments, settlements = _ledger_for_user(user_id)

    balance_by_chain: dict[str, float] = {}
    credited = 0.0
    for p in payments:
        amount = float(p.get("amount_usd") or 0.0)
        credited += amount
        chain = _chain_of(str(p.get("network") or ""))
        balance_by_chain[chain] = round(balance_by_chain.get(chain, 0.0) + amount, 4)

    spent = sum(float(s.get("amount_usd") or 0.0) for s in settlements)
    last_settlement = ""
    if settlements:
        last_settlement = str(
            max(settlements, key=lambda s: str(s.get("settled_at") or s.get("verified_at") or "")).get(
                "settled_at"
            )
            or ""
        )

    return {
        "user_id": user_id,
        "balance_usd": round(credited - spent, 4),
        "balance_by_chain": balance_by_chain,
        "last_settlement": last_settlement,
    }
