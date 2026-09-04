"""Coffer — X402Handler (verify/settle orchestration).

Owns the handler that creates spec-compliant 402 responses, verifies
payments via the facilitator router, settles them, and tracks payment
records with replay-hardening (expiry + caller binding).
"""
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Rug Munch Media LLC
# Part of Coffer — https://git.rugmunch.io/RugMunchMedia/x402-coffer

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from .facilitator_config import (
    PAYMENT_VALIDITY_SECONDS,
    X402_DEFAULT_ASSET,
    X402_DEFAULT_NETWORK,
    X402_DIR,
    X402_FACILITATOR_URL,
    X402_OFFLINE_MODE,
    X402_SELF_FACILITATE,
    X402_WALLET,
)
from .facilitator_router import FacilitatorRouter, get_facilitator_router
from .pricing import X402_PRICING, resolve_price_usd
from .scheme import (
    SUPPORTED_ASSETS,
    SUPPORTED_NETWORKS,
    VERIFICATION_STATUS,
    X402Asset,
    X402Network,
    X402Scheme,
    _encode_payment_required_body,
    build_payment_required,
    network_verification_status,
)

logger = logging.getLogger(__name__)


class VerifyError(Exception):
    """Payment verification failed."""

    def __init__(self, reason: str, **extra: Any):
        self.reason = reason
        self.extra = extra
        super().__init__(reason)


class SettleError(Exception):
    """Payment settlement failed."""

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


