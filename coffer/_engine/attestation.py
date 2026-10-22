"""Coffer — server attestation over the ``PAYMENT-REQUIRED`` challenge.

Parity reference (Pry): ``pryscraper/src/pry/payments/x402/scheme/digest.py``
L38-75 and ``scheme/attestation.py``. Pry signs the canonical (payTo, amount,
network, nonce, issued_at, operation) tuple of every accept with the server's
secp256k1 key and embeds a recoverable signature, so a MITM/proxy/shared cache
cannot rewrite ``payTo`` or the amount and have the payer sign it.

Coffer gap this closes: ``coffer/_engine/response_handler.py`` builds the 402
body unsigned — a rewritten ``payTo`` in transit is indistinguishable from
ours. This module adds the missing half: a domain-separated digest, an
ECDSA signature bound to a freshness window, and a verify path that fails
closed on tamper/expiry.

Scheme: ``keccak256-ecdsa-secp256k1-v1`` over
``keccak256(domain_utf8 || 0x00 || json.dumps(canonical_list))`` where the
canonical list is one fixed-order tuple per accept. Key precedence is env-only
(never hardcoded): ``X402_ATTEST_KEY`` -> ``X402_FACILITATOR_KEY`` ->
``X402_ATTEST_SECRET`` (sha256-derived). No key => the body is emitted
**unattested** and the verifier reports ``attested=False``; callers decide.

[REVIEW] Byte-level compatibility with Pry's canonical list was not confirmed
against a Pry-produced signature; the *behaviour* matches. Crossover between the
two implementations requires aligning the canonical tuple — tracked separately.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

ATTEST_DOMAIN = "coffer-x402/attestation/v1"
ATTEST_ALGORITHM = "keccak256-ecdsa-secp256k1-v1"
ATTEST_VERSION = 1
ATTEST_KEY_ENV = "X402_ATTEST_KEY"
ATTEST_FACILITATOR_KEY_ENV = "X402_FACILITATOR_KEY"
ATTEST_SECRET_ENV = "X402_ATTEST_SECRET"
ATTEST_MAX_SKEW_SECONDS = int(os.getenv("X402_ATTEST_MAX_SKEW", "120"))
ATTEST_MAX_AGE_SECONDS = int(os.getenv("X402_ATTEST_MAX_AGE", "600"))

# Canonical tuple order per accept. Append-only: changing it breaks signatures.
_CANONICAL_FIELDS = (
    "payTo",
    "maxAmountRequired",
    "network",
    "asset",
    "resource",
    "operation",
    "nonce",
    "issued_at",
)

try:  # optional crypto dependency; attestation is disabled without it
    from eth_keys import keys as _eth_keys  # type: ignore[import-untyped]
    from eth_utils import keccak as _keccak  # type: ignore[import-untyped]

    CRYPTO_AVAILABLE = True
except ImportError:  # pragma: no cover - depends on runtime deps
    _eth_keys = None  # type: ignore[assignment]
    _keccak = None  # type: ignore[assignment]
    CRYPTO_AVAILABLE = False


@dataclass(frozen=True)
class AttestationResult:
    """Outcome of verifying a challenge body's embedded attestation."""

    attested: bool
    ok: bool
    reason: str = ""
    signer: str = ""
    alg: str = ""


def canonical_payload(accepts: list[Mapping[str, Any]]) -> list[list[str]]:
    """Fixed-order, string-only projection of every accept (no key-order ambiguity)."""
    payload: list[list[str]] = []
    for accept in accepts:
        payload.append([str(accept.get(field, "") or "") for field in _CANONICAL_FIELDS])
    return payload


def attestation_digest(payload: list[list[str]], *, domain: str = ATTEST_DOMAIN) -> bytes:
    """Domain-separated keccak256 digest of :func:`canonical_payload` output."""
    if _keccak is None:
        raise RuntimeError("attestation crypto unavailable (eth-utils not installed)")
    body = json.dumps(payload, separators=(",", ":"), sort_keys=False).encode()
    return bytes(_keccak(domain.encode() + b"\x00" + body))


def _load_key() -> Any | None:
    """Resolve the signing key from env (explicit key > facilitator key > secret)."""
    if not CRYPTO_AVAILABLE:
        return None
    for env_name in (ATTEST_KEY_ENV, ATTEST_FACILITATOR_KEY_ENV):
        raw = os.getenv(env_name, "").strip()
        if raw:
            try:
                return _eth_keys.PrivateKey(bytes.fromhex(raw.removeprefix("0x")))
            except (ValueError, TypeError):
                continue
    secret = os.getenv(ATTEST_SECRET_ENV, "").strip()
    if secret:
        return _eth_keys.PrivateKey(hashlib.sha256(secret.encode()).digest())
    return None


