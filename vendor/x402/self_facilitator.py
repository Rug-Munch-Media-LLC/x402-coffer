"""x402 — self-hosted facilitator (no external facilitator / Coinbase CDP).

Implements EVM ``exact``-scheme verify + settle entirely self-hosted:

- **verify**: ecrecover the EIP-3009 ``transferWithAuthorization`` signer
  locally (via ``eth_keys``), then validate amount >= price, recipient == the
  configured ``payTo`` wallet, validAfter/validBefore window, chain id and
  nonce. Replays are rejected by a bounded in-memory nonce set with optional
  JSON persistence (``X402_DIR/self_facilitator_nonces.json``).
- **settle**: sign + broadcast the authorization (or a plain
  ``transferFrom`` when the payer pre-approved) to the configured RPC via
  httpx, returning the tx hash (or ``None`` when not settlable / chain
  unavailable / missing key). Settlement is hardened: idempotent
  (an already-settled payment returns the existing tx hash, never
  double-submits), tracked in bounded ``pending_settlements`` /
  ``_settled_txs`` structures, and failures are classified into a
  :class:`SettleErrorCode` taxonomy (insufficient gas, nonce conflict, RPC
  timeout, wrong chain, …) via :meth:`SelfFacilitator.settle_with_details`.

Crypto: ``eth-keys`` (ecrecover + digest signing) and ``eth-account``
(transaction signing / RLP). Keccak backend is ``eth-hash[pycryptodome]``.
These are new [project] dependencies added in pyproject.toml — [REVIEW] lead
reconciles. Nothing here talks to Coinbase / CDP / any external facilitator.

The EIP-712 domain defaults to Circle USDC (name "USD Coin", version "2").
Operators using a different EIP-3009 token MUST verify the token's
domain name/version (``domain_name`` / ``domain_version`` args or
``X402_EIP712_NAME`` / ``X402_EIP712_VERSION`` env). [REVIEW]

This module is NOT wired into the ASGI middleware yet (the middleware is
out of scope for this change): to use self-facilitation end-to-end, either
call ``SelfFacilitator`` directly or attach the authorization to the router
via ``FacilitatorRouter.submit_authorization(payment_id, authorization)``.
Middleware-level plumbing is a follow-up. [REVIEW]
"""
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Rug Munch Media LLC
#
# Standalone x402 implementation — https://git.rugmunch.io/RugMunchMedia/x402-py
# Licensed under MIT. See LICENSE.

from __future__ import annotations

import json
import logging
import time
from collections import OrderedDict, deque
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

import httpx
from eth_account import Account
from eth_keys.datatypes import PrivateKey, PublicKey, Signature
from eth_utils.address import to_canonical_address, to_checksum_address
from eth_utils.crypto import keccak

from x402 import (
    X402_DEFAULT_ASSET,
    X402_DIR,
    X402_SELF_FACILITATOR_KEY,
    X402_SELF_FACILITATOR_RPC,
    X402_TIMEOUT_SECONDS,
    X402_UNCONFIGURED_WALLET,
    X402_WALLET,
    _env,
)
from x402.scheme import (
    X402Scheme,
    get_asset_usd_price,
    normalize_network_id,
    resolve_asset_entry,
)

logger = logging.getLogger(__name__)

# CAIP-2 / legacy network name -> EVM chain id (used for the EIP-712 domain
# and the signed transaction's EIP-155 v). eip155:NNNN ids not listed here
# are parsed directly from the CAIP-2 suffix.
CHAIN_ID_BY_NETWORK: dict[str, int] = {
    "eip155:8453": 8453,
    "base": 8453,
    "eip155:1": 1,
    "ethereum": 1,
    "eip155:42161": 42161,
    "arbitrum": 42161,
    "eip155:10": 10,
    "optimism": 10,
    "eip155:137": 137,
    "polygon": 137,
    "eip155:56": 56,
    "bnb": 56,
    "eip155:43114": 43114,
    "avalanche": 43114,
    "eip155:84532": 84532,
    "base-sepolia": 84532,
}

# EIP-712 typehashes (EIP-3009 / EIP-712 domain).
_EIP712_DOMAIN_TYPEHASH = keccak(
    text="EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)"
)
_TRANSFER_WITH_AUTH_TYPEHASH = keccak(
    text="TransferWithAuthorization("
    "address from,address to,uint256 value,uint256 validAfter,"
    "uint256 validBefore,bytes32 nonce)"
)

# ERC-20 selectors used to build settlement calldata.
_TRANSFER_WITH_AUTH_SELECTOR = keccak(
    text="transferWithAuthorization(address,address,uint256,uint256,uint256,bytes32,uint8,bytes32,bytes32)"
)[:4]
_TRANSFER_FROM_SELECTOR = keccak(text="transferFrom(address,address,uint256)")[:4]

