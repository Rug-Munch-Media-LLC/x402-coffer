"""Coffer — payment settlement orchestration.

Extracted from the former ``handler.py`` god file. Provides the
:class:`SettleMixin` that :class:`coffer._engine.handler.X402Handler`
inherits: settling payments via the facilitator router with retry +
idempotency, the strict (exception-raising) variant, and the settle hook
registration surface.
"""
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Rug Munch Media LLC
# Part of Coffer — https://git.rugmunch.io/RugMunchMedia/x402-coffer

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from .facilitator_config import X402_DEFAULT_ASSET, X402_DEFAULT_NETWORK
from .pricing import X402_PRICING, resolve_price_usd
from .response_handler import _extension_header

logger = logging.getLogger(__name__)


class SettleError(Exception):
    """Payment settlement failed."""

    def __init__(self, reason: str, **extra: Any):
        self.reason = reason
        self.extra = extra
        super().__init__(reason)


class SettleMixin:
    """Payment settlement orchestration (mixed into X402Handler)."""

    # Shared state owned by X402Handler.__init__; declared here so mypy can
    # resolve the attributes used by these methods when the mixin is composed
    # into the concrete handler class.
    router: Any
    usage: Any
    _verify_lock: Any
    _settled_txs: dict[str, dict]
    _pre_settle_hooks: list
    _post_settle_hooks: list
    _record_settlement: Any

    async def settle_payment(
        self,
        payment_id: str,
        tx_hash: str,
        network: str = "",
        asset: str = "",
        amount_usd: float = 0.0,
        operation: str = "",
        user_id: str = "",
    ) -> dict[str, Any]:
        """Settle a payment via the facilitator router.

        Returns a settlement receipt (to be Base64-encoded in PAYMENT-RESPONSE header).
        ``user_id`` attributes the settlement to the initiating customer and is
        persisted on the receipt so billing history can be filtered per user.
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
            _settle_started = asyncio.get_event_loop().time()
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
            # Prometheus metrics (fail-safe, never breaks the payment flow).
            try:
                from .observability import record_rpc_latency, record_settle

                record_rpc_latency(network, "settle", asyncio.get_event_loop().time() - _settle_started)
                record_settle(
                    network,
                    bool(settlement.get("success")),
                    amount_usd=float(amount_usd or 0.0),
                    asset=asset,
                )
            except Exception:  # noqa: BLE001, S110 — observability never breaks payments
                pass
            # Only index successful settles so a failed attempt can be retried.
            if settlement.get("success") is True:
                self._settled_txs.setdefault(key, settlement)
                self.usage.record(
                    payment_id,
                    amount_usd=amount_usd,
                    gas_used=int(settlement.get("gas_used", 0) or 0),
                    response_ms=int(settlement.get("response_ms", 0) or 0),
                    endpoint=operation,
                    network=network,
                )
                for hook in self._post_settle_hooks:
                    try:
                        hook(settlement)
                    except Exception:  # noqa: BLE001, S110 — post-settle hooks must never block
                        pass
        # Stamp customer attribution BEFORE persisting so settlements.jsonl
        # carries the user_id for per-customer billing history.
        if user_id:
            settlement["user_id"] = user_id
        self._record_settlement(settlement)
        settlement["extension_responses"] = _extension_header(settlement)
        return settlement

    async def settle_payment_strict(
        self,
        payment_id: str,
        tx_hash: str,
        network: str = "",
        asset: str = "",
        amount_usd: float = 0.0,
        operation: str = "",
        user_id: str = "",
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
            user_id=user_id,
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

    def on_pre_settle(self, hook):
        """Register a hook executed before settlement. Hook receives (payment_id, tx_hash, network, asset, amount_usd). Can abort by returning {"abort": True, "reason": "..."}."""
        self._pre_settle_hooks.append(hook)

    def on_post_settle(self, hook):
        """Register a hook executed after successful settlement. Hook receives the settlement record."""
        self._post_settle_hooks.append(hook)
