"""EIP-2612 permit crypto for the x402 gas-sponsoring extensions.

Split out of ``gas_sponsoring.py`` so the pure crypto layer (EIP-712 domain
separator, ``Permit`` struct hash, ``ecrecover``-based signature verification,
and permit ``calldata`` building) lives apart from the facilitator facade.

Owned by this module (byte-identical behavior to the former single file):

* the EIP-2612 ``permit()`` signature path — signed by the BUYER off-chain,
  submitted by the facilitator atomically during settlement;
* honest fail-closed verification: a forged or tampered permit fails closed
  (``verify_permit_signature`` returns ``(False, reason, "")``); the crypto
  backend being unavailable fails closed too (never ``verified:true`` without
  a real recover).

[REVIEW] This proves the permit is authentically signed by ``owner`` for THAT
token/chain (EIP-712 ``\\x19\\x01`` digest bound to name/version/chainId/
verifyingContract). It does NOT check the on-chain ``nonces(owner)`` replay
counter, the token's actual deployed ``name``/``version`` domain values, or the
current block deadline — those are RPC reads the scaffold cannot perform and
remain a facilitator duty before broadcast.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Callable

log = logging.getLogger(__name__)

EIP2612_KEY = "eip2612GasSponsoring"
ERC20_APPROVAL_KEY = "erc20ApprovalGasSponsoring"
MAX_UINT256 = str(2**256 - 1)  # ERC-20 approval to Permit2

# Canonical UniversalPermit2 contract (same address on every EVM chain) — the
# ``spender`` of the EIP-2612 permit and the target of the ERC-20 approval.
PERMIT2_ADDRESS = "0x000000000022D473030F116dDEE9F6B43aC78BA3"

# EIP-712 domain + Permit typehashes (EIP-2612). Mirrors self_facilitator.py's
# EIP-3009 approach: the domain structure is the SAME; only the struct differs.
_EIP712_DOMAIN_STRING = (
    "EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)"
)
_PERMIT_STRING = (
    "Permit(address owner,address spender,uint256 value,uint256 nonce,uint256 deadline)"
)
_PERMIT_SELECTOR = "0xd505accf"  # permit(address,address,uint256,uint256,uint8,bytes32,bytes32)

# Optional ecrecover-aware crypto layer (eth_keys/eth_utils are pyproject deps).
# Declared Optional so pyright/mypy know a failed import yields None; every
# consumer guards (fail-closed) before calling.
_eth_keys: Any = None
to_canonical_address: Callable[[str], bytes] | None = None
to_checksum_address: Callable[[str], str] | None = None
keccak: Callable[..., bytes] | None = None
_ECRECOVER_AVAILABLE = False

try:  # pragma: no cover - guarded optional import (mirrors domain service.py)
    from eth_keys import keys as _eth_keys  # type: ignore[import-untyped]
    from eth_utils.address import (  # type: ignore[import-untyped]
        to_canonical_address,
        to_checksum_address,
    )
    from eth_utils.crypto import keccak  # type: ignore[import-untyped]

    _ECRECOVER_AVAILABLE = True
except ImportError:  # pragma: no cover - defensive
    _eth_keys = None  # type: ignore[assignment]
    to_canonical_address = None  # type: ignore[assignment]
    to_checksum_address = None  # type: ignore[assignment]
    keccak = None  # type: ignore[assignment]
    _ECRECOVER_AVAILABLE = False


def _eip2612_available() -> bool:
    """True when the crypto backend (eth_keys/eth_utils) is importable."""
    return _ECRECOVER_AVAILABLE


def _enc_address(addr: str) -> bytes:
    """Left-pad a 20-byte address to 32 bytes (right-aligned EVM address slot)."""
    to_canonical = to_canonical_address
    if to_canonical is None:
        raise RuntimeError("eip2612 crypto unavailable: eth_utils not installed")
    return b"\x00" * 12 + to_canonical(addr)


def _enc_uint(v: int) -> bytes:
    """Encode a uint256 as a big-endian 32-byte word."""
    return int(v).to_bytes(32, "big")


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


def _eip2612_domain_separator(
    *, name: str, version: str, chain_id: int, token_address: str
) -> bytes:
    """EIP-712 domain separator for an EIP-2612 token (verifyingContract = token)."""
    keccak_fn = keccak
    if keccak_fn is None:
        raise RuntimeError("eip2612 crypto unavailable: eth_utils keccak not installed")
    return keccak_fn(
        keccak_fn(text=_EIP712_DOMAIN_STRING)
        + keccak_fn(text=name)
        + keccak_fn(text=version)
        + _enc_uint(chain_id)
        + _enc_address(token_address)
    )


def _permit_struct_hash(
    *, owner: str, spender: str, value: int, nonce: int, deadline: int
) -> bytes:
    """EIP-712 struct hash of ``Permit(address owner,address spender,uint256
    value,uint256 nonce,uint256 deadline)`` (EIP-2612)."""
    keccak_fn = keccak
    if keccak_fn is None:
        raise RuntimeError("eip2612 crypto unavailable: eth_utils keccak not installed")
    return keccak_fn(
        keccak_fn(text=_PERMIT_STRING)
        + _enc_address(owner)
        + _enc_address(spender)
        + _enc_uint(value)
        + _enc_uint(nonce)
        + _enc_uint(deadline)
    )


def eip2612_digest(
    *,
    owner: str,
    spender: str,
    value: int,
    nonce: int,
    deadline: int,
    chain_id: int,
    token_address: str,
    name: str = "USD Coin",
    version: str = "1",
) -> bytes:
    """Compute the EIP-712 signed digest for an EIP-2612 permit.

    ``keccak("\\x19\\x01" || domainSeparator || structHash)`` per EIP-712 — the
    bytes the payer actually signed.
    """
    keccak_fn = keccak
    if keccak_fn is None:
        raise RuntimeError("eip2612 crypto unavailable: eth_utils keccak not installed")
    domain = _eip2612_domain_separator(
        name=name, version=version, chain_id=chain_id, token_address=token_address
    )
    struct = _permit_struct_hash(
        owner=owner, spender=spender, value=value, nonce=nonce, deadline=deadline
    )
    return keccak_fn(b"\x19\x01" + domain + struct)


def _parse_signature(signature: Any) -> tuple[int, bytes, bytes] | None:
    """Parse a 65-byte (r||s||v) EIP-2612 signature -> (v, r, s). v in {27,28}."""
    if isinstance(signature, (bytes, bytearray)):
        raw = bytes(signature)
    elif isinstance(signature, str):
        try:
            raw = bytes.fromhex(signature.removeprefix("0x"))
        except ValueError:
            return None
    else:
        return None
    if len(raw) != 65:
        return None
    return int(raw[64]), raw[:32], raw[32:64]


def _parse_permit_fields(payload: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
    """Parse + validate the EIP-2612 permit scalar fields.

    Returns ``(fields, "")`` on success or ``(None, reason)`` fail-closed.
    Reason strings are the canonical ones callers already surface; malformed
    addresses / unparseable values never raise out of here.
    """
    to_cs = to_checksum_address
    if to_cs is None:
        return None, "eip2612 crypto unavailable: eth_keys not installed"
    try:
        owner = to_cs(str(payload.get("owner") or ""))
    except (ValueError, TypeError):
        return None, "missing/invalid permit 'owner'"
    try:
        spender = to_cs(str(payload.get("spender") or PERMIT2_ADDRESS))
    except (ValueError, TypeError):
        return None, "missing/invalid permit 'spender'"
    try:
        token = to_cs(str(payload.get("token") or ""))
    except (ValueError, TypeError):
        return None, "missing/invalid permit 'token' (verifyingContract)"
    try:
        value = _to_int(
            payload.get("amount")
            if payload.get("amount") is not None
            else payload.get("value") or 0
        )
        nonce = _to_int(payload.get("nonce") or 0)
        deadline = _to_int(payload.get("deadline") or 0)
    except ValueError:
        return None, "unparseable permit value/nonce/deadline"
    return (
        {
            "owner": owner,
            "spender": spender,
            "token": token,
            "value": value,
            "nonce": nonce,
            "deadline": deadline,
        },
        "",
    )


def _resolve_domain(name: str, version: str, payload: dict[str, Any]) -> tuple[str, str]:
    """Resolve the EIP-712 token name/version (payload -> env -> default)."""
    resolved_name = (
        name or str(payload.get("tokenName") or "") or os.getenv("X402_EIP712_NAME", "USD Coin")
    ).strip()
    resolved_version = (
        version or str(payload.get("tokenVersion") or "") or os.getenv("X402_EIP712_VERSION", "1")
    ).strip()
    return resolved_name, resolved_version


def _recover_owner(*, digest: bytes, v_norm: int, r: bytes, s: bytes) -> tuple[str | None, str]:
    """ecrecover the permit signer from the EIP-712 digest.

    Returns ``(recovered_owner_or_None, error_reason_or_empty)``. Mirrors the
    canonical ``x402/self_facilitator.py`` ecrecover pattern via ``eth_keys``.
    Fail-closed: any recover error yields ``(None, reason)`` — never a guess.
    """
    eth_keys = _eth_keys
    to_cs = to_checksum_address
    if eth_keys is None or to_cs is None:
        return None, "eip2612 crypto unavailable: eth_keys not installed"
    try:
        signature_obj = eth_keys.Signature(
            vrs=(v_norm, int.from_bytes(r, "big"), int.from_bytes(s, "big"))
        )
        recovered_addr = to_cs(
            eth_keys.PublicKey.recover_from_msg_hash(digest, signature_obj).to_address()
        )
        return recovered_addr, ""
    except Exception as e:  # noqa: BLE001 - fail closed on any recover error
        return None, f"cannot recover permit signer: {e}"


def verify_permit_signature(
    payload: dict[str, Any], *, chain_id: int, name: str = "", version: str = ""
) -> tuple[bool, str, str]:
    """Verify an EIP-2612 permit signature, mirroring self_facilitator ecrecover.

    Returns ``(ok, reason, recovered_owner)``. The permit is verified when the
    recovered signer matches the payload ``owner`` AND the domain binds
    ``chain_id`` + ``token`` (verifyingContract). Fails closed when the crypto
    backend is unavailable, fields are malformed, or the signer mismatches.

    [REVIEW] This proves the permit is authentically signed by ``owner`` for
    THAT token/chain. It does NOT check the on-chain ``nonces(owner)`` counter,
    the token's actual ``name``/``version`` domain values, or the current block
    deadline — those are RPC reads the scaffold cannot perform and remain a
    facilitator duty before broadcast.
    """
    if not _ECRECOVER_AVAILABLE or _eth_keys is None:
        return False, "eip2612 crypto unavailable: eth_keys not installed", ""
    fields, fields_reason = _parse_permit_fields(payload)
    if fields is None:
        return False, fields_reason, ""

    # EIP-2612 domain: the ERC-20 token's name + version (EIP-712 "version" is
    # the EIP-2612 version string, typically "1"; the x402 payload may carry it
    # explicitly via tokenName/tokenVersion, else fall back to env / default).
    name, version = _resolve_domain(name, version, payload)

    sig = _parse_signature(payload.get("signature"))
    if sig is None:
        return False, "malformed EIP-2612 signature (expected 65-byte r||s||v)", ""
    v, r, s = sig
    v_norm = v if v in (0, 1) else (v - 27 if v in (27, 28) else -1)
    if v_norm not in (0, 1):
        return False, "invalid EIP-2612 signature recovery id (v)", ""

    digest = eip2612_digest(
        owner=fields["owner"],
        spender=fields["spender"],
        value=fields["value"],
        nonce=fields["nonce"],
        deadline=fields["deadline"],
        chain_id=chain_id,
        token_address=fields["token"],
        name=name,
        version=version,
    )
    recovered_addr, recover_reason = _recover_owner(digest=digest, v_norm=v_norm, r=r, s=s)
    if recovered_addr is None:
        return False, recover_reason, ""
    if recovered_addr != fields["owner"]:
        return (
            False,
            (
                f"permit signer {recovered_addr} != owner {fields['owner']}"
                f" (forged or tampered permit — fields changed the digest)"
            ),
            "",
        )
    return True, "", recovered_addr


def build_permit_calldata(
    payload: dict[str, Any], *, chain_id: int, name: str = "", version: str = ""
) -> str:
    """Build the real ERC-20 ``permit(owner,spender,value,deadline,v,r,s)`` calldata.

    Dense ABI encoding of the permit so a sponsor key can broadcast it
    (funding gas + atomic ``settleWithPermit``). Returns ``0x``-prefixed hex,
    never a fabricated tx hash. Raises if the payload is not a valid,
    locally-verified permit.
    """
    ok, reason, _owner = verify_permit_signature(
        payload, chain_id=chain_id, name=name, version=version
    )
    if not ok:
        raise ValueError(f"refusing to build permit calldata: {reason}")
    to_cs = to_checksum_address
    if to_canonical_address is None or to_cs is None:
        raise RuntimeError("eip2612 crypto unavailable: eth_utils not installed")
    owner = to_cs(str(payload["owner"]))
    spender = to_cs(str(payload.get("spender") or PERMIT2_ADDRESS))
    value = _to_int(
        payload.get("amount") if payload.get("amount") is not None else payload.get("value") or 0
    )
    deadline = _to_int(payload.get("deadline") or 0)
    parsed = _parse_signature(payload.get("signature"))
    if parsed is None:
        raise ValueError("malformed EIP-2612 signature for permit calldata")
    v, r, s = parsed
    # ABI: (address,address,uint256,uint256,uint8,bytes32,bytes32)
    # NOTE: the EIP-2612 permit nonce is NOT a calldata field — it is read
    # on-chain from token.nonces(owner) at permit() execution time.
    calldata = (
        bytes.fromhex(_PERMIT_SELECTOR[2:])
        + _enc_address(owner)
        + _enc_address(spender)
        + _enc_uint(value)
        + _enc_uint(deadline)
        + _enc_uint(v)
        + r.rjust(32, b"\x00")
        + s.rjust(32, b"\x00")
    )
    return "0x" + calldata.hex()


def _validate_permit2_approval_payload(info: dict[str, Any], key: str) -> list[str]:
    """Shared payload-shape validation for the two gas-sponsoring paths."""
    errors: list[str] = []
    if not isinstance(info, dict):
        return ["extension info must be an object"]
    if key == EIP2612_KEY:
        # Presence check tolerant of falsy-but-valid values: nonce/deadline/amount
        # may legitimately be 0 (a fresh owner's EIP-2612 nonce IS 0). We require
        # the key to be present and non-empty, not merely truthy.
        for f in ("token", "owner", "spender", "amount", "nonce", "deadline", "signature"):
            if f not in info or info.get(f) is None or str(info.get(f)).strip() == "":
                errors.append(f"EIP-2612 permit missing '{f}'")
        sig: Any = info.get("signature")
        if isinstance(sig, str) and not sig.startswith("0x"):
            errors.append("EIP-2612 signature must be 0x-prefixed")
    else:  # ERC-20 approval
        if not info.get("token"):
            errors.append("signed approval missing 'token'")
        if not info.get("signedTransaction"):
            errors.append("ERC-20 approval missing 'signedTransaction'")
        # The signed raw approve tx must target the canonical Permit2 (spec: approve(Permit2)).
        tx: Any = info.get("signedTransaction")
        if isinstance(tx, dict) and tx.get("to") and "permit2" not in str(tx.get("to", "")).lower():
            errors.append("approve() 'to' must be the canonical Permit2 contract")
    return errors


__all__ = [
    "EIP2612_KEY",
    "ERC20_APPROVAL_KEY",
    "MAX_UINT256",
    "PERMIT2_ADDRESS",
    "_eip2612_available",
    "eip2612_digest",
    "build_permit_calldata",
    "verify_permit_signature",
    "_validate_permit2_approval_payload",
]