# Fixed gas limit fallback for ERC-20 settlement if eth_estimateGas is
# unavailable (USDC transferWithAuthorization costs ~65k; 100k is safe).
_DEFAULT_TX_GAS = 100_000



@dataclass
class VerifyResult:
    """Outcome of a self-hosted x402 payment verification.

    Attributes:
        verified: True when the payment satisfies the EIP-3009 checks.
        reason: Human-readable failure reason (empty when verified).
        signer: Recovered/expected signer address (checksummed).
        amount_atomic: Authorized value in atomic units.
        asset: Asset symbol (e.g. "USDC").
        network: Normalized CAIP-2 network id.
        scheme: Payment scheme ("exact").
    """

    verified: bool
    reason: str = ""
    signer: str = ""
    amount_atomic: int = 0
    asset: str = ""
    network: str = ""
    scheme: str = X402Scheme.EXACT.value


class SettleErrorCode(StrEnum):
    """Taxonomy of self-hosted settlement failures.

    Classified so callers and operators can react precisely: retry on
    ``rpc_timeout``, fund the facilitator key on ``insufficient_gas``, and
    treat ``nonce_conflict`` as a re-submission hazard (never blindly
    re-broadcast — idempotency already guards this).
    """

    NOT_SETTLABLE = "not_settlable"
    WRONG_CHAIN = "wrong_chain"
    INSUFFICIENT_GAS = "insufficient_gas"
    NONCE_CONFLICT = "nonce_conflict"
    RPC_TIMEOUT = "rpc_timeout"
    RPC_ERROR = "rpc_error"


@dataclass
class SettleOutcome:
    """Result of a self-hosted settlement attempt with error taxonomy.

    ``success`` True + ``tx_hash`` set means the authorization was broadcast
    (or had already been settled — ``idempotent`` True, never double-submitted).
    On failure ``error_code`` is one of :class:`SettleErrorCode` and
    ``error_detail`` carries the underlying message.
    """

    success: bool
    tx_hash: str | None = None
    error_code: str = ""
    error_detail: str = ""
    idempotent: bool = False


# RPC error-message markers used to classify settlement failures.
_RPC_INSUFFICIENT_GAS_MARKERS = (
    "insufficient funds",
    "insufficient balance",
    "out of gas",
    "gas required exceeds allowance",
)
_RPC_NONCE_MARKERS = (
    "nonce too low",
    "nonce too high",
    "replacement transaction underpriced",
    "same nonce",
)


def _classify_settle_error(exc: BaseException) -> tuple[str, str]:
    """Map an RPC/httpx failure to a :class:`SettleErrorCode` + detail."""
    detail = str(exc)
    lowered = detail.lower()
    if isinstance(exc, httpx.TimeoutException):
        return SettleErrorCode.RPC_TIMEOUT.value, detail
    if any(marker in lowered for marker in _RPC_INSUFFICIENT_GAS_MARKERS):
        return SettleErrorCode.INSUFFICIENT_GAS.value, detail
    if any(marker in lowered for marker in _RPC_NONCE_MARKERS):
        return SettleErrorCode.NONCE_CONFLICT.value, detail
    return SettleErrorCode.RPC_ERROR.value, detail


# ── ABI encoding helpers (minimal EIP-712 subset) ────────────────────────────


def _encode_address(addr: str) -> bytes:
    return b"\x00" * 12 + to_canonical_address(addr)


def _encode_uint(value: int) -> bytes:
    return value.to_bytes(32, "big")


def _encode_bytes32(value: bytes) -> bytes:
    if len(value) < 32:
        return value.rjust(32, b"\x00")
    return value[-32:]


def _to_int(value: Any) -> int:
    """Parse an int from int / decimal str / 0x-hex str (EVM fields)."""
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        s = value.strip()
        if s.startswith(("0x", "0X")):
            return int(s, 16)
        return int(s, 10)
    raise ValueError(f"cannot parse {value!r} as integer")


def _encode_nonce(nonce: Any) -> bytes:
    """Normalize an EIP-3009 nonce (bytes32) to 32 bytes."""
    if isinstance(nonce, bytes):
        return _encode_bytes32(nonce)
    if isinstance(nonce, int):
        return nonce.to_bytes(32, "big")
    value = str(nonce or "")
    if value.startswith(("0x", "0X")):
        raw = bytes.fromhex(value[2:])
    else:
        raw = bytes.fromhex(value) if value else b"\x00"
    return _encode_bytes32(raw)


