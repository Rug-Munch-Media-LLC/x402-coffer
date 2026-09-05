# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Rug Munch Media LLC
# --- facilitator_router.py: smart facilitator orchestration + global accessors ---
from __future__ import annotations

import asyncio
import json
import logging
import math
import random
import time
from datetime import UTC, datetime
from typing import Any

import httpx

from .evm_verify import _eip7702_verify
from .external_verify import normalize_settlement, verify_external, verify_solana
from .facilitator_config import (
    DEFAULT_FACILITATORS,
    X402_NETWORK_WALLETS,
    X402_OFFLINE_MODE,
    X402_SELF_FACILITATE,
    X402_SELF_FACILITATOR_KEY,
    X402_SELF_FACILITATOR_RPC,
    X402_TIMEOUT_SECONDS,
    X402_WALLET,
    FacilitatorConfig,
)
from .models import VerifyResult
from .pricing import (
    SCAFFOLDED_NETWORKS,
    asset_to_atomic,
    network_family,
    network_verification_status,
    normalize_network_id,
    resolve_asset_entry,
)
from .replay import REPLAY_HTTP_STATUS
from .replay_redis import create_replay_store
from .self_facilitator import SelfFacilitator
from .tron_verifier import verify_tron_payment

log = logging.getLogger(__name__)


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
        self._failures: dict[str, tuple[int, float]] = {}
        self._successes: dict[str, int] = {}
        self._latencies: dict[str, list[float]] = {}
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
        entry = self._failures.get(config.name)
        if entry is None:
            return True
        count, last_failure = entry
        if count >= 3 and time.time() - last_failure > 60:
            return True  # half-open: allow a probe after the cooldown window
        return count < 3

    def _health_score(self, config: FacilitatorConfig) -> float:
        successes = self._successes.get(config.name, 0)
        failures = self._failures.get(config.name, (0, 0))[0]
        latencies = self._latencies.get(config.name, [])
        avg_latency = sum(latencies) / len(latencies) if latencies else 0.0
        return successes - failures * 2 - avg_latency

    def _mark_failure(self, config: FacilitatorConfig) -> None:
        count, _ = self._failures.get(config.name, (0, 0.0))
        self._failures[config.name] = (count + 1, time.time())
        log.warning("x402_facilitator_failure", extra={"facilitator": config.name})

    def _mark_success(self, config: FacilitatorConfig, start_time: float | None = None) -> None:
        self._successes[config.name] = self._successes.get(config.name, 0) + 1
        if start_time is not None:
            latencies = self._latencies.setdefault(config.name, [])
            latencies.append(time.time() - start_time)
            if len(latencies) > 100:
                del latencies[: len(latencies) - 100]
        entry = self._failures.get(config.name)
        if entry is None:
            return
        count, _ = entry
        if count <= 1:
            self._failures.pop(config.name, None)
        else:
            self._failures[config.name] = (count - 1, time.time())

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

    # ── verify ─────────────────────────────────────────────────────────────

    async def verify(
        self,
        payment_id: str,
        tx_hash: str,
        network: str,
        asset: str,
        amount_usd: float,
    ) -> dict[str, Any]:
        """Verify a payment with the best available facilitator.

        Returns ``{"verified", "reason", "settlement", "facilitator", ...}``.
        Replay-protected: a successfully-verified tx_hash is burned (SETNX,
        24h TTL) and cannot be reused.
        """
        network_id = normalize_network_id(network)

        custom = self._custom_facilitators.get(network_id)
        if custom:
            return await custom.verify(payment_id, tx_hash, network, asset, amount_usd)

        # Reject unsupported networks before any dispatch to verifiers.
        if not network_id:
            return {"verified": False, "reason": "unsupported network", "settlement": None}

        # Reject invalid amounts before any network call.
        if amount_usd is None or not math.isfinite(float(amount_usd)) or amount_usd < 0:
            return {
                "verified": False,
                "reason": "invalid (non-finite/negative) amount",
                "settlement": None,
                "network": network_id,
            }

        if X402_OFFLINE_MODE:
            ok, reason = await self._enforce_replay(tx_hash, network_id)
            if not ok:
                return {
                    "verified": False,
                    "reason": reason,
                    "settlement": None,
                    "network": network_id,
                    "http_status": REPLAY_HTTP_STATUS,
                }
            return {"verified": True, "reason": "offline_mode", "settlement": None}

        # TRON: real TRC-20 verifier (no longer scaffolded).
        if network_family(network_id) == "tron":
            return await self._verify_tron(payment_id, tx_hash, network_id, asset, amount_usd)

        # Other scaffolded networks stay scaffolded — never claimed verified.
        if network_id in SCAFFOLDED_NETWORKS:
            return self._scaffold_non_evm_verify(payment_id, tx_hash, network_id, asset, amount_usd)

        # Self-hosted / custom impl path (default).
        if self.self_hosted or self._impl_for(network_id) is not None:
            result = await self._verify_self_hosted(payment_id, tx_hash, network_id, asset, amount_usd)
            if result.get("verified"):
                ok, reason = await self._enforce_replay(tx_hash, network_id)
                if not ok:
                    return {
                        "verified": False,
                        "reason": reason,
                        "settlement": None,
                        "network": network_id,
                        "http_status": REPLAY_HTTP_STATUS,
                    }
            return result

        # External facilitator path.
        for attempt in range(3):
            try:
                verified_result = await verify_external(
                    self, payment_id, tx_hash, network_id, asset, amount_usd
                )
                break
            except (httpx.HTTPError, asyncio.TimeoutError):  # noqa: UP041 — explicit per spec
                if attempt == 2:
                    raise
                delay = 0.5 * (2**attempt)
                await asyncio.sleep(delay)
        from coffer._engine.observability import observe_verify
        from coffer._engine.webhooks import emit_payment_event

        observe_verify(verified_result, payment_id=payment_id, tx_hash=tx_hash)
        emit_payment_event(
            "payment.verified",
            payment_id=payment_id,
            transaction=tx_hash,
            network=network_id,
            verified=bool(verified_result.get("verified")),
        )
        return verified_result

    async def _verify_self_hosted(
        self,
        payment_id: str,
        tx_hash: str,
        network: str,
        asset: str,
        amount_usd: float,
    ) -> dict[str, Any]:
        network_id = normalize_network_id(network)
        impl = self._impl_for(network_id) or self._get_self_facilitator()
        payment = self._build_payment(payment_id, tx_hash, network_id, asset, amount_usd)
        try:
            result: Any = await impl.verify(payment)
        except Exception as e:  # noqa: BLE001 — facilitator verify never raises
            result = VerifyResult(verified=False, reason=f"facilitator verify error: {e}")
        if getattr(result, "verified", False):
            return {
                "verified": True,
                "reason": "",
                "settlement": None,
                "facilitator": "self-hosted",
                "signer": getattr(result, "signer", ""),
                "amount_atomic": getattr(result, "amount_atomic", 0),
                "network": getattr(result, "network", None) or network_id,
                "asset": getattr(result, "asset", None) or asset,
            }
        reason = getattr(result, "reason", "") or ""
        if reason == "replay" or "replay" in reason:
            return {
                "verified": False,
                "reason": "transaction already used (replay)",
                "settlement": None,
                "network": network_id,
                "http_status": REPLAY_HTTP_STATUS,
            }

        # EIP-7702 self-verify fallback for EVM chains (receipt + payTo check).
        if network_family(network_id) == "evm":
            eip = await _eip7702_verify(tx_hash, network_id, asset, amount_usd)
            if eip["verified"]:
                eip["facilitator"] = "self-hosted-eip7702"
                eip["network"] = network_id
                eip["hex_reason"] = reason
                return eip
            reason = f"{reason}; EIP-7702: {eip.get('reason', '')}"
        elif network_family(network_id) == "solana":
            return await verify_solana(payment_id, tx_hash, network_id, asset, amount_usd)

        return {
            "verified": False,
            "reason": reason,
            "settlement": None,
            "facilitator": "self-hosted",
            "network": network_id,
        }

    async def _verify_tron(
        self,
        payment_id: str,
        tx_hash: str,
        network: str,
        asset: str,
        amount_usd: float,
    ) -> dict[str, Any]:
        """Verify a TRON TRC-20 USDT payment via the Trongrid RPC."""
        import os

        network_id = normalize_network_id(network)
        recipient = X402_NETWORK_WALLETS.get(network_id) or X402_WALLET
        rpc_url = os.getenv("X402_TRON_RPC", "https://api.trongrid.io")
        fallbacks = [u.strip() for u in os.getenv("X402_TRON_RPC_FALLBACKS", "").split(",") if u.strip()]

        result = await verify_tron_payment(
            tx_hash=tx_hash,
            recipient=recipient,
            amount_usd=amount_usd,
            rpc_url=rpc_url,
            fallback_rpc_urls=fallbacks,
        )
        result["network"] = network_id
        result["payment_id"] = payment_id
        result["facilitator"] = "tron-rpc"
        if result.get("verified"):
            ok, reason = await self._enforce_replay(tx_hash, network_id)
            if not ok:
                return {
                    "verified": False,
                    "reason": reason,
                    "settlement": None,
                    "network": network_id,
                    "http_status": REPLAY_HTTP_STATUS,
                }
        return result

    def _scaffold_non_evm_verify(
        self,
        payment_id: str,
        tx_hash: str,
        network: str,
        asset: str,
        amount_usd: float,
    ) -> dict[str, Any]:
        """[REVIEW] Scaffolded acceptance for TRON — NEVER returns verified.

        Header structure, asset membership and non-negative USD amount are
        validated; on-chain settlement is NOT checked. Result is explicitly
        marked ``scaffolded`` and ``verified: false`` so it can never be
        treated as paid until a real TRC-20 verifier is wired in.
        """
        network_id = normalize_network_id(network)
        entry = resolve_asset_entry(network_id, asset)
        if not payment_id or not tx_hash:
            return {
                "verified": False,
                "reason": "needs_facilitator: missing payment_id/tx_hash in header",
                "settlement": None,
                "scaffolded": True,
                "network": network_id,
            }
        if entry is None:
            return {
                "verified": False,
                "reason": f"needs_facilitator: asset {asset!r} not in SUPPORTED_ASSETS for {network_id}",
                "settlement": None,
                "scaffolded": True,
                "network": network_id,
            }
        if amount_usd is None or amount_usd < 0:
            return {
                "verified": False,
                "reason": "needs_facilitator: invalid (non-finite/negative) amount",
                "settlement": None,
                "scaffolded": True,
                "network": network_id,
            }
        return {
            "verified": False,
            "reason": "needs_facilitator: on-chain settlement verification not implemented for this chain",
            "settlement": None,
            "scaffolded": True,
            "network": network_id,
            "asset": asset,
            "supported": True,
            "verification_status": network_verification_status(network_id),
            "note": "Header structure, asset and price validated; settlement NOT verified. "
            "See VERIFICATION_STATUS.",
        }

    # ── settle ─────────────────────────────────────────────────────────────

    async def settle(
        self,
        payment_id: str,
        tx_hash: str,
        network: str,
        asset: str,
        amount_usd: float,
    ) -> dict[str, Any]:
        """Confirm settlement with the best available facilitator."""
        network_id = normalize_network_id(network)
        if X402_OFFLINE_MODE:
            return {
                "success": True,
                "transaction": tx_hash,
                "network": network_id,
                "payment_id": payment_id,
                "facilitator": "offline",
                "settled_at": datetime.now(UTC).isoformat(),
                "note": "Offline mode",
            }

        if self.self_hosted or self._impl_for(network_id) is not None:
            return await self._settle_self_hosted(payment_id, tx_hash, network_id, asset, amount_usd)

        payload = {
            "paymentId": payment_id,
            "transactionHash": tx_hash,
            "network": network_id,
            "asset": asset,
            "amount": str(asset_to_atomic(amount_usd, asset, network_id)),
            "payToAddress": X402_NETWORK_WALLETS.get(network_id) or X402_WALLET,
            "scheme": "exact",
            "timestamp": datetime.now(UTC).isoformat(),
        }
        last_error = "No facilitators available"
        async with httpx.AsyncClient(timeout=X402_TIMEOUT_SECONDS) as client:
            healthy = [f for f in self.facilitators if self._is_healthy(f) and f.settle_url]
            if not healthy:
                return {
                    "success": False,
                    "payment_id": payment_id,
                    "reason": last_error,
                }
            # Weighted random: higher health score = higher selection probability
            scores = [max(1, self._health_score(f)) for f in healthy]
            total = sum(scores)
            weights = [s / total for s in scores]
            selected = random.choices(healthy, weights=weights, k=1)[0]
            # Try the weighted-random pick first, then fall through to the rest.
            ordered = [selected] + [f for f in healthy if f is not selected]
            for config in ordered:
                try:
                    start_time = time.time()
                    response = await client.post(config.settle_url, json=payload)
                    response.raise_for_status()
                    data = response.json()
                    self._mark_success(config, start_time)
                    settled = normalize_settlement(data, payment_id, tx_hash, network_id, config.name)
                    from coffer._engine.observability import observe_settle

                    observe_settle(settled, payment_id=payment_id, tx_hash=tx_hash)
                    return settled
                except httpx.HTTPStatusError as e:
                    last_error = f"{config.name}: HTTP {e.response.status_code}"
                    self._mark_failure(config)
                except httpx.RequestError as e:
                    last_error = f"{config.name}: {e}"
                    self._mark_failure(config)
                except json.JSONDecodeError as e:
                    last_error = f"{config.name}: invalid JSON - {e}"
                    self._mark_failure(config)
        if network_family(network_id) == "evm":
            # EIP-7702 self-VERIFY checks on-chain receipts; it does NOT
            # broadcast a settlement. Returning success here would fabricate
            # a settlement record (C1 honesty fix) -> fail closed.
            return {
                "success": False,
                "payment_id": payment_id,
                "reason": (
                    "needs_facilitator: no external facilitator settled and "
                    "no self-hosted settle key configured (EIP-7702 verifies "
                    "on-chain receipts but does not broadcast settlements)"
                ),
                "scaffolded": True,
                "network": network_id,
                "facilitator": "eip-7702-verify-only",
            }
        if network_family(network_id) in ("solana", "tron"):
            return {
                "success": False,
                "payment_id": payment_id,
                "reason": "needs_facilitator: settlement verification not implemented for this chain",
                "scaffolded": True,
                "network": network_id,
            }
        return {"success": False, "payment_id": payment_id, "reason": last_error}

    async def _settle_self_hosted(
        self,
        payment_id: str,
        tx_hash: str,
        network: str,
        asset: str,
        amount_usd: float,
    ) -> dict[str, Any]:
        network_id = normalize_network_id(network)
        impl = self._impl_for(network_id) or self._get_self_facilitator()
        payment = self._build_payment(payment_id, tx_hash, network_id, asset, amount_usd)
        try:
            settled_tx = await impl.settle(payment)
        except Exception as e:  # noqa: BLE001 — settle failure is handled below
            settled_tx = None
            log.warning("x402_self_settle_error", extra={"error": str(e)})
        if settled_tx:
            return {
                "success": True,
                "transaction": str(settled_tx),
                "network": network_id,
                "payment_id": payment_id,
                "facilitator": "self-hosted",
                "settled_at": datetime.now(UTC).isoformat(),
                "note": "Broadcast by self-hosted facilitator",
            }
        if network_family(network_id) == "solana":
            # SPL RPC verifies on-chain receipts; it does NOT broadcast a
            # settlement. Returning success here would fabricate a settlement
            # record (C2 honesty fix) -> fail closed.
            return {
                "success": False,
                "payment_id": payment_id,
                "reason": (
                    "needs_facilitator: solana SPL RPC verifies on-chain "
                    "receipts but does not broadcast settlements"
                ),
                "scaffolded": True,
                "network": network_id,
                "facilitator": "solana-rpc-verify-only",
            }
        return {
            "success": False,
            "payment_id": payment_id,
            "reason": "self-hosted settle unavailable: no authorization, "
            "no facilitator key/RPC, or chain unreachable",
            "facilitator": "self-hosted",
        }


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
