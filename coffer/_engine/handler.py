"""Coffer — X402Handler (verify/settle orchestration).

Thin orchestrator that composes the verify/settle/response/tracking mixins
into the single public :class:`X402Handler`. Owns the handler's shared
state (audit files, replay lock, idempotency index, auth-capture records,
hooks) and the JSONL persistence helpers; the behavioral logic lives in
focused modules:

- :mod:`coffer._engine.verify_handler`  — verification orchestration
- :mod:`coffer._engine.settle_handler`  — settlement orchestration
- :mod:`coffer._engine.response_handler` — 402 response formatting + gates
- :mod:`coffer._engine.tracking`        — usage / subscription / product trackers

The public names (``X402Handler``, ``VerifyError``, ``SettleError``,
``UsageTracker``, ``SubscriptionManager``, ``ProductManager``) are
re-exported here so existing ``from coffer._engine.handler import ...``
call sites keep working unchanged.
"""
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Rug Munch Media LLC
# Part of Coffer — https://git.rugmunch.io/RugMunchMedia/x402-coffer

from __future__ import annotations

import json
import logging
import os
import threading
from typing import Any

from .facilitator_config import X402_DIR, X402_FACILITATOR_URL, X402_WALLET
from .facilitator_router import FacilitatorRouter, get_facilitator_router
from .response_handler import ResponseMixin
from .settle_handler import SettleError, SettleMixin
from .tracking import ProductManager, SubscriptionManager, UsageTracker
from .verify_handler import FacilitatorTimeoutError, VerifyError, VerifyMixin

logger = logging.getLogger(__name__)

__all__ = [
    "FacilitatorTimeoutError",
    "ProductManager",
    "SettleError",
    "SubscriptionManager",
    "UsageTracker",
    "VerifyError",
    "X402Handler",
]


class X402Handler(VerifyMixin, SettleMixin, ResponseMixin):
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
        # Per-token usage analytics (settlement volume, gas cost, response time).
        self.usage = UsageTracker()
        # Recurring subscriptions with grace period state machine.
        self.subscriptions = SubscriptionManager()
        # Paywalled URL products: pay → get access token → redirect.
        self.products = ProductManager()

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