def _nonce_hex(nonce: Any) -> str:
    """Canonical lowercase hex string for a nonce (replay key component)."""
    return _encode_nonce(nonce).hex()


# ── EIP-712 / EIP-3009 digest ────────────────────────────────────────────────


def domain_separator(
    name: str,
    version: str,
    chain_id: int,
    verifying_contract: str,
) -> bytes:
    """Compute the EIP-712 domain separator for an EIP-3009 token."""
    return keccak(
        _EIP712_DOMAIN_TYPEHASH
        + keccak(text=name)
        + keccak(text=version)
        + _encode_uint(chain_id)
        + _encode_address(verifying_contract)
    )


def transfer_with_authorization_struct_hash(
    *,
    from_address: str,
    to_address: str,
    value: int,
    valid_after: int,
    valid_before: int,
    nonce: Any,
) -> bytes:
    """Compute the EIP-712 struct hash for ``TransferWithAuthorization``."""
    return keccak(
        _TRANSFER_WITH_AUTH_TYPEHASH
        + _encode_address(from_address)
        + _encode_address(to_address)
        + _encode_uint(value)
        + _encode_uint(valid_after)
        + _encode_uint(valid_before)
        + _encode_nonce(nonce)
    )


def build_transfer_with_authorization_digest(
    *,
    from_address: str,
    to_address: str,
    value: int,
    valid_after: int,
    valid_before: int,
    nonce: Any,
    chain_id: int,
    token_address: str,
    name: str = "USD Coin",
    version: str = "2",
) -> bytes:
    """Compute the EIP-712 signed digest for an EIP-3009 authorization.

    The digest is what the payer signs: ``keccak("\x19\x01" || domain || struct)``
    per EIP-712 / EIP-3009.
    """
    domain = domain_separator(name, version, chain_id, token_address)
    struct = transfer_with_authorization_struct_hash(
        from_address=from_address,
        to_address=to_address,
        value=value,
        valid_after=valid_after,
        valid_before=valid_before,
        nonce=nonce,
    )
    return keccak(b"\x19\x01" + domain + struct)


# ── Signature helpers ────────────────────────────────────────────────────────


def _signature_from_bytes(r: bytes, s: bytes, v: int) -> Signature | None:
    """Build an eth_keys Signature, normalizing v (27/28 -> 0/1).

    eth-keys 0.7 stores r/s as ints and requires v in {0, 1}.
    """
    if len(r) != 32 or len(s) != 32:
        return None
    v_norm = int(v)
    if v_norm in (27, 28):
        v_norm -= 27
    if v_norm not in (0, 1):
        return None
    return Signature(vrs=(v_norm, int.from_bytes(r, "big"), int.from_bytes(s, "big")))


def _parse_signature(auth: dict[str, Any]) -> Signature | None:
    """Parse an EIP-3009 signature from ``signature`` (65-byte hex/bytes) or v/r/s.

    r/s may be hex strings, ints, or bytes; v may be 0/1 or 27/28.
    Odd-length hex (top nibble 0, e.g. ``hex()`` output) is left-padded.
    """
    signature = auth.get("signature")
    if signature:
        if isinstance(signature, (bytes, bytearray)):
            raw = bytes(signature)
        else:
            try:
                raw = bytes.fromhex(str(signature).removeprefix("0x"))
            except ValueError:
                return None
        if len(raw) != 65:
            return None
        r, s, v = raw[:32], raw[32:64], int(raw[64])
        return _signature_from_bytes(r, s, v)
    r_value = auth.get("r")
    s_value = auth.get("s")
    if r_value is None or s_value is None:
        return None
    try:
        r = r_value.to_bytes(32, "big") if isinstance(r_value, int) else _hex_to_bytes(str(r_value))
        s = s_value.to_bytes(32, "big") if isinstance(s_value, int) else _hex_to_bytes(str(s_value))
        v = int(auth.get("v", 27))
    except (ValueError, OverflowError):
        return None
    return _signature_from_bytes(r, s, v)


def _hex_to_bytes(value: str) -> bytes:
    """Parse a hex string to bytes, left-padding odd-length input."""
    s = value.removeprefix("0x")
    if len(s) % 2:
        s = "0" + s
    return bytes.fromhex(s)


def _chain_id_for(network: str) -> int | None:
    """Resolve the EVM chain id for a legacy name or CAIP-2 network id."""
    network_id = normalize_network_id(network)
    if network_id in CHAIN_ID_BY_NETWORK:
        return CHAIN_ID_BY_NETWORK[network_id]
    if network_id.startswith("eip155:"):
        try:
            return int(network_id.split(":", 1)[1])
        except ValueError:
            return None
    return None


# ── Replay protection ────────────────────────────────────────────────────────


