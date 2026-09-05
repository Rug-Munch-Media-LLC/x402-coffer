"""Settlement / verification response models for the x402 domain.

Covers the v1 spec §5.3 SettlementResponse (fleet contract §5.2) and the
facilitator ``verify`` result (fleet contract §5.1).

Notes for other agents
----------------------
- ``network`` fields accept the known legacy bare names ("base", "solana",
  ...) and normalise them to CAIP-2; anything else must already be valid
  CAIP-2 (``namespace:reference``) or construction fails.
- The pre-spec ``PaymentReceipt`` survives only as an alias of
  :class:`PaymentResponse` so legacy imports keep working.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .assets import _require_caip2, _require_caip2_or_none


class PaymentResponse(BaseModel):
    """Settlement receipt (v1 spec §5.3 SettlementResponse + contract §5.2).

    Extends the spec core ``{success, transaction, network, payer}`` with the
    fleet facilitator metadata (``payment_id``, ``facilitator``, ``settled_at``,
    block confirmation detail). ``transaction`` is the broadcast tx hash and
    MUST be non-empty whenever settlement was broadcast.
    """

    model_config = ConfigDict(str_strip_whitespace=True, populate_by_name=True)

    success: bool
    transaction: str = Field(description="Blockchain transaction hash ('' only if nothing was broadcast)")
    network: str = Field(description="CAIP-2 network id")
    payment_id: str = ""
    facilitator: str = ""
    settled_at: str = Field(default="", description="ISO-8601 UTC settlement timestamp")
    payer: str | None = Field(default=None, description="Payer wallet address")
    error_reason: str | None = Field(
        default=None,
        alias="errorReason",
        description="Spec error code when settlement failed",
    )
    block_number: int | None = None
    block_hash: str | None = None
    confirmations: int | None = None

    @field_validator("network", mode="before")
    @classmethod
    def _network_caip2(cls, value: Any) -> str:
        return _require_caip2(value)


#: Backward-compatible alias: the pre-spec ``PaymentReceipt`` name now points
#: at the spec-conformant settlement receipt. Prefer ``PaymentResponse``.
PaymentReceipt = PaymentResponse


class VerifyResult(BaseModel):
    """Facilitator verification result (fleet contract §5.1 ``verify``).

    ``reason`` is always present (empty when verified); ``settlement`` carries
    the settlement detail dict when the facilitator returned one.
    """

    model_config = ConfigDict(str_strip_whitespace=True, populate_by_name=True)

    verified: bool
    reason: str = ""
    settlement: dict[str, Any] | None = None
    facilitator: str = ""
    signer: str | None = None
    amount_atomic: int | None = None
    network: str | None = Field(default=None, description="CAIP-2 network id")
    asset: str | None = None

    @field_validator("network", mode="before")
    @classmethod
    def _network_caip2(cls, value: Any) -> str | None:
        return _require_caip2_or_none(value)
