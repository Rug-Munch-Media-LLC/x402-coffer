"""Local x402 facilitator double for Coffer's parity tests.

Real cryptography, zero chain I/O: it recomputes the EIP-3009
``TransferWithAuthorization`` digest with the *same* code the production
``SelfFacilitator`` uses (``_digest``), recovers the payer with ``eth_keys``,
enforces amount / payTo / validity window, and burns the authorization nonce so
a replay is rejected exactly as a real facilitator would.

Registered through Coffer's real seam
(``FacilitatorRouter.register_facilitator``), so every line of orchestration,
pricing, replay and persistence under test is production code. No transaction
is broadcast anywhere: ``settle`` returns a deterministic pseudo-tx hash and is
explicitly labelled ``simulated``.
"""

from __future__ import annotations

import time
from typing import Any

USDC_BASE_SEPOLIA = "0x036CbD53842c5426634e7929541eC2318f3dCF7e"
BASE_SEPOLIA_CHAIN_ID = 84532


class FakeFacilitator:
    """Deterministic, offline facilitator with nonce-burn replay defence.

    Speaks the same contract as a self-hosted facilitator implementation:
    ``verify(payment)`` / ``settle(payment)``, where ``payment`` carries the
    signed EIP-3009 ``authorization`` (Coffer attaches it from
    ``router.submit_authorization``). A real facilitator receives it over HTTP.
    """

    def __init__(self, *, pay_to: str, required_atomic: int, token: str = USDC_BASE_SEPOLIA) -> None:
        self.pay_to = pay_to
        self.required_atomic = int(required_atomic)
        self.token = token
        self.chain_id = BASE_SEPOLIA_CHAIN_ID
        self.burned: set[str] = set()
        self.refusals: list[dict[str, Any]] = []
        self.settlements: list[dict[str, Any]] = []

    def _recover(self, auth: dict[str, Any]) -> tuple[str, str]:
        """Return (signer, reason). Empty signer means invalid."""
        from coffer._engine.self_facilitator import SelfFacilitator

        facilitator = SelfFacilitator(pay_to=self.pay_to)
        try:
            digest = facilitator._digest(auth, self.chain_id, self.token)
        except (KeyError, TypeError, ValueError):
            return "", "malformed_authorization"
        try:
            from eth_keys import keys as eth_keys

            raw = bytes.fromhex(str(auth["signature"]).removeprefix("0x"))
            if len(raw) == 65 and raw[64] >= 27:  # eth_account returns legacy v (27/28)
                raw = raw[:64] + bytes([raw[64] - 27])
            signature = eth_keys.Signature(raw)
            return signature.recover_public_key_from_msg_hash(digest).to_checksum_address(), ""
        except (ImportError, KeyError, TypeError, ValueError):
            return "", "invalid_signature"

    def _check(self, auth: dict[str, Any]) -> tuple[bool, str, dict[str, Any]]:
        value = int(auth.get("value", 0) or 0)
        if value < self.required_atomic:
            return False, "insufficient_amount", {"required_atomic": self.required_atomic}
        if str(auth.get("to", "")).lower() != self.pay_to.lower():
            return False, "wrong_pay_to", {}
        signer, reason = self._recover(auth)
        if not signer:
            return False, reason, {}
        if signer.lower() != str(auth.get("from", "")).lower():
            return False, "signature_mismatch", {}
        now = int(time.time())
        if int(auth.get("valid_before", auth.get("validBefore", 0)) or 0) <= now:
            return False, "authorization_expired", {}
        if int(auth.get("valid_after", auth.get("validAfter", 0)) or 0) > now:
            return False, "authorization_not_yet_valid", {}
        nonce = str(auth.get("nonce", ""))
        if nonce in self.burned:
            return False, "authorization_already_used", {}
        return True, "", {"payer": signer, "amount_atomic": value}

    async def verify(self, payment: dict[str, Any]) -> Any:
        """Return a ``VerifyResult`` — the type the orchestrator reads."""
        from coffer._engine.models_settlement import VerifyResult

        ok, reason, extra = self._check(payment.get("authorization") or {})
        return VerifyResult(
            verified=ok,
            reason="" if ok else reason,
            signer=extra.get("payer"),
            amount_atomic=extra.get("amount_atomic"),
            facilitator="local-fake",
            settlement=None,
        )

    async def settle(self, payment: dict[str, Any]) -> str | None:
        """Return a pseudo tx hash on success, ``None`` on any refusal."""
        auth = payment.get("authorization") or {}
        ok, reason, extra = self._check(auth)
        if not ok:
            self.refusals.append({"reason": reason, "payment_id": payment.get("payment_id", "")})
            return None
        self.burned.add(str(auth.get("nonce", "")))
        from eth_utils import keccak

        tx_hash = (
            "0x"
            + keccak(
                bytes.fromhex(str(auth["signature"]).removeprefix("0x")) + str(auth.get("nonce", "")).encode()
            ).hex()
        )
        self.settlements.append(
            {
                "txHash": tx_hash,
                "network": payment.get("network", ""),
                "payer": extra["payer"],
                "amount_atomic": extra["amount_atomic"],
                "simulated": True,  # local fake — no chain broadcast
            }
        )
        return tx_hash
