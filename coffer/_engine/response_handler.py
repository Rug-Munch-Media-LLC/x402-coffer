"""Coffer — 402 response formatting and payment-gate queries.

Extracted from the former ``handler.py`` god file. Provides the
:class:`ResponseMixin` that :class:`coffer._engine.handler.X402Handler`
inherits: building spec-compliant 402 responses, replay-hardened
``is_paid``/``get_payment`` gates, and the ``get_stats`` surface.
"""
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Rug Munch Media LLC
# Part of Coffer — https://git.rugmunch.io/RugMunchMedia/x402-coffer

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime
from typing import Any

from .facilitator_config import (
    PAYMENT_VALIDITY_SECONDS,
    X402_DEFAULT_ASSET,
    X402_DEFAULT_NETWORK,
    X402_OFFLINE_MODE,
    X402_SELF_FACILITATE,
)
from .pricing import X402_PRICING
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


def _extension_header(result: dict) -> str:
    """Encode out-of-band extension data as base64 for EXTENSION-RESPONSES header."""
    extras = {k: v for k, v in result.items() if k not in ("verified", "reason", "success")}
    return base64.b64encode(json.dumps(extras).encode()).decode()


class ResponseMixin:
    """402 response formatting + payment-gate queries (mixed into X402Handler)."""

    # Shared state owned by X402Handler.__init__; declared here so mypy can
    # resolve the attributes used by these methods when the mixin is composed
    # into the concrete handler class.
    wallet: str
    facilitator_url: str
    payments: list[dict[str, Any]]
    _ensure_loaded: Any

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
