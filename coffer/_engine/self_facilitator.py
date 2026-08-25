# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Rug Munch Media LLC
# --- self_facilitator.py: self-hosted EIP-3009 SelfFacilitator ---
from __future__ import annotations

import os
from typing import Any

import httpx

from .facilitator_config import (
    X402_SELF_FACILITATOR_KEY,
    X402_SELF_FACILITATOR_RPC,
    X402_TIMEOUT_SECONDS,
    X402_WALLET,
)
from .models import VerifyResult
from .pricing import asset_to_atomic, normalize_network_id, resolve_asset_entry

# ── Self-hosted EIP-3009 SelfFacilitator ─────────────────────────────────────


# Verification results use the canonical wire type ``models.VerifyResult``
# (fleet contract §5.1) so the service layer and the HTTP surface share ONE
# result shape. Construct with keyword arguments only.


def _ecrecover_available() -> bool:
    try:
        import eth_keys  # noqa: F401
        import eth_utils  # noqa: F401

        return True
    except ImportError:
        return False


_ECRECOVER_AVAILABLE = _ecrecover_available()


class SelfFacilitator:
    """Self-hosted EVM ``exact`` verifier (EIP-3009 transferWithAuthorization).

    ``verify`` recovers the signer of the EIP-712 digest locally (eth-keys),
    validates amount >= the server-side required atomic amount, recipient ==
    the configured payTo wallet, the validity window, chain id and nonce
    replay. ``settle`` broadcasts the authorization (or a plain transferFrom)
    to the configured RPC. Crypto deps (eth-keys/eth-utils) are optional: if
    absent, verification FAILS CLOSED with a clear reason. [REVIEW]
    """

    def __init__(
        self,
        *,
        pay_to: str = X402_WALLET,
        facilitator_key: str = X402_SELF_FACILITATOR_KEY,
        rpc_url: str = X402_SELF_FACILITATOR_RPC,
        timeout_seconds: float = X402_TIMEOUT_SECONDS,
    ) -> None:
        self.pay_to = (pay_to or X402_WALLET or "").lower()
        self.facilitator_key = facilitator_key or X402_SELF_FACILITATOR_KEY
        self.rpc_url = rpc_url or X402_SELF_FACILITATOR_RPC
        self.timeout_seconds = timeout_seconds
        self.nonces: set[str] = set()

    # EIP-3009 digest (EIP-712). Requires eth_keys/eth_utils — guarded.
    def _digest(self, auth: dict[str, Any], chain_id: int, token: str) -> bytes:
        from eth_utils.address import to_canonical_address  # type: ignore[import-untyped]
        from eth_utils.crypto import keccak  # type: ignore[import-untyped]

        def enc_addr(addr: str) -> bytes:
            return b"\x00" * 12 + to_canonical_address(addr) if True else b""

        def enc_uint(v: int) -> bytes:
            return int(v).to_bytes(32, "big")

        domain_type = keccak(
            text=(
                "EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)"
            )
        )
        struct_type = keccak(
            text="TransferWithAuthorization(address from,address to,uint256 value,"
            "uint256 validAfter,uint256 validBefore,bytes32 nonce)"
        )
        name, version = (
            os.getenv("X402_EIP712_NAME", "USD Coin"),
            os.getenv("X402_EIP712_VERSION", "2"),
        )
        domain = keccak(
            domain_type
            + keccak(text=name)
            + keccak(text=version)
            + enc_uint(chain_id)
            + enc_addr(token)
        )
        nonce = self._nonce_bytes(auth.get("nonce"))
        struct = keccak(
            struct_type
            + enc_addr(str(auth["from"]))
            + enc_addr(str(auth["to"]))
            + enc_uint(int(auth["value"]))
            + enc_uint(int(auth.get("validAfter") or 0))
            + enc_uint(int(auth.get("validBefore") or 0))
            + nonce
        )
        return keccak(b"\x19\x01" + domain + struct)

    @staticmethod
    def _nonce_bytes(nonce: Any) -> bytes:
        if isinstance(nonce, bytes):
            return nonce[-32:].rjust(32, b"\x00")
        if isinstance(nonce, int):
            return nonce.to_bytes(32, "big")
        s = str(nonce or "").removeprefix("0x")
        return bytes.fromhex(s)[-32:].rjust(32, b"\x00") if s else b"\x00" * 32

    @staticmethod
    def _chain_id_for(network: str) -> int | None:
        nid = normalize_network_id(network)
        if nid.startswith("eip155:"):
            try:
                return int(nid.split(":", 1)[1])
            except ValueError:
                return None
        return None

    async def verify(self, payment: dict[str, Any]) -> VerifyResult:
        """Verify an EVM exact payment entirely locally (fail-closed)."""
        network = normalize_network_id(str(payment.get("network") or ""))
        asset = str(payment.get("asset") or "USDC").upper()
        try:
            amount_usd = float(payment.get("amount_usd") or 0.0)
        except (TypeError, ValueError):
            amount_usd = 0.0
        entry = resolve_asset_entry(network, asset)
        if entry is None:
            return VerifyResult(
                verified=False,
                reason=f"asset {asset!r} not supported on {network}",
                asset=asset,
                network=network,
            )
        if entry.get("verification") != "eip3009":
            return VerifyResult(
                verified=False,
                reason="self-facilitator supports eip3009 assets only",
                asset=asset,
                network=network,
            )
        if not _ECRECOVER_AVAILABLE:
            return VerifyResult(
                verified=False,
                reason="eip3009 crypto unavailable: eth-keys not installed",
                asset=asset,
                network=network,
            )
        if not self.pay_to or self.pay_to == "coffer-unconfigured-wallet":
            return VerifyResult(
                verified=False,
                reason="server wallet (payTo) not configured",
                asset=asset,
                network=network,
            )
        auth = payment.get("authorization")
        if not isinstance(auth, dict) or not auth:
            return VerifyResult(
                verified=False,
                reason="missing EIP-3009 transferWithAuthorization",
                asset=asset,
                network=network,
            )
        chain_id = self._chain_id_for(network)
        if chain_id is None:
            return VerifyResult(
                verified=False,
                reason=f"no EVM chain id known for {network}",
                asset=asset,
                network=network,
            )
        token = str(entry.get("contract") or "").lower()
        if not token:
            return VerifyResult(
                verified=False,
                reason=f"no token contract for {asset} on {network}",
                asset=asset,
                network=network,
            )

        try:
            from eth_keys import keys as eth_keys  # type: ignore[import-untyped]
            from eth_utils.address import to_checksum_address  # type: ignore[import-untyped]

            digest = self._digest(auth, chain_id, token)
            sig_hex = str(auth.get("signature") or "").removeprefix("0x")
            raw_sig = bytearray(bytes.fromhex(sig_hex))
            # EIP-155 v-normalization: eth_account/viem emit v ∈ {27, 28};
            # eth_keys expects the recovery id {0, 1}. (Matches the settle
            # path's normalization below — without this EVERY externally
            # signed authorization fails verification.)
            if len(raw_sig) == 65 and raw_sig[64] in (27, 28):
                raw_sig[64] -= 27
            sig = eth_keys.Signature(bytes(raw_sig))
            from_addr = to_checksum_address(str(auth.get("from") or ""))
            recovered = to_checksum_address(
                eth_keys.PublicKey.recover_from_msg_hash(digest, sig).to_address()
            )
            if recovered != from_addr:
                return VerifyResult(
                    verified=False,
                    reason=f"signer {recovered} != authorization from {from_addr}",
                    asset=asset,
                    network=network,
                )
        except Exception as e:
            return VerifyResult(
                verified=False,
                reason=f"cannot verify authorization: {e}",
                asset=asset,
                network=network,
            )

        try:
            value = int(auth["value"])
            valid_after = int(auth.get("validAfter") or 0)
            valid_before = int(auth.get("validBefore") or 0)
        except (ValueError, TypeError, KeyError):
            return VerifyResult(
                verified=False,
                reason="unparseable authorization value/window",
                asset=asset,
                network=network,
            )

        try:
            required_atomic = asset_to_atomic(amount_usd, asset, network)
        except ValueError as e:
            return VerifyResult(verified=False, reason=str(e), asset=asset, network=network)
        if value < required_atomic:
            return VerifyResult(
                verified=False,
                reason=f"underpayment: authorized {value} < required {required_atomic}"
                f" atomic {asset}",
                signer=recovered,
                asset=asset,
                network=network,
            )

        to_addr = str(auth.get("to") or "").lower().lstrip("0x")
        if to_addr and ("0x" + to_addr).lower() != self.pay_to and to_addr.lower() != self.pay_to:
            if str(auth.get("to") or "").lower() != self.pay_to:
                return VerifyResult(
                    verified=False,
                    reason=f"recipient {auth.get('to')} != configured payTo {self.pay_to}",
                    asset=asset,
                    network=network,
                )

        import time as _t

        now = int(_t.time())
        if now < valid_after:
            return VerifyResult(
                verified=False,
                reason="authorization not valid yet (validAfter in future)",
                asset=asset,
                network=network,
            )
        if valid_before and now >= valid_before:
            return VerifyResult(
                verified=False,
                reason="authorization expired (validBefore passed)",
                asset=asset,
                network=network,
            )

        nonce_key = f"{chain_id}:{token}:{self._nonce_bytes(auth.get('nonce')).hex()}"
        if nonce_key in self.nonces:
            return VerifyResult(
                verified=False, reason="nonce already used (replay)", asset=asset, network=network
            )
        self.nonces.add(nonce_key)

        return VerifyResult(
            verified=True,
            reason="",
            signer=recovered,
            amount_atomic=value,
            asset=asset,
            network=network,
        )

    async def settle(self, payment: dict[str, Any]) -> str | None:
        """Sign + broadcast settlement. Returns tx hash, or None if not settlable."""
        if not self.rpc_url or not self.facilitator_key:
            return None
        auth = payment.get("authorization")
        if not isinstance(auth, dict) or not auth:
            return None
        network = normalize_network_id(str(payment.get("network") or ""))
        asset = str(payment.get("asset") or "USDC").upper()
        entry = resolve_asset_entry(network, asset)
        chain_id = self._chain_id_for(network)
        if entry is None or chain_id is None or not entry.get("contract"):
            return None
        try:
            from eth_account import Account  # type: ignore[import-untyped]

            calldata = self._transfer_with_auth_calldata(auth)
        except Exception:
            return None
        try:
            payer = Account.from_key(self.facilitator_key.removeprefix("0x")).address
            nonce_hex = await self._rpc("eth_getTransactionCount", [payer, "pending"])
            if nonce_hex is None:
                return None
            gas_price_hex = await self._rpc("eth_gasPrice", [])
            if gas_price_hex is None:
                return None
            signed = Account.from_key(self.facilitator_key.removeprefix("0x")).sign_transaction(
                {
                    "nonce": int(str(nonce_hex), 16),
                    "gasPrice": int(str(gas_price_hex), 16),
                    "gas": 100_000,
                    "to": entry["contract"],
                    "value": 0,
                    "data": calldata,
                    "chainId": chain_id,
                }
            )
            raw = "0x" + str(signed.raw_transaction.hex())
            tx = await self._rpc("eth_sendRawTransaction", [raw])
            return str(tx) if tx else None
        except (httpx.HTTPError, httpx.RequestError, ValueError, OSError):
            return None

    async def _rpc(self, method: str, params: list[Any]) -> Any:
        """Issue a JSON-RPC call against ``self.rpc_url``; returns result or None."""
        if not self.rpc_url:
            return None
        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            response = await client.post(
                self.rpc_url,
                json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
            )
            response.raise_for_status()
            data = response.json()
            if isinstance(data, dict) and data.get("error"):
                raise ValueError(f"RPC error {method}: {data['error']}")
            return data.get("result") if isinstance(data, dict) else None

    def _transfer_with_auth_calldata(self, auth: dict[str, Any]) -> str:
        from eth_utils.address import to_canonical_address  # type: ignore[import-untyped]
        from eth_utils.crypto import keccak  # type: ignore[import-untyped]

        def enc_addr(addr: str) -> bytes:
            return b"\x00" * 12 + to_canonical_address(addr)

        def enc_uint(v: int) -> bytes:
            return int(v).to_bytes(32, "big")

        selector = keccak(
            text="transferWithAuthorization(address,address,uint256,uint256,uint256,bytes32,uint8,bytes32,bytes32)"
        )[:4]
        sig_hex = str(auth.get("signature") or "").removeprefix("0x")
        raw = bytes.fromhex(sig_hex)
        v = ((raw[64] - 27) if raw[64] in (27, 28) else raw[64]).to_bytes(32, "big")
        calldata = (
            selector
            + enc_addr(str(auth["from"]))
            + enc_addr(str(auth["to"]))
            + enc_uint(int(auth["value"]))
            + enc_uint(int(auth.get("validAfter") or 0))
            + enc_uint(int(auth.get("validBefore") or 0))
            + self._nonce_bytes(auth.get("nonce"))
            + v
            + raw[0:32].rjust(32, b"\x00")
            + raw[32:64].rjust(32, b"\x00")
        )
        return "0x" + calldata.hex()