class NonceStore:
    """Bounded set of used EIP-3009 nonces with optional JSON persistence.

    Replay protection is keyed by ``chain_id:token:nonce`` (a nonce is only
    unique per token contract). The store is bounded (oldest entries are
    evicted past ``max_size``) and persisted to ``path`` as JSON so used
    nonces survive restarts.
    """

    def __init__(self, path: Path | str | None = None, max_size: int = 10_000) -> None:
        self._path = Path(path) if path else None
        self._max_size = max(int(max_size), 1)
        self._used: set[str] = set()
        self._order: deque[str] = deque()
        self._load()

    def _load(self) -> None:
        if self._path is None or not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text())
        except (json.JSONDecodeError, OSError):
            return
        nonces = data.get("nonces") if isinstance(data, dict) else data
        if not isinstance(nonces, list):
            return
        for nonce in nonces:
            if isinstance(nonce, str) and nonce not in self._used:
                self._used.add(nonce)
                self._order.append(nonce)
                self._trim()

    def contains(self, nonce_key: str) -> bool:
        return nonce_key in self._used

    def mark_used(self, nonce_key: str) -> bool:
        """Record a used nonce key. Returns False when it was already used."""
        if nonce_key in self._used:
            return False
        self._used.add(nonce_key)
        self._order.append(nonce_key)
        self._trim()
        self._persist()
        return True

    def _trim(self) -> None:
        while len(self._order) > self._max_size:
            oldest = self._order.popleft()
            self._used.discard(oldest)

    def _persist(self) -> None:
        if self._path is None:
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(
                json.dumps(
                    {"nonces": sorted(self._used), "max_size": self._max_size},
                    sort_keys=True,
                )
            )
        except OSError as e:
            logger.warning("x402_nonce_persist_failed", extra={"error": str(e)})

    def clear(self) -> None:
        self._used.clear()
        self._order.clear()

    @property
    def count(self) -> int:
        return len(self._used)


# ── The facilitator ──────────────────────────────────────────────────────────


