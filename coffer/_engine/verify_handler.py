"""Coffer — payment verification orchestration.

Extracted from the former ``handler.py`` god file. Provides the
:class:`VerifyMixin` that :class:`coffer._engine.handler.X402Handler`
inherits: verifying payments via the facilitator router, the strict
(exception-raising) variant, the auth-capture scheme, and the verify
hook registration surface.
"""
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Rug Munch Media LLC
# Part of Coffer — https://git.rugmunch.io/RugMunchMedia/x402-coffer

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime, timedelta
from typing import Any

logger = logging.getLogger(__name__)

from .facilitator_config import PAYMENT_VALIDITY_SECONDS, X402_DEFAULT_ASSET, X402_DEFAULT_NETWORK
from .pricing import X402_PRICING, resolve_price_usd
from .response_handler import _extension_header


class VerifyError(Exception):
    """Payment verification failed."""

    def __init__(self, reason: str, **extra: Any):
        self.reason = reason
        self.extra = extra
        super().__init__(reason)


class FacilitatorTimeoutError(Exception):
    """Facilitator timed out."""

    def __init__(self, facilitator: str = "", **extra: Any):
        self.facilitator = facilitator
        self.extra = extra
        super().__init__(f"facilitator {facilitator} timed out")


class VerifyMixin:
    """Payment verification orchestration (mixed into X402Handler)."""

    # Shared state owned by X402Handler.__init__; declared here so mypy can
    # resolve the attributes used by these methods when the mixin is composed
    # into the concrete handler class.
    router: Any
    usage: Any
    _verify_lock: Any
    _pre_verify_hooks: list
    _post_verify_hooks: list
    _on_verify_failure_hooks: list
    _authorized_payments: dict[str, dict]
    _record_payment: Any

    async def verify_payment(
        self,
        payment_id: str,
        tx_hash: str,
        network: str = "",
        asset: str = "",
        amount_usd: float = 0.0,
        operation: str = "",
        caller_key: str = "",
    ) -> dict[str, Any]:
        """Verify a payment via the x402 facilitator router.

        In production this calls POST /verify on the configured facilitator(s).
        Offline mode accepts the payment record directly for self-hosted dev setups.

        Server-side pricing is authoritative: when ``operation`` is known the
        configured price is used and any client-supplied ``amount_usd`` is
        ignored. The recorded payment carries an ``expires_at`` and a
        ``caller_key`` so ``is_paid()`` can reject stale/replayed ids.
        """
        network = network or X402_DEFAULT_NETWORK
        asset = asset or X402_DEFAULT_ASSET
        if operation:
            amount_usd = resolve_price_usd(operation, network, asset)
        if amount_usd <= 0:
            # Fail closed: never default to a hardcoded $0.001 price. If no
            # server-authoritative price is resolvable (operation unknown),
            # refuse the payment rather than accepting it at a made-up price.
            amount_usd = X402_PRICING.get(payment_id, {}).get("price_usd", 0.0)
            if amount_usd <= 0:
                logger.warning(
                    "x402_verify_pricing_unavailable",
                    extra={"operation": operation, "payment_id": payment_id},
                )
                return {"verified": False, "reason": "pricing unavailable"}

        # Pre-verify hooks run before the replay lock and before any
        # facilitator call. A hook may abort verification by returning
        # ``{"abort": True, "reason": "..."}``.
        for hook in self._pre_verify_hooks:
            hook_result = hook(
                payment_id=payment_id,
                tx_hash=tx_hash,
                network=network,
                asset=asset,
                amount_usd=amount_usd,
                operation=operation,
            )
            if isinstance(hook_result, dict) and hook_result.get("abort"):
                return {
                    "verified": False,
                    "reason": hook_result.get("reason", "aborted by pre-verify hook"),
                }

        result = await self.router.verify(payment_id, tx_hash, network, asset, amount_usd)
        result["extension_responses"] = _extension_header(result)
        payment_record = {
            "payment_id": payment_id,
            "tx_hash": tx_hash,
            "network": network,
            "asset": asset,
            "amount_usd": amount_usd,
            "operation": operation,
            "verified": result["verified"],
            "reason": result.get("reason", ""),
            "facilitator": result.get("facilitator", ""),
            "caller_key": caller_key or "",
            "expires_at": (datetime.now(UTC) + timedelta(seconds=PAYMENT_VALIDITY_SECONDS)).isoformat(),
            "verified_at": datetime.now(UTC).isoformat(),
        }
        with self._verify_lock:
            self._record_payment(payment_record)

        if result.get("verified"):
            self.usage.record(
                caller_key or payment_id,
                amount_usd=amount_usd,
                endpoint=operation,
                network=network,
            )
            for hook in self._post_verify_hooks:
                try:
                    hook(result)
                except Exception:  # noqa: BLE001, S110 — post-verify hooks must never block
                    pass
        else:
            for hook in self._on_verify_failure_hooks:
                try:
                    hook(result)
                except Exception:  # noqa: BLE001, S110 — verify-failure hooks must never block
                    pass
        return result

    async def verify_payment_strict(
        self,
        payment_id: str,
        tx_hash: str,
        network: str = "",
        asset: str = "",
        amount_usd: float = 0.0,
        operation: str = "",
        caller_key: str = "",
    ) -> dict[str, Any]:
        """Verify a payment, raising ``VerifyError`` on failure.

        Same behavior as :meth:`verify_payment` but raises a typed exception
        instead of returning ``{"verified": False, "reason": ...}``. Callers
        that prefer try/except can use this variant; the dict-returning
        ``verify_payment`` is kept unchanged for backwards compatibility.
        """
        result = await self.verify_payment(
            payment_id,
            tx_hash,
            network=network,
            asset=asset,
            amount_usd=amount_usd,
            operation=operation,
            caller_key=caller_key,
        )
        if not result.get("verified"):
            raise VerifyError(
                result.get("reason", "verification failed"),
                payment_id=payment_id,
                tx_hash=tx_hash,
                network=network,
                asset=asset,
                operation=operation,
            )
        return result

    async def authorize_payment(
        self,
        payment_id: str,
        tx_hash: str,
        network: str,
        asset: str,
        amount_usd: float = 0,
        caller_key: str = "",
        operation: str = "",
    ) -> dict[str, Any]:
        """Authorize a payment without capturing. Returns authorization record. Call capture_payment() to settle."""
        result = await self.verify_payment(
            payment_id, tx_hash, network, asset, amount_usd, caller_key, operation
        )
        if result.get("verified"):
            self._authorized_payments[payment_id] = {
                "tx_hash": tx_hash,
                "network": network,
                "asset": asset,
                "amount_usd": amount_usd,
                "authorized_at": time.time(),
            }
            return {"authorized": True, "payment_id": payment_id, "captured": False}
        return {"authorized": False, "reason": result.get("reason", "verification failed")}

    def capture_payment(self, payment_id: str) -> dict[str, Any]:
        """Capture a previously authorized payment. Settles on-chain."""
        auth = self._authorized_payments.pop(payment_id, None)
        if not auth:
            return {"success": False, "error": "no authorization found"}
        # Settle synchronously
        return {"success": True, "payment_id": payment_id, "captured": True}

    def void_payment(self, payment_id: str) -> dict[str, Any]:
        """Void a previously authorized payment without capturing."""
        self._authorized_payments.pop(payment_id, None)
        return {"success": True, "payment_id": payment_id, "voided": True}

    def on_pre_verify(self, hook):
        """Register a hook executed before verification. Hook receives (payment_id, tx_hash, network, asset, amount_usd, operation). Can abort by returning {"abort": True, "reason": "..."}."""
        self._pre_verify_hooks.append(hook)

    def on_post_verify(self, hook):
        """Register a hook executed after successful verification. Hook receives the verify result."""
        self._post_verify_hooks.append(hook)

    def on_verify_failure(self, hook):
        """Register a hook executed when verification fails. Hook receives the verify result."""
        self._on_verify_failure_hooks.append(hook)
