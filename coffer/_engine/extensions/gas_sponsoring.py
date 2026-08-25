"""Gas-sponsored approval extensions facade — EIP-2612 + ERC-20 Permit2.

Facade over ``permit.py`` (the EIP-2612 crypto layer). Implements the two
official x402 extensions whose only integration point is the FACILITATOR side
("Gas sponsoring extensions ... inject batch signing capabilities into the
settlement flow so the facilitator can sponsor gas on behalf of the payer" —
extensions overview):

1. ``extensions__eip2612-gas-sponsoring.md`` — gasless EIP-2612 ``permit()``.
   Client signs a permit authorizing Permit2 off-chain, facilitator submits it
   atomically during settlement (`x402ExactPermit2Proxy.settleWithPermit`).
2. ``extensions__erc20-approval-gas-sponsoring.md`` — universal fallback for
   tokens WITHOUT EIP-2612. Client signs (not broadcasts) a raw
   ``approve(Permit2, MaxUint256)`` tx, facilitator funds gas + broadcasts it
   atomically before settling (`x402ExactPermit2Proxy.settle`).

Crypto (EIP-2612 path): the sponsor path now performs REAL signature
verification (delegated to ``permit.verify_permit_signature``) so a forged or
tampered permit fails closed; ``permit.build_permit_calldata`` emits the real
``permit(...)`` calldata for the facilitator to broadcast.

Honest status: the sponsor path either delegates to a configured ``sponsor_impl``
(a real sponsor key) or reports ``scaffolded:needs_facilitator`` — it NEVER
returns a fabricated tx_hash and NEVER claims ``verified:true`` for the
on-chain settlement step it did not execute. On-chain replay protection for the
permit is the token's ``nonces(owner)`` counter — an RPC check the scaffold
cannot perform, so it is flagged [REVIEW] and remains a facilitator duty.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from coffer._engine.extensions.permit import (
    EIP2612_KEY,
    ERC20_APPROVAL_KEY,
    MAX_UINT256,
    PERMIT2_ADDRESS,
    _eip2612_available,
    _validate_permit2_approval_payload,
    build_permit_calldata,
    eip2612_digest,
    verify_permit_signature,
)
from coffer._engine.service import VerifyResult, normalize_network_id, register_facilitator

__all__ = [
    "EIP2612_KEY",
    "ERC20_APPROVAL_KEY",
    "MAX_UINT256",
    "PERMIT2_ADDRESS",
    "GasSponsoringFacilitator",
    "build_permit_calldata",
    "declare_eip2612_gas_sponsoring_extension",
    "declare_erc20_approval_gas_sponsoring_extension",
    "eip2612_digest",
    "register_gas_sponsoring_facilitator",
    "verify_permit_signature",
]

log = logging.getLogger(__name__)


# Gas economics (per x402 gas-sponsoring spec):
#   - The BUYER pays gas for their actual payment transaction (the USDC
#     transfer / on-chain payment). Gas sponsoring does NOT cover that.
#   - This extension sponsors ONLY the Permit2 *approval* gas step (EIP-2612
#     permit / ERC-20 approval), so a buyer without ETH can still approve.
#   - The facilitator/merchant fronts the approval tx; the buyer's payment
#     tx gas remains the buyer's responsibility. Never overclaim "gasless".
def declare_eip2612_gas_sponsoring_extension() -> dict[str, dict[str, Any]]:
    """Advertise EIP-2612 gas sponsoring (spec §Server Usage / How It Works)."""
    return {
        EIP2612_KEY: {
            "type": "eip2612",
            "assetTransferMethod": "permit2",
            "description": "Gasless EIP-2612 permit; facilitator submits on chain",
        }
    }


def declare_erc20_approval_gas_sponsoring_extension() -> dict[str, dict[str, Any]]:
    """Advertise ERC-20 approval gas sponsoring (spec §Server Usage)."""
    return {
        ERC20_APPROVAL_KEY: {
            "type": "erc20-approval",
            "assetTransferMethod": "permit2",
            "sponsorsGas": True,
            "description": "Facilitator funds gas + broadcasts approve(Permit2) then settles",
        }
    }


def _chain_id_for(network: str) -> int | None:
    """Resolve the EVM chain id for a legacy name or CAIP-2 network id."""
    nid = normalize_network_id(network)
    if nid.startswith("eip155:"):
        try:
            return int(nid.split(":", 1)[1])
        except ValueError:
            return None
    return None


@dataclass
class GasSponsoringFacilitator:
    """Facilitator-side gas sponsoring with REAL EIP-2612 permit verification.

    The facilitator resolves the correct sponsor flow from the payment payload's
    extension info, VERIFIES the EIP-2612 permit signature locally (ecrecover),
    and returns an honest pre-settle payment object.

    On-chain settlement is NOT executed here:

    * with a configured ``sponsor_impl`` (a real sponsor key) the batch is
      delegated to it and its tx_hash returned;
    * WITHOUT one, the scaffold reports ``scaffolded:needs_facilitator`` and
      ``apply_gas_batch`` raises ``NotImplementedError`` — so no caller can
      mistake the scaffold for a verified settlement or record a fabricated hash.
    """

    key: str  # network id (CAIP-2 or legacy)
    payment: dict[str, Any]
    sponsor_impl: Callable[..., Awaitable[str]] | None = field(default=None)

    def _resolve_flow(self) -> str:
        exts: Any = (self.payment or {}).get("extensions") or {}
        if not isinstance(exts, dict):
            return "none"
        if EIP2612_KEY in exts:
            return EIP2612_KEY
        if ERC20_APPROVAL_KEY in exts:
            return ERC20_APPROVAL_KEY
        return "none"

    async def resolve_sponsor_flow(self) -> str:
        """Pick the sponsor flow (FULL extension key) from the payment payload.

        Returns ``EIP2612_KEY``, ``ERC20_APPROVAL_KEY``, or ``"none"`` — the
        full extension keys, not short labels, so callers (and this module's
        own validation) can address the correct payload member. (This fixes a
        scaffold bug where the short flow label was used as the extension key,
        so the EIP-2612 branch silently never validated its own permit.)
        """
        return self._resolve_flow()

    def _sponsor_ready(self) -> bool:
        """True only when a real sponsor implementation (sponsor key) is set."""
        return self.sponsor_impl is not None

    async def prepare_payment(self) -> dict[str, Any]:
        """Validate the payload *and* verify the EIP-2612 permit signature.

        Honest: structure + validation + (EIP-2612) REAL ecrecover implemented.
        A forged/tampered permit fails closed here (raises ValueError). The
        returned payment carries NO fabricated tx_hash — that is only provided
        by a real ``sponsor_impl``. Status is ``needs_facilitator`` when no
        sponsor key is configured; the on-chain step is never claimed verified.
        """
        flow = self._resolve_flow()
        exts: Any = (self.payment or {}).get("extensions") or {}
        info = exts.get(flow, {}) if (isinstance(exts, dict) and flow != "none") else {}
        kind = (
            "eip2612"
            if flow == EIP2612_KEY
            else "erc20-approval"
            if flow == ERC20_APPROVAL_KEY
            else "none"
        )
        errors = _validate_permit2_approval_payload(info, flow) if flow != "none" else []
        if errors:
            raise ValueError(f"invalid {flow} gas-sponsoring payload: {'; '.join(errors)}")

        chain_id = _chain_id_for(self.key or "")
        sponsored = self._sponsor_ready()

        if flow == EIP2612_KEY:
            if chain_id is None:
                raise ValueError(f"no EVM chain id known for {self.key}")
            if not _eip2612_available():
                raise ValueError("eip2612 crypto unavailable: eth_keys not installed")
            ok, reason, owner = verify_permit_signature(info, chain_id=chain_id)
            if not ok:
                raise ValueError(f"failed EIP-2612 permit verification: {reason}")
            status = "ready" if sponsored else "needs_facilitator"
            return {
                "sponsor_flow": kind,
                "permit2_method": "settleWithPermit",
                "scaffolded": status,
                "signature_verified": True,  # EIP-2612 ecrecover confirmed the permit signature
                "verified": False,  # H1: payment NOT settled without a sponsor key
                "signer": owner,
                "permit_calldata": build_permit_calldata(info, chain_id=chain_id),
                "tx_hash": None,  # set by the on-chain sponsor only — never fabricated
            }

        # ERC-20 approval path: structure validated; the raw signed tx replay /
        # broadcast is the sponsor's job (no local ecrecover of a raw tx).
        status = "ready" if sponsored else "needs_facilitator"
        return {
            "sponsor_flow": kind,
            "permit2_method": "settle",
            "scaffolded": status,
            "tx_hash": None,  # set by the on-chain sponsor only — never fabricated
        }

    async def apply_gas_batch(self) -> str:
        """On-chain step — NEVER fabricated.

        With a configured ``sponsor_impl`` (a real sponsor key) the batch is
        delegated there and its returned tx_hash used. WITHOUT one, we raise
        so no caller can record a non-existent settlement; the honest status is
        ``scaffolded:needs_facilitator``.
        """
        if self.sponsor_impl is not None:
            return await self.sponsor_impl(self.payment)
        raise NotImplementedError(
            "gas sponsoring on-chain settlement is scaffolded:needs_facilitator "
            "(no sponsor_impl / sponsor key configured — nothing was broadcast)"
        )


def register_gas_sponsoring_facilitator(
    network: str, sponsor_impl: Callable[..., Awaitable[str]] | None = None
) -> GasSponsoringFacilitator:
    """Integrate gas sponsoring into the FacilitatorRouter.

    Wires a SponsoringFacilitator wrapper ``impl`` onto ``network`` via the
    router's ``register_facilitator``, exposing ``async verify(payment)`` and
    ``async settle(payment)`` (the contract documented on
    ``FacilitatorRouter.register_facilitator``).

    ``verify`` runs honest payload validation + (EIP-2612) local ecrecover;
    ``settle`` calls ``apply_gas_batch`` which delegates to a real sponsor or
    raises until one is configured. [REVIEW] The VerifyResult-as-verdict shape
    is the only facilitator contract exposed here; the extension spec does not
    itself define a verify step (assumption mapped onto the router contract).
    """

    async def _verify(payment: dict[str, Any]) -> VerifyResult:
        fac = GasSponsoringFacilitator(key=network, payment=payment, sponsor_impl=sponsor_impl)
        try:
            prepped = await fac.prepare_payment()
            # H1 fix: only verified=True when a real sponsor is configured
            # (scaffolded == "ready"). Otherwise the payment is NOT settled.
            is_settled = prepped.get("scaffolded") == "ready"
            return VerifyResult(
                verified=is_settled,
                reason=(
                    ""
                    if is_settled
                    else f"scaffolded:{prepped['scaffolded']} flow={prepped['sponsor_flow']}"
                ),
                asset=(payment or {}).get("asset") or "",
                network=network,
            )
        except ValueError as e:
            return VerifyResult(
                verified=False,
                reason=str(e),
                asset=(payment or {}).get("asset") or "",
                network=network,
            )

    async def _settle(payment: dict[str, Any]) -> str | None:
        fac = GasSponsoringFacilitator(key=network, payment=payment, sponsor_impl=sponsor_impl)
        return await fac.apply_gas_batch()  # delegates to sponsor or raises honestly

    class _Wrapper:
        async def verify(self, payment: dict[str, Any]) -> VerifyResult:
            return await _verify(payment)

        async def settle(self, payment: dict[str, Any]) -> str | None:
            return await _settle(payment)

    register_facilitator(network, _Wrapper())
    log.info("x402_gas_sponsoring_registered", extra={"network": network})
    return GasSponsoringFacilitator(key=network, payment={}, sponsor_impl=sponsor_impl)
