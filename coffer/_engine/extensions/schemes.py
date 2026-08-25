"""Additional x402 payment schemes — ``upto`` + ``batch-settlement``.

Implements, as HONEST stubs, the two schemes in the official spec that the
core exact/Permit2 flow does not cover:

* ``schemes__upto.md`` — usage-based, max-authorization payments. Buyer signs
  once for a maximum; seller set_settlement_overrides()s the actual amount
  (raw atomic units | ``"50%"`` | ``"$0.05"``). On EVM it uses Permit2 with a
  facilitator-advertised ``facilitatorAddress``.
* ``schemes__batch-settlement.md`` — escrow / cumulative voucher / batched
  on-chain redemption (stateless unidirectional payment channels).

Both are implemented at the STRUCTURE + PRICING + ASSET-VALIDATION level.
On-chain settlement is marked ``scaffolded:needs_facilitator`` and is never
claimed to be verified — the facilitator must supply the real deposit /
voucher-claim / redeem machinery.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

from coffer._engine.models import Scheme

log = logging.getLogger(__name__)

SCHEME_UPTO = Scheme.UPTO  # "upto" — already present on the Scheme enum
SCHEME_BATCH_SETTLEMENT = "batch-settlement"  # wire value (not yet on enum)

# batch-settlement constants (spec §Deposit Policy / §Facilitator Setup)
DEFAULT_DEPOSIT_MULTIPLIER = 5
MIN_DEPOSIT_MULTIPLIER = 3

_ASSET_ALIASES: dict[str, dict[str, str | int]] = {
    "usdc": {"name": "USDC", "decimals": 6},
    "usdt": {"name": "USDT", "decimals": 6},
    "dai": {"name": "DAI", "decimals": 18},
    "weth": {"name": "WETH", "decimals": 18},
}


# ── UPTO (usage-based, max-authorization) ────────────────────────────────────


@dataclass
class UpToSettlement:
    """Authorized-maximum settlement override (spec §Settlement Override)."""

    max_amount_atomic: int
    max_price_usd: Decimal
    override: str  # "50000" | "50%" | "$0.05" — raw | percentage | dollar
    asset: str

    @property
    def scale_factor(self) -> Decimal:
        """Fraction of the route maximum to settle (0..1).

        * ``"50000"`` raw atomic units -> exact fraction of ``max_amount_atomic``
        * ``"50%"`` -> 0.5
        * ``"$0.05"`` -> dollar price converted against route max
        """
        o = (self.override or "").strip()
        try:
            if o.endswith("%"):
                pct = Decimal(o[:-1])
                return max(Decimal(0), min(Decimal(1), pct / Decimal(100)))
            if o.startswith("$"):
                return Decimal(o[1:]) / self.max_price_usd if self.max_price_usd else Decimal(0)
            raw = int(o)
            if raw == 0:
                return Decimal(0)  # "0" => no charge (spec: still may land a close tx on SVM)
            if not self.max_amount_atomic:
                return Decimal(0)
            return Decimal(raw) / Decimal(self.max_amount_atomic)
        except (InvalidOperation, ValueError, ZeroDivisionError):
            return Decimal(1)  # unparseable -> settle the full authorized max

    @property
    def settled_atomic(self) -> int:
        return int((self.scale_factor * Decimal(self.max_amount_atomic)).to_integral_value())


@dataclass
class UpToScheme:
    """Structure + pricing + asset validation for the ``upto`` scheme.

    Honest-scaffold: buyer authorizes ``max_amount_atomic``; the server later
    ``settle()``s the actual ``override`` amount (usage metered at handler
    time). On-chain Permit2 settlement is ``scaffolded:needs_facilitator``.

    EVM specifics (spec §EVM Implementation): settlement amount is unknown at
    signing time, so the facilitator advertises a ``facilitatorAddress`` to
    which the buyer binds the authorization.
    """

    scheme: str = SCHEME_UPTO
    network: str = "eip155:8453"
    max_price_usd: Decimal = field(default=Decimal("0.10"))
    asset: str = "usdc"
    facilitator_address: str = field(default="")  # spec: binds the authorization

    @property
    def asset_meta(self) -> dict[str, str | int]:
        return _ASSET_ALIASES.get(self.asset.lower(), {"name": self.asset, "decimals": 18})

    def payment_option(self, resource: str, description: str, pay_to: str) -> dict[str, Any]:
        """Build an ``accepts[]`` option without on-chain interaction."""
        meta = self.asset_meta
        return {
            "scheme": self.scheme,
            "network": self.network,
            "maxAmountRequired": str(self.max_amount_atomic_units()),
            "resource": resource,
            "description": description,
            "payToAddress": pay_to,
            "asset": meta["name"],
            "extra": {
                "facilitatorAddress": self.facilitator_address,
                "usageBilled": True,
                "scaffolded": "needs_facilitator",
            },
        }

    def max_amount_atomic_units(self) -> int:
        decimals = int(self.asset_meta["decimals"])
        return int(self.max_price_usd * Decimal(10**decimals))

    def authorize(self, max_amount_atomic: int) -> dict[str, Any]:
        """Buyer-side: sign an authorization for the max (no on-chain yet)."""
        return {
            "scheme": self.scheme,
            "maxAmountAuthorized": str(max_amount_atomic),
            "boundToFacilitator": self.facilitator_address,
            "scaffolded": "needs_facilitator",
        }

    async def settle(self, payment: dict[str, Any], override: str) -> str | None:
        """Usage settlement — ON-CHAIN STEP NOT VERIFIED by this scaffold.

        Raises so no caller can fabricate a settlement tx_hash. A real
        facilitator must permitWithAuthorization the settled amount + rebind
        the remaining authorization.
        """
        raise NotImplementedError(
            "upto on-chain settlement is scaffolded:needs_facilitator "
            "(settle the actual usage amount via the facilitator)"
        )


# ── BATCH-SETTLEMENT (escrow / vouchers / batched redemption) ────────────────


@dataclass
class BatchSettlementScheme:
    """Structure + storage-config + deposit policy for batch settlement.

    Spec: buyer deposits funds into onchain escrow once (PerMIT2 or EIP-3009,
    submitted by the facilitator), signs off-chain cumulative vouchers per
    request; server verifies vouchers quickly, redeems onchain later in
    batches (claim -> settle -> refund).

    On-chain claim/settle/refund is ``scaffolded:needs_facilitator``.
    ``receiver_authorizer`` follows spec §Receiver Authorizer (self-managed or
    facilitator-delegated).
    """

    scheme: str = SCHEME_BATCH_SETTLEMENT
    network: str = "eip155:8453"
    receiver_address: str = ""
    deposit_multiplier: int = DEFAULT_DEPOSIT_MULTIPLIER
    receiver_authorizer: str = field(default="")  # empty => facilitator-delegated
    claim_interval_secs: int = 60
    settle_interval_secs: int = 300
    refund_interval_secs: int = 3600
    max_claims_per_batch: int = 100

    def __post_init__(self) -> None:
        if self.deposit_multiplier < MIN_DEPOSIT_MULTIPLIER:
            raise ValueError(f"deposit_multiplier must be >= {MIN_DEPOSIT_MULTIPLIER} (spec min 3)")

    def validate_asset(self, asset: str) -> None:
        """Reject unsupported assets before any channel is touched."""
        if asset.lower() not in _ASSET_ALIASES:
            raise ValueError(f"unsupported asset for batch-settlement: {asset!r}")

    def deposit_amount(self, per_request_max_atomic: int) -> int:
        """Client deposit = per-request max x multiplier (spec §Deposit Policy)."""
        return int(per_request_max_atomic) * self.deposit_multiplier

    def open_channel_option(self, resource: str, description: str, pay_to: str) -> dict[str, Any]:
        """Build an ``accepts[]`` option (no on-chain yet)."""
        return {
            "scheme": self.scheme,
            "network": self.network,
            "price": "$0.01",  # per-request maximum
            "resource": resource,
            "description": description,
            "payToAddress": pay_to,
            "asset": "USDC",
            "extra": {
                "escrow": True,
                "depositMultiplier": self.deposit_multiplier,
                "receiverAuthorizer": self.receiver_authorizer or "<facilitator-delegated>",
                "scaffolded": "needs_facilitator",
            },
        }

    def verify_voucher(self, voucher: dict[str, Any]) -> bool:
        """Honest voucher-shape validation (spec §Voucher / §Receiver Authorizer).

        Asserts the cumulative voucher carries a signed channel + total. Real
        on-chain verification is the facilitator's job.
        """
        return bool(
            isinstance(voucher, dict)
            and voucher.get("channelAddress")
            and voucher.get("totalClaimable") is not None
        )

    async def claim_and_settle(self) -> str | None:
        """Claim latest vouchers + sweep to receiver — ON-CHAIN, NOT VERIFIED."""
        raise NotImplementedError(
            "batch-settlement claim/settle/refund is scaffolded:needs_facilitator "
            "(run a channel manager with a real facilitator)"
        )

    def channel_manager(self) -> dict[str, Any]:
        """Stub of server-side channel manager config (spec §Settlement Policy)."""
        return {
            "claimIntervalSecs": self.claim_interval_secs,
            "settleIntervalSecs": self.settle_interval_secs,
            "refundIntervalSecs": self.refund_interval_secs,
            "maxClaimsPerBatch": self.max_claims_per_batch,
            "scaffolded": "needs_facilitator",
        }


# ── scheme registry ──────────────────────────────────────────────────────────


SCHEME_FACTORIES = {
    SCHEME_UPTO: lambda **kw: UpToScheme(**kw),
    SCHEME_BATCH_SETTLEMENT: lambda **kw: BatchSettlementScheme(**kw),
}


def build_scheme(name: str, **kwargs: Any) -> Any:
    """Factory for the additional schemes (structure/pricing/validation only)."""
    try:
        return SCHEME_FACTORIES[name](**kwargs)
    except KeyError as e:
        raise ValueError(f"unknown scheme stub: {name!r}") from e


__all__ = [
    "DEFAULT_DEPOSIT_MULTIPLIER",
    "MIN_DEPOSIT_MULTIPLIER",
    "SCHEME_BATCH_SETTLEMENT",
    "SCHEME_FACTORIES",
    "SCHEME_UPTO",
    "BatchSettlementScheme",
    "UpToScheme",
    "UpToSettlement",
    "build_scheme",
]
