"""x402 — X402Handler (verify/settle orchestration).

Owns the handler that creates spec-compliant 402 responses, verifies
payments via the facilitator router, settles them, and tracks payment
records with replay-hardening (expiry + caller binding). Config constants
come from the package root; scheme/facilitator helpers from their modules.
"""
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Rug Munch Media LLC
#
# Standalone x402 implementation — https://git.rugmunch.io/RugMunchMedia/x402-py
# Licensed under MIT. See LICENSE.

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from x402 import (
    PAYMENT_VALIDITY_SECONDS,
    X402_DEFAULT_ASSET,
    X402_DEFAULT_NETWORK,
    X402_DIR,
    X402_FACILITATOR_URL,
    X402_OFFLINE_MODE,
    X402_SELF_FACILITATE,
    X402_WALLET,
)
from x402.facilitator import FacilitatorRouter, get_facilitator_router
from x402.scheme import (
    SUPPORTED_ASSETS,
    SUPPORTED_NETWORKS,
    VERIFICATION_STATUS,
    X402_PRICING,
    X402Asset,
    X402Network,
    X402Scheme,
    _encode_payment_required_body,
    build_payment_required,
    network_verification_status,
    resolve_price_usd,
)

logger = logging.getLogger(__name__)


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
        self._load_payments()

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
        try:
            with open(self.payments_file, "a") as f:
                f.write(json.dumps(payment) + "\n")
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
            accepts,
            description,
            preferred_networks=preferred_networks,
            preferred_assets=preferred_assets,
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
            amount_usd = resolve_price_usd(operation)
        if amount_usd <= 0:
            amount_usd = X402_PRICING.get(payment_id, {}).get("price_usd", 0.001)

        result = await self.router.verify(payment_id, tx_hash, network, asset, amount_usd)
        payment_record = {
            "payment_id": payment_id,
            "tx_hash": tx_hash,
            "network": network,
            "asset": asset,
            "amount_usd": amount_usd,
            "verified": result["verified"],
            "reason": result.get("reason", ""),
            "facilitator": result.get("facilitator", ""),
            "caller_key": caller_key or "",
            "expires_at": (datetime.now(UTC) + timedelta(seconds=PAYMENT_VALIDITY_SECONDS)).isoformat(),
            "verified_at": datetime.now(UTC).isoformat(),
        }
        self._record_payment(payment_record)
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
            amount_usd = resolve_price_usd(operation)
        if amount_usd <= 0:
            amount_usd = X402_PRICING.get(payment_id, {}).get("price_usd", 0.001)

        settlement = await self.router.settle(payment_id, tx_hash, network, asset, amount_usd)
        self._record_settlement(settlement)
        return settlement

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

    def is_paid(self, payment_id: str, caller_key: str = "") -> bool:
        """Return True only if the payment is verified, unexpired and caller-bound.

        Replay hardening: a verified payment_id is usable for
        ``PAYMENT_VALIDITY_SECONDS`` only, and only by the caller it was bound
        to (IP/payer wallet where available). Payments recorded without an
        ``expires_at`` (pre-hardening data) fail closed.
        """
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
            return True
        return False

    def get_payment(self, payment_id: str) -> dict[str, Any] | None:
        for p in self.payments:
            if p["payment_id"] == payment_id:
                return p
        return None

    def get_stats(self) -> dict[str, Any]:
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