def attest_payment_required(
    body: dict[str, Any],
    *,
    private_key: Any | None = None,
    now: float | None = None,
    nonce: str = "",
) -> dict[str, Any]:
    """Attach ``body["attestation"]`` when a key exists; return the body unchanged otherwise.

    The body's ``accepts`` entries are copied with ``issued_at`` stamped so the
    canonical tuple and the wire body agree exactly.
    """
    key = private_key if private_key is not None else _load_key()
    if key is None:
        return body
    issued_at = int(now if now is not None else time.time())
    accepts = body.get("accepts")
    if not isinstance(accepts, list) or not accepts:
        raise ValueError("payment-required body has no accepts to attest")
    stamped: list[dict[str, Any]] = []
    for accept in accepts:
        if not isinstance(accept, dict):
            raise TypeError("accept must be an object")
        entry = dict(accept)
        entry.setdefault("issued_at", issued_at)
        entry.setdefault("nonce", nonce or _fresh_nonce(issued_at))
        stamped.append(entry)
    body["accepts"] = stamped
    digest = attestation_digest(canonical_payload(stamped))
    signature = key.sign_msg_hash(digest)
    body["attestation"] = {
        "version": ATTEST_VERSION,
        "alg": ATTEST_ALGORITHM,
        "domain": ATTEST_DOMAIN,
        "digest": "0x" + digest.hex(),
        "issued_at": issued_at,
        "expires_at": issued_at + ATTEST_MAX_AGE_SECONDS,
        "signer": key.public_key.to_checksum_address(),
        "signature": "0x" + signature.to_bytes().hex(),
    }
    return body


def verify_payment_required(
    body: Mapping[str, Any],
    *,
    now: float | None = None,
    expected_signer: str = "",
    max_age: int | None = None,
    max_skew: int | None = None,
) -> AttestationResult:
    """Verify the embedded attestation. Any ambiguity => ``ok=False``."""
    if not CRYPTO_AVAILABLE:
        return AttestationResult(attested=False, ok=False, reason="crypto_unavailable")
    attestation = body.get("attestation")
    if not isinstance(attestation, Mapping):
        return AttestationResult(attested=False, ok=False, reason="unattested")
    alg = str(attestation.get("alg", ""))
    if alg != ATTEST_ALGORITHM:
        return AttestationResult(attested=True, ok=False, reason="unsupported_alg", alg=alg)
    accepts = body.get("accepts")
    if not isinstance(accepts, list) or not accepts:
        return AttestationResult(attested=True, ok=False, reason="no_accepts", alg=alg)
    stamp = now if now is not None else time.time()
    issued_at = int(attestation.get("issued_at", 0) or 0)
    age = stamp - issued_at
    if issued_at <= 0 or age > (max_age if max_age is not None else ATTEST_MAX_AGE_SECONDS):
        return AttestationResult(attested=True, ok=False, reason="expired", alg=alg)
    if age < -(max_skew if max_skew is not None else ATTEST_MAX_SKEW_SECONDS):
        return AttestationResult(attested=True, ok=False, reason="from_future", alg=alg)
    try:
        digest = attestation_digest(canonical_payload(accepts))
        signature = _eth_keys.Signature(
            bytes.fromhex(str(attestation.get("signature", "")).removeprefix("0x"))
        )
        signer = signature.recover_public_key_from_msg_hash(digest).to_checksum_address()
    except (ValueError, TypeError, RuntimeError):
        return AttestationResult(attested=True, ok=False, reason="malformed", alg=alg)
    if "0x" + digest.hex() != str(attestation.get("digest", "")).lower():
        return AttestationResult(attested=True, ok=False, reason="digest_mismatch", alg=alg, signer=signer)
    if expected_signer and signer.lower() != expected_signer.lower():
        return AttestationResult(attested=True, ok=False, reason="wrong_signer", alg=alg, signer=signer)
    return AttestationResult(attested=True, ok=True, alg=alg, signer=signer)


def _fresh_nonce(issued_at: int) -> str:
    """Deterministic-per-second nonce stem (caller may override)."""
    return hashlib.sha256(f"{ATTEST_DOMAIN}:{issued_at}".encode()).hexdigest()[:16]
