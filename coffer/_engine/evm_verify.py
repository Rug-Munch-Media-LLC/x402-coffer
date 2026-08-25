# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Rug Munch Media LLC
# --- evm_verify.py: EIP-7702 self-verify receipt fallback ---
from __future__ import annotations

import os
from typing import Any

import httpx

from .facilitator_config import X402_NETWORK_WALLETS, X402_TIMEOUT_SECONDS, X402_WALLET
from .pricing import (
    ERC20_TRANSFER_SELECTOR,
    asset_to_atomic,
    normalize_network_id,
    resolve_asset_entry,
)

# EIP-7702 self-verify RPC URLs: network -> JSON-RPC endpoint (env override).
_EIP7702_RPC_DEFAULTS: dict[str, str] = {
    "eip155:8453": "https://mainnet.base.org",
    "eip155:1": "https://eth.llamarpc.com",
    "eip155:42161": "https://arb1.arbitrum.io/rpc",
}
EIP7702_RPC: dict[str, str] = {
    n: os.getenv(f"X402_EIP7702_RPC_{n.split(':')[-1]}", url)
    for n, url in _EIP7702_RPC_DEFAULTS.items()
}

# ── EIP-7702 self-verify fallback ───────────────────────────────────────────


def _eip7702_wallet(network: str) -> str:
    return (X402_NETWORK_WALLETS.get(normalize_network_id(network)) or X402_WALLET or "").lower()


async def _rpc_call(
    client: httpx.AsyncClient, rpc: str, method: str, params: list[Any]
) -> dict[str, Any] | None:
    response = await client.post(
        rpc, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    )
    response.raise_for_status()
    data = response.json()
    return data.get("result") if isinstance(data, dict) else None


def _eip7702_matches(
    tx: dict[str, Any], network: str, asset: str, amount_usd: float
) -> tuple[bool, str]:
    """Check an EIP-7702 tx actually paid OUR wallet at least the price.

    ERC-20 asset -> transfer() to payTo on the token contract; native asset ->
    value to payTo. Unknown assets fail closed.
    """
    pay_to = _eip7702_wallet(network)
    if not pay_to or pay_to == "coffer-unconfigured-wallet":
        return False, "server wallet not configured"
    tx_to = str(tx.get("to") or "").lower()
    entry = resolve_asset_entry(network, asset)
    if entry is None:
        return False, f"unsupported asset {asset} on {network}"
    contract = str(entry.get("contract") or "").lower()
    if entry.get("verification") in ("eip3009", "permit2") and contract:
        if tx_to != contract:
            return False, f"tx recipient {tx_to} != {asset} token on {network}"
        calldata = str(tx.get("input") or "0x")
        if not calldata.lower().startswith(ERC20_TRANSFER_SELECTOR):
            return False, "tx is not an ERC-20 transfer()"
        if len(calldata) < 138:
            return False, "ERC-20 transfer calldata too short"
        recipient = "0x" + calldata[34:74]
        try:
            paid_atomic = int(calldata[74:138], 16)
        except ValueError:
            return False, "unparseable ERC-20 transfer amount"
        if recipient.lower() != pay_to:
            return False, f"transfer recipient {recipient} != server wallet {pay_to}"
        try:
            expected_atomic = asset_to_atomic(amount_usd, asset, network)
        except ValueError as e:
            return False, str(e)
        if paid_atomic < expected_atomic:
            return (
                False,
                f"underpayment: tx paid {paid_atomic} < required {expected_atomic} atomic {asset}",
            )
        return True, ""
    if entry.get("verification") == "native":
        if tx_to != pay_to:
            return False, f"tx recipient {tx_to} != server wallet {pay_to}"
        try:
            value_atomic = int(tx.get("value") or "0x0", 16)
        except (ValueError, TypeError):
            return False, "unparseable tx value"
        try:
            expected_atomic = asset_to_atomic(amount_usd, asset, network)
        except ValueError as e:
            return False, str(e)
        if value_atomic < expected_atomic:
            return (
                False,
                f"underpayment: tx value {value_atomic} < required "
                f"{expected_atomic} atomic {asset}",
            )
        return True, ""
    return False, f"asset {asset} not self-verifiable via EIP-7702 on {network}"


async def _eip7702_verify(
    tx_hash: str, network: str, asset: str, amount_usd: float
) -> dict[str, Any]:
    """Poll RPC for the tx receipt, then assert it paid OUR wallet >= price."""
    network_id = normalize_network_id(network)
    rpc = EIP7702_RPC.get(network_id)
    if not rpc:
        return {
            "verified": False,
            "reason": f"No EIP-7702 RPC for {network_id}",
            "settlement": None,
        }
    try:
        async with httpx.AsyncClient(timeout=X402_TIMEOUT_SECONDS) as client:
            receipt = await _rpc_call(client, rpc, "eth_getTransactionReceipt", [tx_hash])
            if not receipt or receipt.get("status") != "0x1":
                return {
                    "verified": False,
                    "reason": "EIP-7702 receipt not found or tx failed",
                    "settlement": receipt or None,
                    "facilitator": "eip-7702",
                }
            tx = await _rpc_call(client, rpc, "eth_getTransactionByHash", [tx_hash])
            if not tx:
                return {
                    "verified": False,
                    "reason": "EIP-7702 tx not found",
                    "settlement": receipt,
                    "facilitator": "eip-7702",
                }
            ok, reason = _eip7702_matches(tx, network_id, asset, amount_usd)
            if not ok:
                return {
                    "verified": False,
                    "reason": f"EIP-7702 payment mismatch: {reason}",
                    "settlement": receipt,
                    "facilitator": "eip-7702",
                }
            return {
                "verified": True,
                "reason": "",
                "settlement": receipt,
                "facilitator": "eip-7702",
            }
    except (httpx.HTTPError, httpx.RequestError) as e:
        return {"verified": False, "reason": f"EIP-7702 verify failed: {e}", "settlement": None}