class SelfFacilitator:
    """Self-hosted x402 facilitator (EVM ``exact`` scheme).

    Args:
        pay_to: Receiving wallet. Must equal the authorization's ``to``.
            Defaults to ``X402_WALLET``; verification fails closed while the
            wallet is the ``coffer-unconfigured-wallet`` placeholder.
        facilitator_key: EVM private key used to pay gas when broadcasting
            settlement. Optional for verify; required for settle.
        rpc_url: JSON-RPC endpoint for settlement broadcast (optional; verify
            is fully local).
        domain_name / domain_version: EIP-712 domain of the EIP-3009 token
            (defaults to Circle USDC "USD Coin" / "2").
        nonce_path: JSON file backing the nonce store (defaults to
            ``X402_DIR/self_facilitator_nonces.json``).
        max_nonces: Bound of the in-memory replay set.
        transport: Optional httpx transport (tests).
        max_settled: Bound of the idempotent settled-tx tracker.
        max_pending: Bound of the in-flight pending-settlement tracker.
    """

    def __init__(
        self,
        *,
        pay_to: str = "",
        facilitator_key: str = "",
        rpc_url: str = "",
        domain_name: str = "",
        domain_version: str = "",
        nonce_path: Path | str | None = None,
        max_nonces: int = 10_000,
        timeout_seconds: float = X402_TIMEOUT_SECONDS,
        transport: httpx.AsyncBaseTransport | None = None,
        max_settled: int = 10_000,
        max_pending: int = 10_000,
    ) -> None:
        self.pay_to = self._normalize_address(pay_to or X402_WALLET)
        self.facilitator_key = facilitator_key or X402_SELF_FACILITATOR_KEY
        self.rpc_url = rpc_url or X402_SELF_FACILITATOR_RPC
        self.domain_name = domain_name or _env("X402_EIP712_NAME", "USD Coin")
        self.domain_version = domain_version or _env("X402_EIP712_VERSION", "2")
        self.timeout_seconds = timeout_seconds
        self.nonces = NonceStore(
            path=nonce_path or X402_DIR / "self_facilitator_nonces.json",
            max_size=max_nonces,
        )
        self._transport = transport
        key_hex = self.facilitator_key.removeprefix("0x") if self.facilitator_key else ""
        self._signer: PrivateKey | None = PrivateKey(bytes.fromhex(key_hex)) if len(key_hex) == 64 else None
        self._acct: Any = Account.from_key(bytes.fromhex(key_hex)) if self._signer else None
        # Settlement hardening: bounded idempotency + in-flight trackers.
        self._settled_txs: OrderedDict[str, Any] = OrderedDict()
        self._max_settled = max(int(max_settled), 1)
        self._pending_settlements: OrderedDict[str, Any] = OrderedDict()
        self._max_pending = max(int(max_pending), 1)

    # ── settlement trackers ──────────────────────────────────────────────────

    @property
    def pending_count(self) -> int:
        """Number of settlement attempts currently in-flight (pending)."""
        return sum(1 for s in self._pending_settlements.values() if s.get("status") == "pending")

    def _settlement_id(self, payment: dict[str, Any]) -> str:
        """Idempotency key for a payment: payment_id, else EIP-3009 nonce key."""
        pid = str(payment.get("payment_id") or "").strip()
        if pid:
            return pid
        auth = payment.get("authorization")
        if not isinstance(auth, dict) or auth.get("nonce") is None:
            return ""
        network = normalize_network_id(str(payment.get("network") or ""))
        asset = str(payment.get("asset") or X402_DEFAULT_ASSET).upper()
        entry = resolve_asset_entry(network, asset)
        chain_id = _chain_id_for(network) or 0
        token = str(entry["contract"]).lower() if entry else ""
        return f"nonce:{chain_id}:{token}:{_nonce_hex(auth.get('nonce'))}"

    def _mark_pending(self, payment_id: str) -> None:
        self._pending_settlements[payment_id] = {
            "status": "pending",
            "started_at": time.time(),
        }
        self._trim(self._pending_settlements, self._max_pending)

    def _mark_settled(self, payment_id: str, tx_hash: str) -> None:
        entry = self._pending_settlements.get(payment_id)
        if entry is None:
            entry = {}
            self._pending_settlements[payment_id] = entry
        entry["status"] = "settled"
        entry["tx_hash"] = tx_hash
        entry["settled_at"] = time.time()
        self._settled_txs[payment_id] = tx_hash
        self._settled_txs.move_to_end(payment_id)
        self._trim(self._settled_txs, self._max_settled)

    def _mark_pending_failed(self, payment_id: str, code: str, detail: str) -> None:
        entry = self._pending_settlements.get(payment_id)
        if entry is None:
            entry = {}
            self._pending_settlements[payment_id] = entry
        entry["status"] = "failed"
        entry["error_code"] = code
        entry["error_detail"] = detail
        entry["failed_at"] = time.time()

    @staticmethod
    def _trim(ordered: OrderedDict[str, Any], max_size: int) -> None:
        while len(ordered) > max_size:
            ordered.popitem(last=False)

    @staticmethod
    def _normalize_address(value: str) -> str:
        try:
            return to_checksum_address(value)
        except (ValueError, TypeError):
            return value

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=self.timeout_seconds, transport=self._transport)

    async def _rpc_call(self, client: httpx.AsyncClient, method: str, params: list[Any]) -> Any:
        """Issue a JSON-RPC call against ``self.rpc_url``; returns result or None."""
        if not self.rpc_url:
            return None
        response = await client.post(
            self.rpc_url,
            json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
        )
        response.raise_for_status()
        data = response.json()
        if isinstance(data, dict) and data.get("error"):
            raise ValueError(f"RPC error {method}: {data['error']}")
        return data.get("result") if isinstance(data, dict) else None

    # ── verify ───────────────────────────────────────────────────────────────

    async def verify(self, payment: dict[str, Any]) -> VerifyResult:
        """Verify an EVM ``exact`` payment entirely locally.

        ``payment`` must carry:
        - ``network`` (legacy name or CAIP-2), ``asset``, ``amount_usd``
          (minimum required USD);
        - ``authorization``: the EIP-3009 ``transferWithAuthorization``
          fields + signature (``from``, ``to``, ``value``, ``validAfter``,
          ``validBefore``, ``nonce`` and either ``signature`` (65-byte hex) or
          ``v``/``r``/``s``).

        Returns a :class:`VerifyResult`. Replays are rejected via the nonce
        store. The signer recovery, amount, recipient, validity window, chain
        id and asset support are all validated; anything off fails closed.
        """
        network = normalize_network_id(str(payment.get("network") or ""))
        asset = str(payment.get("asset") or X402_DEFAULT_ASSET).upper()
        try:
            amount_usd = float(payment.get("amount_usd") or 0.0)
        except (TypeError, ValueError):
            amount_usd = 0.0

        entry = resolve_asset_entry(network, asset)
        if entry is None:
            return VerifyResult(
                False,
                f"asset {asset!r} not supported on {network}",
                asset=asset,
                network=network,
            )
        verification = entry.get("verification")
        if verification != "eip3009":
            return VerifyResult(
                False,
                f"verification method {verification!r} is not EIP-3009 — self-facilitator supports eip3009 assets only",
                asset=asset,
                network=network,
            )
        auth = payment.get("authorization")
        if not isinstance(auth, dict) or not auth:
            return VerifyResult(
                False,
                "missing EIP-3009 transferWithAuthorization in payment",
                asset=asset,
                network=network,
            )

        if self.pay_to == X402_UNCONFIGURED_WALLET or not self.pay_to:
            return VerifyResult(False, "server wallet (payTo) not configured", asset=asset, network=network)

        chain_id = _chain_id_for(network)
        if chain_id is None:
            return VerifyResult(False, f"no EVM chain id known for {network}", asset=asset, network=network)
        if auth.get("chainId") is not None:
            try:
                if _to_int(auth["chainId"]) != chain_id:
                    return VerifyResult(
                        False,
                        f"authorization chainId {auth['chainId']} != configured {chain_id}",
                        asset=asset,
                        network=network,
                    )
            except ValueError:
                return VerifyResult(False, "unparseable authorization chainId", asset=asset, network=network)

        # Recover the signer first — tampered fields (amount, recipient, …)
        # change the digest and therefore the recovered signer.
        signature = _parse_signature(auth)
        if signature is None:
            return VerifyResult(False, "malformed EIP-3009 signature", asset=asset, network=network)
        try:
            from_address = to_checksum_address(str(auth.get("from") or ""))
        except (ValueError, TypeError):
            return VerifyResult(False, "missing/invalid authorization 'from'", asset=asset, network=network)
        try:
            digest = build_transfer_with_authorization_digest(
                from_address=from_address,
                to_address=self._normalize_address(str(auth.get("to") or "")),
                value=_to_int(auth.get("value")),
                valid_after=_to_int(auth.get("validAfter") or 0),
                valid_before=_to_int(auth.get("validBefore") or 0),
                nonce=auth.get("nonce"),
                chain_id=chain_id,
                token_address=str(entry["contract"]),
                name=self.domain_name,
                version=self.domain_version,
            )
            recovered = PublicKey.recover_from_msg_hash(digest, signature)
        except (ValueError, TypeError) as e:
            return VerifyResult(False, f"cannot verify authorization: {e}", asset=asset, network=network)
        recovered_address = to_checksum_address(recovered.to_address())
        if recovered_address != from_address:
            return VerifyResult(
                False,
                f"signer {recovered_address} != authorization from {from_address}",
                asset=asset,
                network=network,
            )

        # Amount: authorized value must be >= the server-side required amount
        # (converted to atomic units using the live-oracle/static USD quote).
        try:
            value = _to_int(auth.get("value"))
        except ValueError:
            return VerifyResult(False, "unparseable authorization value", asset=asset, network=network)
        decimals = int(entry.get("decimals", 6))
        quote = await get_asset_usd_price(asset, network)
        if not quote.usd or quote.usd <= 0:
            return VerifyResult(False, f"no USD price available for {asset}", asset=asset, network=network)
        required_atomic = round(amount_usd / quote.usd * (10**decimals)) if amount_usd > 0 else 1
        if value < required_atomic:
            return VerifyResult(
                False,
                f"underpayment: authorized {value} < required {required_atomic} atomic {asset}",
                signer=recovered_address,
                asset=asset,
                network=network,
            )

        # Recipient must be our configured payTo wallet.
        try:
            to_address = to_checksum_address(str(auth.get("to") or ""))
        except (ValueError, TypeError):
            return VerifyResult(False, "missing/invalid authorization 'to'", asset=asset, network=network)
        if to_address != self.pay_to:
            return VerifyResult(
                False,
                f"recipient {to_address} != configured payTo {self.pay_to}",
                signer=recovered_address,
                asset=asset,
                network=network,
            )

        # Validity window (Unix seconds). validBefore == 0 means no expiry
        # (some clients omit it); otherwise must not be in the past.
        now = int(time.time())
        try:
            valid_after = _to_int(auth.get("validAfter") or 0)
            valid_before = _to_int(auth.get("validBefore") or 0)
        except ValueError:
            return VerifyResult(
                False,
                "unparseable authorization validity window",
                signer=recovered_address,
                asset=asset,
                network=network,
            )
        if now < valid_after:
            return VerifyResult(
                False,
                "authorization not valid yet (validAfter in the future)",
                signer=recovered_address,
                asset=asset,
                network=network,
            )
        if valid_before and now >= valid_before:
            return VerifyResult(
                False,
                "authorization expired (validBefore passed)",
                signer=recovered_address,
                asset=asset,
                network=network,
            )

        # Replay protection: the nonce must be unused (per token contract).
        nonce_key = f"{chain_id}:{str(entry['contract']).lower()}:{_nonce_hex(auth.get('nonce'))}"
        if not self.nonces.mark_used(nonce_key):
            return VerifyResult(
                False,
                "nonce already used (replay)",
                signer=recovered_address,
                amount_atomic=value,
                asset=asset,
                network=network,
            )

        return VerifyResult(
            True,
            "",
            signer=recovered_address,
            amount_atomic=value,
            asset=asset,
            network=network,
        )

    # ── settle ───────────────────────────────────────────────────────────────

    async def settle(self, payment: dict[str, Any]) -> str | None:
        """Sign + broadcast settlement. Returns the tx hash, or None.

        Legacy contract (returns ``str | None``). For the hardened result
        with idempotency + error taxonomy use :meth:`settle_with_details`.

        Settlement order:
        1. ``transferWithAuthorization`` broadcast when the payment carries an
           authorization (anyone may broadcast; our key pays the gas);
        2. plain ``transferFrom`` when the payment is flagged ``approved``
           (payer pre-approved our wallet) — our key is the caller;
        3. otherwise None (not settlable / chain unavailable / missing key).

        Idempotent: settling an already-settled payment returns the existing
        tx hash without re-broadcasting.

        RPC calls: eth_chainId, eth_getTransactionCount, eth_estimateGas,
        eth_gasPrice, eth_sendRawTransaction.
        """
        outcome = await self.settle_with_details(payment)
        return outcome.tx_hash if outcome.success else None

    async def settle_with_details(self, payment: dict[str, Any]) -> SettleOutcome:
        """Sign + broadcast settlement with idempotency + error taxonomy.

        Never double-submits: an already-settled payment (by ``payment_id``,
        or by EIP-3009 nonce when no id is present) returns the existing tx
        hash with ``idempotent=True``. Failures are classified into
        :class:`SettleErrorCode` (insufficient gas, nonce conflict, RPC
        timeout, …) so callers can react precisely instead of guessing.
        """
        if not self.rpc_url or self._acct is None:
            return SettleOutcome(
                False,
                error_code=SettleErrorCode.NOT_SETTLABLE.value,
                error_detail="no RPC URL or facilitator key configured",
            )

        network = normalize_network_id(str(payment.get("network") or ""))
        asset = str(payment.get("asset") or X402_DEFAULT_ASSET).upper()
        entry = resolve_asset_entry(network, asset)
        if entry is None:
            return SettleOutcome(
                False,
                error_code=SettleErrorCode.NOT_SETTLABLE.value,
                error_detail=f"asset {asset!r} not supported on {network}",
            )
        chain_id = _chain_id_for(network)
        if chain_id is None:
            return SettleOutcome(
                False,
                error_code=SettleErrorCode.NOT_SETTLABLE.value,
                error_detail=f"no EVM chain id known for {network}",
            )

        # Idempotency: an already-settled payment returns the existing hash —
        # never re-broadcast (double-spend of the same authorization).
        settle_id = self._settlement_id(payment)
        if settle_id and settle_id in self._settled_txs:
            existing = str(self._settled_txs[settle_id])
            logger.info("x402_self_settle_idempotent", extra={"settle_id": settle_id})
            return SettleOutcome(True, tx_hash=existing, idempotent=True)

        auth = payment.get("authorization")
        if isinstance(auth, dict) and auth:
            try:
                data = self._build_transfer_with_auth_calldata(auth)
            except (ValueError, KeyError) as e:
                return SettleOutcome(
                    False,
                    error_code=SettleErrorCode.NOT_SETTLABLE.value,
                    error_detail=f"malformed authorization: {e}",
                )
            to_address = self._normalize_address(str(entry["contract"]))
        elif payment.get("approved"):
            data = self._build_transfer_from_calldata(payment)
            to_address = self._normalize_address(str(entry["contract"]))
        else:
            logger.info(
                "x402_self_settle_not_settlable",
                extra={"network": network, "asset": asset},
            )
            return SettleOutcome(
                False,
                error_code=SettleErrorCode.NOT_SETTLABLE.value,
                error_detail="no authorization and payment not pre-approved",
            )

        if self.pay_to == X402_UNCONFIGURED_WALLET or not self.pay_to:
            return SettleOutcome(
                False,
                error_code=SettleErrorCode.NOT_SETTLABLE.value,
                error_detail="server wallet (payTo) not configured",
            )

        pid = settle_id or str(payment.get("payment_id") or "pending")
        self._mark_pending(pid)
        try:
            async with self._client() as client:
                # Refuse to broadcast on the wrong chain.
                onchain_chain = await self._rpc_call(client, "eth_chainId", [])
                if onchain_chain is not None:
                    try:
                        if int(str(onchain_chain), 16) != chain_id:
                            detail = f"RPC chain {onchain_chain} != expected {chain_id}"
                            logger.warning(
                                "x402_self_settle_wrong_chain",
                                extra={"rpc_chain": onchain_chain, "expected": chain_id},
                            )
                            self._mark_pending_failed(pid, SettleErrorCode.WRONG_CHAIN.value, detail)
                            return SettleOutcome(
                                False,
                                error_code=SettleErrorCode.WRONG_CHAIN.value,
                                error_detail=detail,
                            )
                    except ValueError:
                        detail = "unparseable eth_chainId result"
                        self._mark_pending_failed(pid, SettleErrorCode.RPC_ERROR.value, detail)
                        return SettleOutcome(
                            False,
                            error_code=SettleErrorCode.RPC_ERROR.value,
                            error_detail=detail,
                        )
                tx = await self._send_tx(client, to=to_address, data=data, chain_id=chain_id, value=0)
        except (httpx.HTTPError, httpx.RequestError, ValueError, OSError) as e:
            code, detail = _classify_settle_error(e)
            logger.warning(
                "x402_self_settle_failed",
                extra={"error_code": code, "error": detail},
            )
            self._mark_pending_failed(pid, code, detail)
            return SettleOutcome(False, error_code=code, error_detail=detail)
        if not tx:
            detail = "broadcast returned no tx hash"
            self._mark_pending_failed(pid, SettleErrorCode.RPC_ERROR.value, detail)
            return SettleOutcome(False, error_code=SettleErrorCode.RPC_ERROR.value, error_detail=detail)
        self._mark_settled(pid, tx)
        return SettleOutcome(True, tx_hash=tx)

    def _build_transfer_with_auth_calldata(self, auth: dict[str, Any]) -> str:
        """Calldata for ERC-20 ``transferWithAuthorization(...)``."""
        signature = _parse_signature(auth)
        if signature is None:
            raise ValueError("malformed EIP-3009 signature for settlement")
        v = int(signature.v)
        if v in (27, 28):
            v -= 27
        calldata = (
            _TRANSFER_WITH_AUTH_SELECTOR
            + _encode_address(str(auth["from"]))
            + _encode_address(str(auth["to"]))
            + _encode_uint(_to_int(auth["value"]))
            + _encode_uint(_to_int(auth.get("validAfter") or 0))
            + _encode_uint(_to_int(auth.get("validBefore") or 0))
            + _encode_nonce(auth.get("nonce"))
            + _encode_uint(v)
            + _encode_bytes32(signature.r.to_bytes(32, "big"))
            + _encode_bytes32(signature.s.to_bytes(32, "big"))
        )
        return "0x" + calldata.hex()

    def _build_transfer_from_calldata(self, payment: dict[str, Any]) -> str:
        """Calldata for ERC-20 ``transferFrom(payer, payTo, value)``."""
        calldata = (
            _TRANSFER_FROM_SELECTOR
            + _encode_address(str(payment.get("from") or ""))
            + _encode_address(self.pay_to)
            + _encode_uint(_to_int(payment.get("value") or 0))
        )
        return "0x" + calldata.hex()

    async def _send_tx(
        self,
        client: httpx.AsyncClient,
        *,
        to: str,
        data: str,
        chain_id: int,
        value: int,
    ) -> str | None:
        """Fetch nonce/gas, sign a legacy tx, broadcast, return tx hash."""
        if self._acct is None:
            return None  # no settlement key — contract: None when not settlable
        payer = self._signer.public_key.to_checksum_address() if self._signer else ""
        nonce_hex = await self._rpc_call(client, "eth_getTransactionCount", [payer, "pending"])
        if nonce_hex is None:
            return None
        nonce = int(str(nonce_hex), 16)

        gas_limit = _DEFAULT_TX_GAS
        try:
            estimated = await self._rpc_call(
                client,
                "eth_estimateGas",
                [{"from": payer, "to": to, "data": data, "value": hex(value)}],
            )
            if estimated is not None:
                gas_limit = int(str(estimated), 16)
        except (ValueError, httpx.HTTPError):
            pass

        gas_price_hex = await self._rpc_call(client, "eth_gasPrice", [])
        if gas_price_hex is None:
            return None
        gas_price = int(str(gas_price_hex), 16)

        signed = self._acct.sign_transaction(
            {
                "nonce": nonce,
                "gasPrice": gas_price,
                "gas": gas_limit,
                "to": to,
                "value": value,
                "data": data,
                "chainId": chain_id,
            }
        )
        raw = str(signed.raw_transaction.hex())
        result = await self._rpc_call(client, "eth_sendRawTransaction", ["0x" + raw.removeprefix("0x")])
        return str(result) if result else None
