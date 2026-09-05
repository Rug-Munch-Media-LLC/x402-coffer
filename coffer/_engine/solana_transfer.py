"""x402 — Solana SPL payment verification (RPC + transfer matching).

Verifies a Solana SPL payment on-chain by querying the Solana RPC
``getTransaction`` with ``encoding: "jsonParsed"`` and walking the parsed
``splToken`` ``transferChecked`` instructions — httpx + stdlib only, no Solana
SDK. The verifier NEVER raises; every failure returns a stable dict.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any

import httpx

from .solana_constants import (
    DEFAULT_SOLANA_RPC_URL,
    SOLANA_RPC_COMMITMENT,
    SOLANA_RPC_TIMEOUT_SECONDS,
    SOLANA_USDC_MINT,
)
from .solana_parse import (
    _amount_usd_value,
    _confirmed_at,
    _iter_spl_instructions,
    _owner_token_accounts,
    _parse_amount,
)
from .solana_result import _failure, _success

log = logging.getLogger(__name__)


async def _post_rpc(url: str, payload: dict[str, Any]) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
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
    expected_payer: str = "",
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
                f"rpc_error_response: {rpc_error.get('code', '')} {rpc_error.get('message', '')}".strip()
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
        return _failure("no_spl_transfer_found: no parsed splToken transferChecked instruction", slot=slot)

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
                f"wrong_mint: transfer destination belongs to recipient but mint is not {expected_mint}",
                slot=slot,
            )
        if recipient_mismatch_dest:
            return _failure(
                f"wrong_recipient: SPL transfer to {recipient_mismatch_dest}, "
                f"expected {expected_recipient} (or one of its token accounts)",
                slot=slot,
            )
        return _failure(f"no_payment_found: no {expected_mint} transferChecked to recipient", slot=slot)

    # Prefer the largest matching transfer (one x402 payment = one transfer).
    paid_atomic, decimals, payer = max(matches, key=lambda item: item[0])
    if expected_payer and payer and payer != expected_payer:
        return _failure(
            f"payer mismatch: authority {payer} != expected {expected_payer}",
            recipient=expected_recipient,
            mint=expected_mint,
            confirmed_at=_confirmed_at(result),
            slot=slot,
            expected_payer=expected_payer,
            actual_payer=payer,
        )
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
        expected_payer=expected_payer,
    )
