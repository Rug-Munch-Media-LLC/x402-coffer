# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Rug Munch Media LLC
#
# Part of Coffer — https://git.rugmunch.io/RugMunchMedia/x402-coffer
# Licensed under MIT. See LICENSE.
"""x402 — TRON TRC-20 USDT payment verifier.

Verifies a TRON TRC-20 USDT payment on-chain by querying the Trongrid RPC
``/walletsolidity/gettransactioninfobyid`` and walking the ``log`` events for
a TRC-20 Transfer event — httpx + stdlib only, no TRON SDK.

Uses **topic-based** decoding (same approach as EVM log decoding):
- Queries `/walletsolidity/gettransactioninfobyid`
- Walks `result.log[]` for entries with `topics[0] == TRANSFER_EVENT_TOPIC0`
- Decodes `from` (topics[1]), `to` (topics[2]), `value` (data)
- Compares decoded `to` (hex) against expected recipient (hex)

This is more robust than the event-name approach because it works with both
raw and parsed RPC formats, and follows the same pattern as EVM log decoding.

The verifier NEVER raises: every RPC error / not-found / malformed response
is converted into ``{"verified": False, "reason": ...}`` so callers can rely
on a stable dict shape.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

import httpx

log = logging.getLogger(__name__)

# TRON mainnet USDT contract (TRC-20).
TRON_USDT_CONTRACT = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"  # noqa: S105

# Default public Trongrid RPC (override per call / env in production).
DEFAULT_TRON_RPC_URL = "https://api.trongrid.io"

TRON_RPC_TIMEOUT_SECONDS = 15.0

# TRC-20 Transfer event signature (keccak of "Transfer(address,address,uint256)").
TRANSFER_EVENT_TOPIC0 = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"

# TRON mainnet address prefix byte (base58check encoding starts with 'T').
TRON_ADDRESS_PREFIX = "41"


def _failure(reason: str, **extra: Any) -> dict[str, Any]:
    """Stable failure dict — the verifier never raises."""
    return {
        "verified": False,
        "recipient": "",
        "amount_raw": 0,
        "amount_decimals": 6,
        "amount_usd": 0.0,
        "contract": "",
        "confirmed_at": "",
        "reason": reason,
        "block": None,
        **extra,
    }


def _success(
    *,
    recipient: str,
    amount_raw: int,
    decimals: int,
    amount_usd: float,
    contract: str,
    confirmed_at: str,
    block: int | None,
    signer: str = "",
    tx_hash: str = "",
) -> dict[str, Any]:
    """Stable success dict."""
    return {
        "verified": True,
        "recipient": recipient,
        "amount_raw": amount_raw,
        "amount_decimals": decimals,
        "amount_usd": amount_usd,
        "contract": contract,
        "confirmed_at": confirmed_at,
        "block": block,
        "signer": signer,
        "tx_hash": tx_hash,
    }


def _confirmed_at_from_timestamp(timestamp_ms: int | float | None) -> str:
    """ISO-8601 UTC timestamp from a millisecond block timestamp."""
    if not isinstance(timestamp_ms, (int, float)):
        return ""
    try:
        return datetime.fromtimestamp(int(timestamp_ms) / 1000, tz=UTC).isoformat()
    except (ValueError, OSError, OverflowError):
        return ""


def _iter_trc20_transfers(result: dict[str, Any]) -> list[dict[str, Any]]:
    """Yield decoded TRC-20 Transfer events from a contract result.

    Each event is a dict with ``from``, ``to``, ``value``, ``topic0``.
    Only events with the canonical Transfer topic0 are returned.
    Uses topic-based decoding (same approach as EVM log decoding).
    """
    events: list[dict[str, Any]] = []
    logs = result.get("log") or result.get("logs") or []
    if not isinstance(logs, list):
        return events

    for entry in logs:
        if not isinstance(entry, dict):
            continue
        topics = entry.get("topics") or []
        if not isinstance(topics, list) or len(topics) < 3:
            continue
        topic0 = topics[0]
        if topic0.lower() != TRANSFER_EVENT_TOPIC0:
            continue

        # Decode the Transfer event.
        # topics[1] = from (32-byte, address is last 20 bytes, TRON prefix 41)
        # topics[2] = to (32-byte, address is last 20 bytes, TRON prefix 41)
        # data = value (uint256)
        from_hex = topics[1][-40:] if len(topics[1]) >= 40 else topics[1]
        to_hex = topics[2][-40:] if len(topics[2]) >= 40 else topics[2]
        data_hex = entry.get("data", "0x0")

        try:
            value = int(data_hex, 16) if data_hex.startswith("0x") else int(data_hex)
        except (ValueError, TypeError):
            value = 0

        events.append(
            {
                "from": TRON_ADDRESS_PREFIX + from_hex,
                "to": TRON_ADDRESS_PREFIX + to_hex,
                "value": value,
                "topic0": topic0,
            }
        )

    return events


async def verify_tron_payment(
    tx_hash: str,
    recipient: str,
    amount_usd: float,
    *,
    rpc_url: str = DEFAULT_TRON_RPC_URL,
    contract: str = TRON_USDT_CONTRACT,
    usdt_price_usd: float = 1.0,
    timeout: float = TRON_RPC_TIMEOUT_SECONDS,
    fallback_rpc_urls: list[str] | None = None,
) -> dict[str, Any]:
    """Verify a TRON TRC-20 USDT payment on-chain.

    Queries the Trongrid RPC for the transaction, walks the event logs for a
    TRC-20 Transfer event from the USDT contract to the merchant wallet, and
    verifies the amount meets the required USD value.

    Uses **topic-based** decoding: logs must have `topics[0]` matching the
    canonical Transfer event signature. This is more robust than event-name
    matching because it works with both raw and parsed RPC formats.

    NEVER raises — every failure returns a stable dict with ``verified: False``.
    """
    if not tx_hash or not str(tx_hash).strip():
        return _failure("missing tx_hash")
    if not recipient or not str(recipient).strip():
        return _failure("missing recipient wallet")
    if amount_usd is None or amount_usd < 0 or not isinstance(amount_usd, (int, float)):
        return _failure("invalid_amount: amount_usd must be a non-negative number")

    # Normalize expected recipient to hex for comparison.
    expected_hex = recipient.strip().lower()
    if expected_hex.startswith("0x"):
        expected_hex = expected_hex[2:]
    if expected_hex.startswith(TRON_ADDRESS_PREFIX.lower()):
        expected_hex = expected_hex[2:]

    rpc_urls = [rpc_url] + list(fallback_rpc_urls or [])
    last_error = "no RPC available"

    async with httpx.AsyncClient(timeout=timeout) as client:
        for rpc in rpc_urls:
            try:
                # Query transaction info (includes event logs).
                resp = await client.post(
                    f"{rpc}/walletsolidity/gettransactioninfobyid",
                    json={"value": tx_hash},
                )
                resp.raise_for_status()
                data = resp.json()

                # Transaction not found.
                if not data or data.get("id") is None:
                    return _failure("transaction_not_found", tx_hash=tx_hash)

                # Check transaction was confirmed (has a block number).
                block_number = data.get("blockNumber") or data.get("block_number")
                if not block_number:
                    return _failure("transaction_unconfirmed", tx_hash=tx_hash)

                # Decode TRC-20 Transfer events (topic-based).
                events = _iter_trc20_transfers(data)
                if not events:
                    # Fallback: try decoding from receipt.logs if present.
                    receipt = data.get("receipt") or {}
                    events = _iter_trc20_transfers(receipt)

                if not events:
                    return _failure(
                        "no_trc20_transfer_found: no TRC-20 Transfer event in transaction",
                        tx_hash=tx_hash,
                        block=block_number,
                    )

                # Find the best matching transfer to the expected recipient.
                matching: list[dict[str, Any]] = []
                wrong_recipient: str | None = None
                for event in events:
                    to_hex = event["to"][2:] if event["to"].startswith(TRON_ADDRESS_PREFIX) else event["to"]
                    if to_hex.lower() == expected_hex.lower():
                        matching.append(event)
                    else:
                        wrong_recipient = event["to"]

                if not matching:
                    if wrong_recipient:
                        return _failure(
                            f"wrong_recipient: TRC-20 transfer to {wrong_recipient}, expected {recipient}",
                            tx_hash=tx_hash,
                            block=block_number,
                        )
                    return _failure(
                        "no_trc20_transfer_found: no TRC-20 transfer to recipient",
                        tx_hash=tx_hash,
                        block=block_number,
                    )

                # Prefer the largest transfer to the expected recipient.
                best = max(matching, key=lambda e: e["value"])
                paid_atomic = best["value"]
                paid_usd = paid_atomic / 1_000_000  # USDT has 6 decimals

                if paid_usd + 1e-9 < amount_usd:
                    return _failure(
                        "insufficient_amount",
                        tx_hash=tx_hash,
                        amount_atomic=paid_atomic,
                        required_usd=amount_usd,
                        actual_usd=paid_usd,
                        block=block_number,
                    )

                # Extract confirmation timestamp.
                timestamp = data.get("blockTimeStamp") or data.get("block_timestamp") or 0

                return _success(
                    recipient=recipient,
                    amount_raw=paid_atomic,
                    decimals=6,
                    amount_usd=round(paid_usd * usdt_price_usd, 6),
                    contract=contract,
                    confirmed_at=_confirmed_at_from_timestamp(timestamp),
                    block=block_number,
                    signer=best.get("from", ""),
                    tx_hash=tx_hash,
                )

            except httpx.HTTPStatusError as e:
                last_error = f"RPC HTTP {e.response.status_code}"
                log.warning("x402_tron_rpc_http_error", extra={"rpc": rpc, "error": str(e)})
                continue
            except httpx.RequestError as e:
                last_error = f"RPC transport error: {e}"
                log.warning("x402_tron_rpc_error", extra={"rpc": rpc, "error": str(e)})
                continue
            except Exception as e:  # noqa: BLE001 — never raise
                last_error = f"unexpected error: {e}"
                log.warning("x402_tron_verify_error", extra={"rpc": rpc, "error": str(e)})
                continue

    return _failure(last_error, tx_hash=tx_hash)
