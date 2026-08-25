# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Rug Munch Media LLC
# --- external_verify.py: external-facilitator verify + settlement normalization ---
from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from typing import Any

import httpx

from .evm_verify import _eip7702_verify
from .facilitator_config import X402_NETWORK_WALLETS, X402_TIMEOUT_SECONDS, X402_WALLET
from .pricing import asset_to_atomic, network_family, normalize_network_id
from .replay import REPLAY_HTTP_STATUS
from .solana_verifier import (
    DEFAULT_SOLANA_RPC_URL,
    SPL_ASSET_MINTS,
    verify_solana_payment,
)

# Comma-separated fallback Solana RPCs tried in order on transport failures.
SOLANA_RPC_FALLBACKS_ENV = "X402_SOLANA_RPC_FALLBACKS"


async def verify_external(
    self,
    payment_id: str,
    tx_hash: str,
    network: str,
    asset: str,
    amount_usd: float,
) -> dict[str, Any]:
    network_id = normalize_network_id(network)
    payload = {
        "paymentId": payment_id,
        "transactionHash": tx_hash,
        "network": network_id,
        "asset": asset,
        "amount": str(asset_to_atomic(amount_usd, asset, network_id)),
        "payToAddress": X402_NETWORK_WALLETS.get(network_id) or X402_WALLET,
        "scheme": "exact",
        "timestamp": datetime.now(UTC).isoformat(),
    }
    last_error = "No facilitators available"
    async with httpx.AsyncClient(timeout=X402_TIMEOUT_SECONDS) as client:
        for config in self.facilitators:
            if not self._is_healthy(config):
                continue
            try:
                response = await client.post(config.verify_url, json=payload)
                response.raise_for_status()
                data = response.json()
                self._mark_success(config)
                # H3 honesty: a bare "success":true is NOT proof of payment
                # -> require an explicit verification field (isValid/verified).
                verified = bool(data.get("isValid") or data.get("verified"))
                if verified:
                    ok, reason = await self._enforce_replay(tx_hash, network_id)
                    if not ok:
                        return {
                            "verified": False,
                            "reason": reason,
                            "settlement": None,
                            "network": network_id,
                            "http_status": REPLAY_HTTP_STATUS,
                        }
                return {
                    "verified": verified,
                    "reason": data.get("error", data.get("reason", "")) if not verified else "",
                    "settlement": data if verified else None,
                    "facilitator": config.name,
                    "network": network_id,
                }
            except httpx.HTTPStatusError as e:
                last_error = f"{config.name}: HTTP {e.response.status_code}"
                self._mark_failure(config)
            except httpx.RequestError as e:
                last_error = f"{config.name}: {e}"
                self._mark_failure(config)
            except json.JSONDecodeError as e:
                last_error = f"{config.name}: invalid JSON - {e}"
                self._mark_failure(config)

    # Self-verify fallbacks (EIP-7702 / Solana SPL) when external fails.
    if network_family(network_id) == "evm":
        eip = await _eip7702_verify(tx_hash, network_id, asset, amount_usd)
        if eip["verified"]:
            eip["facilitator"] = "self-hosted-eip7702"
            eip["network"] = network_id
            await self._enforce_replay(tx_hash, network_id)
            return eip
        last_error = eip.get("reason", last_error)
    elif network_family(network_id) == "solana":
        # NOTE: module-level verify_solana, not a router method — the router
        # carries no _verify_solana (it only exists on the vendored reference
        # class), so self._verify_solana here would raise AttributeError.
        res = await verify_solana(payment_id, tx_hash, network_id, asset, amount_usd)
        if res.get("verified"):
            await self._enforce_replay(tx_hash, network_id)
        return res

    return {"verified": False, "reason": last_error, "settlement": None, "network": network_id}


def normalize_settlement(
    data: dict[str, Any],
    payment_id: str,
    tx_hash: str,
    network: str,
    facilitator: str,
) -> dict[str, Any]:
    # M2 honesty: do not fabricate success on any HTTP-200 body -> require
    # an explicit settlement/verification signal from the facilitator.
    success = bool(
        data.get("success") is True
        or data.get("isValid") is True
        or data.get("verified") is True
        or data.get("settled") is True
        or bool(data.get("transactionHash") or data.get("transaction"))
    )
    return {
        "success": success,
        "transaction": data.get("transactionHash", data.get("transaction", tx_hash)),
        "network": network,
        "payment_id": payment_id,
        "facilitator": facilitator,
        "settled_at": datetime.now(UTC).isoformat(),
        "block_number": data.get("blockNumber", data.get("block_number", "")),
        "block_hash": data.get("blockHash", data.get("block_hash", "")),
        "confirmations": data.get("confirmations", 0),
        "amount_paid": data.get("amountPaid", data.get("amount_paid", "")),
        "reason": ""
        if success
        else (data.get("error", data.get("reason", "facilitator returned no settlement signal"))),
        "receipt": data,
    }


async def verify_solana(
    payment_id: str,
    tx_hash: str,
    network: str,
    asset: str,
    amount_usd: float,
) -> dict[str, Any]:
    network_id = normalize_network_id(network)
    if not payment_id or not tx_hash:
        return {
            "verified": False,
            "reason": "needs_facilitator: missing payment_id/tx_hash in header",
            "settlement": None,
            "scaffolded": True,
            "network": network_id,
        }
    # SPL-verifiable assets: USDC + USDT (mint table in solana_verifier). A
    # symbol outside the table cannot be attributed to a mint on-chain.
    mint = SPL_ASSET_MINTS.get(str(asset).upper())
    if mint is None:
        return {
            "verified": False,
            "reason": f"asset {asset!r} not SPL-verifiable",
            "settlement": None,
            "network": network_id,
        }
    recipient = (X402_NETWORK_WALLETS.get(network_id) or "").strip()
    if not recipient:
        return {
            "verified": False,
            "reason": "solana receiving wallet not configured — set X402_WALLET_SOLANA",
            "settlement": None,
            "network": network_id,
        }
    rpc = os.getenv("X402_SOLANA_RPC", DEFAULT_SOLANA_RPC_URL)
    fallbacks = [
        part.strip() for part in os.getenv(SOLANA_RPC_FALLBACKS_ENV, "").split(",") if part.strip()
    ]
    result = await verify_solana_payment(
        tx_hash,
        recipient,
        amount_usd,
        rpc_url=rpc,
        fallback_rpc_urls=fallbacks,
        expected_mint=mint,
    )
    return {
        "verified": bool(result.get("verified")),
        "reason": result.get("reason", ""),
        "settlement": None,
        "network": network_id,
        "facilitator": "solana-rpc",
        # ``payer`` is the transferChecked authority (the actual payer); the
        # recipient wallet is only a fallback for legacy responses.
        "signer": result.get("payer") or result.get("recipient") or recipient,
        "amount_atomic": result.get("amount_raw"),
        "asset": asset,
        "mint": result.get("mint"),
        "confirmed_at": result.get("confirmed_at"),
        "amount_usd": result.get("amount_usd"),
    }