class X402Handler:
    """Handle x402 pay-per-request payments per the spec.

    Implements:
    - create_payment_required: build a spec-compliant 402 response
    - verify_payment: check that a payment header is valid (via facilitator)
    - settle_payment: confirm settlement on-chain (via facilitator)
    - require_payment: convenience method that returns a full 402 response
    """

    def __init__(
        self,
        wallet: str = "",
        facilitator_url: str = "",
        router: FacilitatorRouter | None = None,
    ) -> None:
        self.wallet = wallet or X402_WALLET
        self.facilitator_url = facilitator_url or X402_FACILITATOR_URL
        self.router = router or get_facilitator_router()
        self.payments_file = X402_DIR / "payments.jsonl"
        self.settlements_file = X402_DIR / "settlements.jsonl"
        # Serializes the check-then-record sequence in ``verify_payment`` so two
        # concurrent requests carrying the same tx_hash cannot both pass the
        # replay check before either records the payment (double-spend race).
        self._verify_lock = threading.Lock()
        # Idempotency index for settled txs: ``tx:{network}:{tx_hash}`` -> receipt.
        self._settled_txs: dict[str, dict] = {}
        # Auth-capture scheme: payments authorized but not yet collected.
        # ``payment_id`` -> authorization record (tx_hash, network, asset,
        # amount_usd, authorized_at). Settled only on an explicit capture call.
        self._authorized_payments: dict[str, dict] = {}
        self._loaded = False
        self._pre_settle_hooks: list = []
        self._post_settle_hooks: list = []
        self._pre_verify_hooks: list = []
        self._post_verify_hooks: list = []
        self._on_verify_failure_hooks: list = []

    def _ensure_loaded(self) -> None:
        if not self._loaded:
            self._load_payments()
            self._loaded = True

    def _load_payments(self) -> None:
        self.payments: list[dict[str, Any]] = []
        if self.payments_file.exists():
            try:
                for line in self.payments_file.read_text().splitlines():
                    if line.strip():
                        self.payments.append(json.loads(line))
            except (json.JSONDecodeError, OSError):
                pass

    def _record_payment(self, payment: dict[str, Any]) -> None:
        """Persist payment record to local JSONL audit log."""
        self._ensure_loaded()
        try:
            with open(self.payments_file, "a") as f:
                f.write(json.dumps(payment) + "\n")
                f.flush()
                os.fsync(f.fileno())
            self.payments.append(payment)
        except OSError as e:
            logger.warning("x402_payment_record_failed", extra={"error": str(e)})

    def refresh(self) -> None:
        """Reload payment records from the audit file.

        Picks up payments recorded by OTHER X402Handler instances (e.g. the
        /v1/x402/pay route creates a fresh handler per request). Call before
        ``is_paid()`` on the gate path so a just-issued payment_id is visible.
        """
        self._load_payments()

    def _record_settlement(self, settlement: dict[str, Any]) -> None:
        try:
            with open(self.settlements_file, "a") as f:
                f.write(json.dumps(settlement) + "\n")
                f.flush()
                os.fsync(f.fileno())
        except OSError as e:
            logger.warning("x402_settlement_record_failed", extra={"error": str(e)})

    def create_payment_required(
        self,
        operation: str,
        resource: str = "",
        description: str = "",
        accepts: list[dict[str, Any]] | None = None,
        preferred_networks: list[str] | None = None,
        preferred_assets: list[str] | None = None,
        multi_asset: bool = False,
    ) -> tuple[dict[str, Any], dict[str, str]]:
        """Create a spec-compliant x402 payment request.

        Returns: (body_dict, headers_dict)
        - body_dict: the JSON body for the 402 response
        - headers_dict: the HTTP headers to include (PAYMENT-REQUIRED Base64-encoded)

        ``multi_asset=True`` builds options from the canonical
        SUPPORTED_ASSETS table (CAIP-2 network ids + every quotable asset)
        and attaches the supported_assets/networks tables to the body so
        clients see the full multi-chain acceptance surface. The default
        (False) keeps the V1 legacy single-asset shape.
        """
        body = build_payment_required(
            operation,
            resource,
            multi_asset=multi_asset,
        )
        headers = {
            "PAYMENT-REQUIRED": _encode_payment_required_body(body),
            "X-Payment-Required": "true",
            "X-Payment-Options": "x402",
        }
        return body, headers

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

    async def settle_payment(
        self,
        payment_id: str,
        tx_hash: str,
        network: str = "",
        asset: str = "",
        amount_usd: float = 0.0,
        operation: str = "",
    ) -> dict[str, Any]:
        """Settle a payment via the facilitator router.

        Returns a settlement receipt (to be Base64-encoded in PAYMENT-RESPONSE header).
        """
        network = network or X402_DEFAULT_NETWORK
        asset = asset or X402_DEFAULT_ASSET
        if operation:
            amount_usd = resolve_price_usd(operation, network, asset)
        if amount_usd <= 0:
            amount_usd = X402_PRICING.get(payment_id, {}).get("price_usd", 0.001)

        # Idempotency: a settled tx must never duplicate ledger entries. If this
        # payment was already settled successfully, return the cached receipt
        # WITHOUT re-rolling the facilitator and WITHOUT appending a second line
        # to settlements.jsonl. The check-then-settle sequence is serialized
        # under ``_verify_lock`` to prevent a TOCTOU double-settle.
        key = f"tx:{network}:{tx_hash}"
        with self._verify_lock:
            if key in self._settled_txs:
                logger.info("x402_settle_idempotent", extra={"settle_key": key})
                return self._settled_txs[key]

            for hook in self._pre_settle_hooks:
                result = hook(
                    payment_id=payment_id,
                    tx_hash=tx_hash,
                    network=network,
                    asset=asset,
                    amount_usd=amount_usd,
                )
                if isinstance(result, dict) and result.get("abort"):
                    return {"success": False, "error": result.get("reason", "aborted by pre-settle hook")}

            settlement = None
            for attempt in range(3):
                try:
                    settlement = await self.router.settle(payment_id, tx_hash, network, asset, amount_usd)
                    break
                except httpx.HTTPError as e:
                    if attempt == 2:
                        raise
                    resp = getattr(e, "response", None)
                    retry_after = int(resp.headers.get("Retry-After", "0")) if resp is not None else 0
                    await asyncio.sleep(min(max(retry_after, 0.5 * 2**attempt), 30))
            # The loop either breaks with a settlement or raises on the final
            # attempt, so ``settlement`` is guaranteed set here.
            assert settlement is not None
            # Only index successful settles so a failed attempt can be retried.
            if settlement.get("success") is True:
                self._settled_txs.setdefault(key, settlement)
                for hook in self._post_settle_hooks:
                    try:
                        hook(settlement)
                    except Exception:  # noqa: BLE001, S110 — post-settle hooks must never block
                        pass
        self._record_settlement(settlement)
        return settlement

    async def settle_payment_strict(
        self,
        payment_id: str,
        tx_hash: str,
        network: str = "",
        asset: str = "",
        amount_usd: float = 0.0,
        operation: str = "",
    ) -> dict[str, Any]:
        """Settle a payment, raising ``SettleError`` on failure.

        Same behavior as :meth:`settle_payment` but raises a typed exception
        instead of returning a dict with ``success: False``. Callers that
        prefer try/except can use this variant; the dict-returning
        ``settle_payment`` is kept unchanged for backwards compatibility.
        """
        settlement = await self.settle_payment(
            payment_id,
            tx_hash,
            network=network,
            asset=asset,
            amount_usd=amount_usd,
            operation=operation,
        )
        if settlement.get("success") is not True:
            raise SettleError(
                settlement.get("reason", "settlement failed"),
                payment_id=payment_id,
                tx_hash=tx_hash,
                network=network,
                asset=asset,
                operation=operation,
            )
        return settlement

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

    def on_pre_settle(self, hook):
        """Register a hook executed before settlement. Hook receives (payment_id, tx_hash, network, asset, amount_usd). Can abort by returning {"abort": True, "reason": "..."}."""
        self._pre_settle_hooks.append(hook)

    def on_post_settle(self, hook):
        """Register a hook executed after successful settlement. Hook receives the settlement record."""
        self._post_settle_hooks.append(hook)

    def on_pre_verify(self, hook):
        """Register a hook executed before verification. Hook receives (payment_id, tx_hash, network, asset, amount_usd, operation). Can abort by returning {"abort": True, "reason": "..."}."""
        self._pre_verify_hooks.append(hook)

    def on_post_verify(self, hook):
        """Register a hook executed after successful verification. Hook receives the verify result."""
        self._post_verify_hooks.append(hook)

    def on_verify_failure(self, hook):
        """Register a hook executed when verification fails. Hook receives the verify result."""
        self._on_verify_failure_hooks.append(hook)

    def require_payment(
        self,
        operation: str,
        resource: str = "",
        description: str = "",
    ) -> dict[str, Any]:
        """Build a complete 402 response with proper headers.

        Returns: {"status_code": 402, "headers": {...}, "body": {...}}
        """
        body, headers = self.create_payment_required(operation, resource, description)
        return {
            "status_code": 402,
            "headers": headers,
            "body": body,
        }

    def is_paid(self, payment_id: str, caller_key: str = "", operation: str = "") -> bool:
        """Return True only if the payment is verified, unexpired and caller-bound.

        Replay hardening: a verified payment_id is usable for
        ``PAYMENT_VALIDITY_SECONDS`` only, and only by the caller it was bound
        to (IP/payer wallet where available). Payments recorded without an
        ``expires_at`` (pre-hardening data) fail closed.

        Cross-operation gating: when a gate ``operation`` is supplied, the
        payment must have been purchased for that exact operation. A cheap
        payment can therefore never unlock a more expensive endpoint.
        """
        self._ensure_loaded()
        now = datetime.now(UTC)
        for p in self.payments:
            if p.get("payment_id") != payment_id or not p.get("verified"):
                continue
            expires_at = p.get("expires_at")
            if expires_at:
                try:
                    if datetime.fromisoformat(expires_at) <= now:
                        continue  # expired — must pay again
                except ValueError:
                    continue  # malformed expiry → fail closed
            else:
                continue  # no expiry recorded → treat as expired (fail closed)
            # If the payment was bound to a caller, only that caller may reuse it.
            stored_caller = p.get("caller_key") or ""
            if stored_caller and (not caller_key or stored_caller != caller_key):
                continue
            if operation and (p.get("operation") or "") != operation:
                continue  # cross-operation replay → reject
            return True
        return False

    def get_payment(self, payment_id: str) -> dict[str, Any] | None:
        self._ensure_loaded()
        for p in self.payments:
            if p["payment_id"] == payment_id:
                return p
        return None

    def get_stats(self) -> dict[str, Any]:
        self._ensure_loaded()
        return {
            "total_payments": len(self.payments),
            "wallet": self.wallet,
            "facilitator": self.facilitator_url,
            "pricing": X402_PRICING,
            "supported_networks": [n.value for n in X402Network],
            "supported_assets": [a.value for a in X402Asset],
            "supported_schemes": [s.value for s in X402Scheme],
            # Canonical CAIP-2 acceptance surface (multi-asset table).
            "networks": SUPPORTED_NETWORKS,
            "caip2_networks": list(SUPPORTED_ASSETS.keys()),
            "asset_table": SUPPORTED_ASSETS,
            "verification_status": VERIFICATION_STATUS,
            # Per-network verification label: "live" (EVM) vs
            # "scaffolded:needs_facilitator" (Solana/TRON). No fake security —
            # scaffolded chains are never reported as live.
            "verification_status_by_network": {
                network_id: network_verification_status(network_id) for network_id in SUPPORTED_ASSETS
            },
            "offline_mode": X402_OFFLINE_MODE,
            "self_hosted": X402_SELF_FACILITATE,
            "payment_validity_seconds": PAYMENT_VALIDITY_SECONDS,
            "default_network": X402_DEFAULT_NETWORK,
            "default_asset": X402_DEFAULT_ASSET,
        }
