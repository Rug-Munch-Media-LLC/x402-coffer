"""Sign-In-With-X (SIWx) extension — CAIP-122 wallet authentication.

Mirrors the official ``@x402/extensions/sign-in-with-x`` semantics: a client
proves control of a wallet (CAIP-122 signed message) and the server grants a
bounded session — enabling access without repurchase.

Honest scope: EVM (EIP-191 personal_sign recovery) is LIVE. Non-EVM chains
fail closed with ``needs_verifier:<family>`` until their verifiers exist.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import secrets
import threading
from collections import OrderedDict
from datetime import UTC, datetime, timedelta
from typing import Any

log = logging.getLogger(__name__)

SIWX_EXTENSION_KEY = "sign-in-with-x"
SIWX_HEADER = "SIGN-IN-WITH-X"
DEFAULT_SESSION_TTL_SECONDS = 3600
DEFAULT_CHALLENGE_TTL_SECONDS = 600

_CAIP122_TEMPLATE = (
    "{domain} wants you to sign in with your {chain} account:\n"
    "{address}\n"
    "\n"
    "{statement}\n"
    "\n"
    "URI: {uri}\n"
    "Version: {version}\n"
    "Chain ID: {chain_id}\n"
    "Nonce: {nonce}\n"
    "Issued At: {issued_at}\n"
    "Expiration Time: {expiration_time}"
)


def build_siwx_message(
    *,
    domain: str,
    address: str,
    chain_id: str,
    nonce: str,
    statement: str = "Sign in to Coffer",
    uri: str = "https://coffer.example",
    version: str = "1",
    issued_at: str | None = None,
    expiration_time: str | None = None,
    ttl_seconds: int = DEFAULT_CHALLENGE_TTL_SECONDS,
) -> str:
    """Build a CAIP-122 formatted sign-in message."""
    now = issued_at or datetime.now(UTC).isoformat()
    exp = expiration_time or (datetime.now(UTC) + timedelta(seconds=ttl_seconds)).isoformat()
    return _CAIP122_TEMPLATE.format(
        domain=domain,
        chain="evm",
        address=address,
        statement=statement,
        uri=uri,
        version=version,
        chain_id=chain_id,
        nonce=nonce,
        issued_at=now,
        expiration_time=exp,
    )


def _recover_eip191(message: str, signature_hex: str) -> tuple[str | None, str]:
    """Recover the EIP-191 personal_sign signer. Fail-closed."""
    try:  # pragma: no cover - guarded optional import (mirrors permit.py)
        from eth_account import Account
        from eth_account.messages import encode_defunct
    except Exception as e:  # noqa: BLE001
        return None, f"crypto_unavailable: {e}"
    try:
        sig = signature_hex.strip()
        if not sig.startswith("0x"):
            sig = f"0x{sig}"
        recovered = Account.recover_message(encode_defunct(text=message), signature=sig)
        return recovered.lower(), ""
    except Exception as e:  # noqa: BLE001 — never raise on bad input
        return None, f"invalid_signature: {e}"


class SIWXChallengeStore:
    """Bounded store of outstanding challenges (nonce -> metadata)."""

    def __init__(
        self, ttl_seconds: int = DEFAULT_CHALLENGE_TTL_SECONDS, max_entries: int = 10_000
    ) -> None:
        self.ttl = int(ttl_seconds)
        self._max = int(max_entries)
        self._lock = threading.Lock()
        self._challenges: OrderedDict[str, dict[str, Any]] = OrderedDict()

    def create(
        self,
        *,
        domain: str,
        chain_id: str,
        address: str | None = None,
        statement: str = "Sign in to Coffer",
        uri: str = "https://coffer.example",
    ) -> dict[str, Any]:
        nonce = secrets.token_hex(16)
        now = datetime.now(UTC)
        msg = build_siwx_message(
            domain=domain,
            address=address or "0x…",
            chain_id=chain_id,
            nonce=nonce,
            statement=statement,
            uri=uri,
            issued_at=now.isoformat(),
            expiration_time=(now + timedelta(seconds=self.ttl)).isoformat(),
            ttl_seconds=self.ttl,
        )
        entry = {
            "nonce": nonce,
            "domain": domain,
            "chain_id": chain_id,
            "message": msg,
            "issued_at": now.timestamp(),
            "used": False,
        }
        with self._lock:
            self._challenges[nonce] = entry
            while len(self._challenges) > self._max:
                self._challenges.popitem(last=False)
        return {"nonce": nonce, "message": msg, "expires_in": self.ttl}

    def consume(self, nonce: str) -> dict[str, Any] | None:
        """Atomically claim a challenge; None when unknown/expired/used."""
        with self._lock:
            entry = self._challenges.pop(nonce, None)
        if entry is None:
            return None
        if datetime.now(UTC).timestamp() - float(entry["issued_at"]) > self.ttl:
            return None
        return entry


class SIWXSessionStore:
    """Bounded, caller-bound session tokens granted after verification."""

    def __init__(
        self, ttl_seconds: int = DEFAULT_SESSION_TTL_SECONDS, max_entries: int = 50_000
    ) -> None:
        self.ttl = int(ttl_seconds)
        self._max = int(max_entries)
        self._lock = threading.Lock()
        self._sessions: OrderedDict[str, dict[str, Any]] = OrderedDict()

    def grant(self, address: str, chain_id: str) -> dict[str, Any]:
        token = secrets.token_urlsafe(32)
        now = datetime.now(UTC).timestamp()
        with self._lock:
            self._sessions[token] = {
                "address": address.lower(),
                "chain_id": chain_id,
                "issued_at": now,
                "expires_at": now + self.ttl,
            }
            while len(self._sessions) > self._max:
                self._sessions.popitem(last=False)
        return {"session_token": token, "address": address.lower(), "expires_in": self.ttl}

    def validate(self, token: str) -> dict[str, Any] | None:
        with self._lock:
            sess = self._sessions.get(token)
        if sess is None or datetime.now(UTC).timestamp() > float(sess["expires_at"]):
            return None
        return sess


def verify_siwx_proof(
    proof_b64: str,
    *,
    challenges: SIWXChallengeStore,
    domain: str,
) -> dict[str, Any]:
    """Verify a base64(JSON{"message","signature"}) SIWx proof. Fail-closed.

    Returns ``{"verified": bool, "reason": str, "address": str|None,
    "session": dict|None}`` — never raises.
    """
    try:
        raw = base64.b64decode(proof_b64, validate=True)
        payload = json.loads(raw)
        message = str(payload.get("message", ""))
        signature = str(payload.get("signature", ""))
    except (binascii.Error, ValueError, TypeError) as e:
        return {
            "verified": False,
            "reason": f"malformed_proof: {e}",
            "address": None,
            "session": None,
        }
    if not message or not signature:
        return {
            "verified": False,
            "reason": "missing message/signature",
            "address": None,
            "session": None,
        }

    # Pull the nonce out of the CAIP-122 message and atomically consume it.
    nonce = ""
    for line in message.splitlines():
        if line.startswith("Nonce:"):
            nonce = line.split(":", 1)[1].strip()
            break
    if not nonce:
        return {
            "verified": False,
            "reason": "no nonce in message",
            "address": None,
            "session": None,
        }
    challenge = challenges.consume(nonce)
    if challenge is None:
        return {
            "verified": False,
            "reason": "unknown/expired/used challenge",
            "address": None,
            "session": None,
        }

    # Domain binding — replay across domains is rejected.
    if f"{domain} wants you to sign in" not in message:
        return {"verified": False, "reason": "domain mismatch", "address": None, "session": None}

    # Expiry binding.
    for line in message.splitlines():
        if line.startswith("Expiration Time:"):
            try:
                if datetime.now(UTC) > datetime.fromisoformat(line.split(":", 1)[1].strip()):
                    return {
                        "verified": False,
                        "reason": "challenge expired",
                        "address": None,
                        "session": None,
                    }
            except ValueError:
                return {
                    "verified": False,
                    "reason": "bad expiration",
                    "address": None,
                    "session": None,
                }
            break

    address, reason = _recover_eip191(message, signature)
    if address is None:
        return {"verified": False, "reason": reason, "address": None, "session": None}

    # The recovered signer must be the account named in the message.
    named = ""
    lines = message.splitlines()
    for i, line in enumerate(lines):
        if "wants you to sign in" in line and i + 1 < len(lines):
            named = lines[i + 1].strip().lower()
            break
    if named and named != address:
        return {
            "verified": False,
            "reason": "signer != account in message",
            "address": None,
            "session": None,
        }

    chain_id = ""
    for line in lines:
        if line.startswith("Chain ID:"):
            chain_id = line.split(":", 1)[1].strip()
            break
    sessions = _get_session_store()
    session = sessions.grant(address, chain_id) if sessions else None
    log.info("x402_siwx_verified", extra={"address": address})
    return {"verified": True, "reason": "", "address": address, "session": session}


_session_store: SIWXSessionStore | None = None


def _get_session_store() -> SIWXSessionStore | None:
    global _session_store
    if _session_store is None:
        _session_store = SIWXSessionStore()
    return _session_store


def get_siwx_stores() -> tuple[SIWXChallengeStore, SIWXSessionStore]:
    """Global (challenge, session) stores."""
    return SIWXChallengeStore(), _get_session_store()  # type: ignore[return-value]


def register_siwx_router(*, domain: str = "coffer.local") -> Any:
    """APIRouter: ``POST /v1/x402/siwx/challenge`` + ``POST /v1/x402/siwx/verify``."""
    from fastapi import APIRouter, Header, HTTPException

    router = APIRouter(prefix="/v1/x402/siwx", tags=["x402-siwx"])
    challenges = SIWXChallengeStore()

    @router.post("/challenge")
    async def challenge(body: dict[str, Any]) -> dict[str, Any]:
        chain_id = str(body.get("chainId") or body.get("chain_id") or "eip155:8453")
        if not chain_id.startswith("eip155:"):
            raise HTTPException(
                status_code=422, detail=f"needs_verifier:{chain_id} (EVM only, honest scope)"
            )
        return challenges.create(
            domain=domain,
            chain_id=chain_id,
            address=body.get("address"),
            statement=str(body.get("statement") or "Sign in to Coffer"),
        )

    @router.post("/verify")
    async def verify(authorization: str = Header(default="")) -> dict[str, Any]:
        proof = authorization.removeprefix("Bearer ").strip() or ""
        result = verify_siwx_proof(proof, challenges=challenges, domain=domain)
        if not result["verified"]:
            raise HTTPException(status_code=401, detail=result["reason"])
        return result

    return router
