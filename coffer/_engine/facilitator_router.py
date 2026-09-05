# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Rug Munch Media LLC
# --- facilitator_router.py: smart facilitator router facade + global accessors ---
#
# Health tracking lives in facilitator_health.py; verify/settle orchestration
# lives in facilitator_orchestration.py. This module keeps the public API
# (FacilitatorRouter + global accessors) intact.
from __future__ import annotations

from typing import Any

from .facilitator_config import (
    DEFAULT_FACILITATORS,
    X402_SELF_FACILITATE,
    X402_SELF_FACILITATOR_KEY,
    X402_SELF_FACILITATOR_RPC,
    X402_WALLET,
    FacilitatorConfig,
)
from .facilitator_health import FacilitatorHealth
from .facilitator_orchestration import settle as _settle_impl
from .facilitator_orchestration import verify as _verify_impl
from .pricing import network_family, normalize_network_id
from .replay_redis import create_replay_store
from .self_facilitator import SelfFacilitator


class FacilitatorRouter:
    """Smart router that picks the best facilitator per chain/asset.

    Falls back through enabled external facilitators with health-aware
    decimation (circuit breaker after 3 failures). ``self_hosted`` mode
    (default on) uses :class:`SelfFacilitator` + EIP-7702 + the Solana SPL RPC
    verifier INSTEAD of the external URLs — no Coinbase/CDP dependency.
    Replay protection burns the tx_hash via Redis SETNX (24h TTL).
    """

    def __init__(
        self,
        facilitators: list[FacilitatorConfig] | None = None,
        self_hosted: bool | None = None,
    ) -> None:
        self.facilitators = sorted(
            [f for f in (facilitators or DEFAULT_FACILITATORS) if f.enabled and f.verify_url],
            key=lambda f: f.priority,
        )
        self.health = FacilitatorHealth()
        self.self_hosted = X402_SELF_FACILITATE if self_hosted is None else bool(self_hosted)
        self._self_facilitator: SelfFacilitator | None = None
        self._network_facilitators: dict[str, Any] = {}
        self._custom_facilitators: dict[str, Any] = {}
        self._pending_auths: dict[str, dict[str, Any]] = {}
        # Redis-backed when X402_REDIS_URL is set (multi-instance safe);
        # in-memory otherwise (single instance).
        self.replay = create_replay_store()

    # ── health / extension points ──────────────────────────────────────────

    def _is_healthy(self, config: FacilitatorConfig) -> bool:
        return self.health.is_healthy(config)

    def _health_score(self, config: FacilitatorConfig) -> float:
        return self.health.health_score(config)

    def _mark_failure(self, config: FacilitatorConfig) -> None:
        self.health.mark_failure(config)

    def _mark_success(self, config: FacilitatorConfig, start_time: float | None = None) -> None:
        self.health.mark_success(config, start_time)

    def register_facilitator(self, network: str, impl: Any) -> None:
        """Register a custom facilitator implementation for a specific network.
        The impl must have async verify(payment_id, tx_hash, network, asset, amount_usd) and async settle(...) methods.
        """
        self._custom_facilitators[network] = impl
        network_id = normalize_network_id(network)
        self._network_facilitators[network_id] = impl
        if ":" not in network:
            self._network_facilitators[network] = impl

    def submit_authorization(self, payment_id: str, authorization: dict[str, Any]) -> None:
        self._pending_auths[payment_id] = authorization

    def readiness(self) -> dict[str, Any]:
        wallet = (X402_WALLET or "").strip()
        wallet_configured = bool(wallet and wallet != "coffer-unconfigured-wallet")
        mode = "self_hosted" if self.self_hosted else "external"
        if self.self_hosted:
            fac = self._get_self_facilitator()
            ready = wallet_configured and bool(fac.rpc_url or fac.pay_to)
        else:
            ready = wallet_configured and any(f.enabled and f.verify_url for f in self.facilitators)
        return {"mode": mode, "ready": ready, "wallet_configured": wallet_configured}

    def settlement_stats(self) -> dict[str, Any]:
        fac = self._get_self_facilitator()
        return {"nonces_seen": len(fac.nonces), "pending": len(self._pending_auths)}

    def _impl_for(self, network: str) -> Any | None:
        network_id = normalize_network_id(network)
        family = network_family(network)
        return (
            self._network_facilitators.get(network_id)
            or self._network_facilitators.get(network)
            or self._network_facilitators.get(family)
        )

    def _get_self_facilitator(self) -> SelfFacilitator:
        if self._self_facilitator is None:
            self._self_facilitator = SelfFacilitator(
                pay_to=X402_WALLET,
                facilitator_key=X402_SELF_FACILITATOR_KEY,
                rpc_url=X402_SELF_FACILITATOR_RPC,
            )
        return self._self_facilitator

    def _build_payment(
        self, payment_id: str, tx_hash: str, network: str, asset: str, amount_usd: float
    ) -> dict[str, Any]:
        payment: dict[str, Any] = {
            "payment_id": payment_id,
            "tx_hash": tx_hash,
            "network": network,
            "asset": asset,
            "amount_usd": amount_usd,
        }
        auth = self._pending_auths.pop(payment_id, None)
        if auth:
            payment["authorization"] = auth
        return payment

    def _replay_key(self, tx_hash: str, network: str) -> str:
        return f"x402:replay:{normalize_network_id(network)}:{str(tx_hash).strip().lower()}"

    async def _enforce_replay(self, tx_hash: str, network: str) -> tuple[bool, str]:
        """Burn the tx_hash nonce. Returns (ok, reason); ok=False => replay."""
        if not tx_hash:
            return True, ""
        key = self._replay_key(tx_hash, network)
        if not await self.replay.check_and_burn(key):
            return False, "transaction already used (replay)"
        return True, ""

    # ── verify / settle (orchestration in facilitator_orchestration) ───────

    async def verify(
        self,
        payment_id: str,
        tx_hash: str,
        network: str,
        asset: str,
        amount_usd: float,
    ) -> dict[str, Any]:
        """Verify a payment with the best available facilitator."""
        return await _verify_impl(self, payment_id, tx_hash, network, asset, amount_usd)

    async def settle(
        self,
        payment_id: str,
        tx_hash: str,
        network: str,
        asset: str,
        amount_usd: float,
    ) -> dict[str, Any]:
        """Confirm settlement with the best available facilitator."""
        return await _settle_impl(self, payment_id, tx_hash, network, asset, amount_usd)


# ── Global router instance (mirrors x402-py accessors) ──────────────────────

_facilitator_router: FacilitatorRouter | None = None


def get_facilitator_router() -> FacilitatorRouter:
    """Return the global facilitator router (lazily initialised)."""
    global _facilitator_router
    if _facilitator_router is None:
        _facilitator_router = FacilitatorRouter()
    return _facilitator_router


def set_facilitator_router(router: FacilitatorRouter) -> None:
    """Override the global facilitator router (for tests)."""
    global _facilitator_router
    _facilitator_router = router


def register_facilitator(network: str, impl: Any) -> None:
    """Register a custom facilitator for ``network`` on the global router.

    Extension point — e.g. plug in a real TRON TRC-20 verifier once one exists
    (currently TRON is scaffolded). ``impl`` must expose
    ``async verify(payment) -> VerifyResult`` and ``async settle(payment) -> str | None``.
    """
    get_facilitator_router().register_facilitator(network, impl)
