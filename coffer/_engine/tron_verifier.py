"""x402 — TRON TRC-20 USDT payment verifier.

Verifies a TRON TRC-20 USDT payment on-chain by querying the Trongrid RPC
``wallet/gettransactioninfobyid`` and walking the ``event`` logs for a
``Transfer`` event from the USDT contract — httpx + stdlib only, no TRON SDK.
The verifier NEVER raises; every failure returns a stable dict.
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
# Used to sanity-check the event is a real transfer, not a spoofed log.
TRANSFER_EVENT_NAME = "Transfer"


def _failure(reason: str, **extra: Any) -> dict[str, Any]:
    """Stable failure dict — the verifier never raises."""
    return {"verified": False, "reason": reason, **extra}


def _success(
    amount_atomic: int,
    decimals: int,
    amount_usd: float,
    confirmed_at: str,
    payer: str,
    **extra: Any,
) -> dict[str, Any]:
    """Stable success dict."""
    return {
        "verified": True,
        "reason": "",
        "amount_atomic": amount_atomic,
        "decimals": decimals,
        "amount_usd": amount_usd,
        "confirmed_at": confirmed_at,
        "signer": payer,
        **extra,
    }


def _extract_transfer_event(
    result: dict[str, Any],
    expected_contract: str,
    expected_recipient: str,
) -> tuple[dict[str, Any] | None, str]:
    """Walk the event logs and find a Transfer from the expected contract.

    Returns ``(event_dict, reason)``. ``event_dict`` is None on failure.
    """
    events = result.get("log") or result.get("events") or []
    if not isinstance(events, list):
        return None, "no event logs in transaction"

    for ev in events:
        if not isinstance(ev, dict):
            continue
        # Trongrid returns event logs with 'event_name' and 'event' fields,
        # or raw Solidity log entries with 'topics' and 'data'.
        event_name = ev.get("event_name") or ev.get("event") or ""
        caller_contract = ev.get("caller_contract_address") or ev.get("contract_address") or ""

        # Normalise contract address comparison (base58, case-insensitive).
        if str(caller_contract).strip().lower() != expected_contract.strip().lower():
            continue
        if str(event_name).strip() != TRANSFER_EVENT_NAME:
            continue

        # Extract from/to/value from the event payload.
        event_data = ev.get("event") or ev.get("data") or {}
        if isinstance(event_data, dict):
            from_addr = event_data.get("from") or event_data.get("0") or ""
            to_addr = event_data.get("to") or event_data.get("1") or ""
            value = event_data.get("value") or event_data.get("2") or 0
        else:
            continue

        # Verify the recipient matches the merchant wallet.
        if str(to_addr).strip().lower() != expected_recipient.strip().lower():
            continue

        return {"from": from_addr, "to": to_addr, "value": value}, ""

    return None, "no matching Transfer event from expected contract/recipient"


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
    ``Transfer`` event from the USDT contract to the merchant wallet, and
    verifies the amount meets the required USD value.

    NEVER raises — every failure returns a stable dict with ``verified: False``.
    """
    if not tx_hash or not str(tx_hash).strip():
        return _failure("missing tx_hash")
    if not recipient or not str(recipient).strip():
        return _failure("missing recipient wallet")

    rpc_urls = [rpc_url] + list(fallback_rpc_urls or [])
    last_error = "no RPC available"

    async with httpx.AsyncClient(timeout=timeout) as client:
        for rpc in rpc_urls:
            try:
                # Query transaction info (includes event logs).
                resp = await client.post(
                    f"{rpc}/wallet/gettransactioninfobyid",
                    json={"value": tx_hash},
                )
                resp.raise_for_status()
                result = resp.json()

                # Transaction not found.
                if not result or result.get("id") is None:
                    return _failure("transaction_not_found", tx_hash=tx_hash)

                # Check transaction was confirmed (has a block number).
                block_number = result.get("blockNumber") or result.get("block_number")
                if not block_number:
                    return _failure("transaction_unconfirmed", tx_hash=tx_hash)

                # Extract the Transfer event.
                event, reason = _extract_transfer_event(result, contract, recipient)
                if event is None:
                    return _failure(reason or "no matching transfer event", tx_hash=tx_hash)

                # Parse the amount (TRC-20 USDT has 6 decimals).
                raw_value = event.get("value", 0)
                try:
                    amount_atomic = int(str(raw_value))
                except (ValueError, TypeError):
                    return _failure("invalid amount in transfer event", tx_hash=tx_hash)

                decimals = 6  # TRC-20 USDT
                amount_usd_actual = (amount_atomic / (10**decimals)) * usdt_price_usd

                # Verify the amount meets the required USD value.
                if amount_usd_actual < amount_usd:
                    return _failure(
                        "insufficient_amount",
                        tx_hash=tx_hash,
                        amount_atomic=amount_atomic,
                        required_usd=amount_usd,
                        actual_usd=amount_usd_actual,
                    )

                # Extract confirmation timestamp.
                timestamp = result.get("time") or result.get("timestamp") or 0
                try:
                    confirmed_at = datetime.fromtimestamp(int(timestamp) / 1000, tz=UTC).isoformat()
                except (ValueError, TypeError, OSError):
                    confirmed_at = datetime.now(UTC).isoformat()

                return _success(
                    amount_atomic=amount_atomic,
                    decimals=decimals,
                    amount_usd=amount_usd_actual,
                    confirmed_at=confirmed_at,
                    payer=event.get("from", ""),
                    tx_hash=tx_hash,
                    contract=contract,
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
