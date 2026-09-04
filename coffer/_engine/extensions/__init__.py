"""Official x402 extensions wired into the FacilitatorRouter.

Composable, OPTIONAL capabilities (spec: ``docs/x402-official-spec/
extensions__overview.md``) layered on top of the core exact/Permit2 flow to
maximize payment options:

* ``payment_identifier`` — idempotency (``extensions__payment-identifier.md``)
* ``gas_sponsoring``     — EIP-2612 + ERC-20 gasless Permit2 approval
  (``extensions__eip2612-gas-sponsoring.md`` + ``extensions__erc20-approval-gas-sponsoring.md``)
* ``schemes``            — ``upto`` + ``batch-settlement``
  (``schemes__upto.md`` + ``schemes__batch-settlement.md``)

All on-chain settlement steps are marked ``scaffolded:needs_facilitator`` and
never claim to be verified; a real facilitator must supply the on-chain half.
"""

from __future__ import annotations

from typing import Any

from coffer._engine.extensions.gas_sponsoring import (
    EIP2612_KEY,
    ERC20_APPROVAL_KEY,
    MAX_UINT256,
    GasSponsoringFacilitator,
    declare_eip2612_gas_sponsoring_extension,
    declare_erc20_approval_gas_sponsoring_extension,
    register_gas_sponsoring_facilitator,
)
from coffer._engine.extensions.payment_identifier import (
    DEFAULT_IDEMPOTENCY_TTL,
    PAYMENT_ID_MAX_LENGTH,
    PAYMENT_ID_MIN_LENGTH,
    PAYMENT_ID_PATTERN,
    PAYMENT_IDENTIFIER,
    PaymentIdentifierStore,
    PaymentIdReplay,
    append_payment_identifier_to_extensions,
    declare_payment_identifier_extension,
    extract_payment_identifier,
    generate_payment_id,
    is_valid_payment_id,
    validate_payment_identifier,
)
from coffer._engine.extensions.schemes import (
    DEFAULT_DEPOSIT_MULTIPLIER,
    MIN_DEPOSIT_MULTIPLIER,
    SCHEME_BATCH_SETTLEMENT,
    SCHEME_FACTORIES,
    SCHEME_UPTO,
    BatchSettlementScheme,
    UpToScheme,
    UpToSettlement,
    build_scheme,
)


def register_all_extensions(router: Any = None, *, gas_sponsor_network: str | None = None) -> dict[str, Any]:
    """Register every extension entry point on the given FacilitatorRouter.

    ``router`` is a :class:`FacilitatorRouter` (the global router is used when
    omitted). Returns a dict of the registered extension keys.

    Logistic notes:
    * PaymentIdentifier produces a caller-bound, TTL-backed idempotency store
      ready for callers to use; it does not need a route to be a value-add.
    * Gas sponsoring is registered per-network via ``register_facilitator``
      (the router's only facilitator seam). It is only wired when a target
      network is supplied.
    * The scheme stubs are structural; on-chain settlement always needs a
      real facilitator (``scaffolded:needs_facilitator``).
    """
    from coffer._engine.service import register_facilitator as _rf

    if router is not None:
        _wire_gas = router.register_facilitator
    else:
        _wire_gas = _rf

    from coffer._engine.extensions.bazaar import get_bazaar_catalog

    registered: dict[str, Any] = {
        "payment_identifier": PaymentIdentifierStore(),
        "bazaar": get_bazaar_catalog(),
        "schemes": {
            "upto": UpToScheme,
            "batch-settlement": BatchSettlementScheme,
        },
    }

    if gas_sponsor_network is not None:
        # Integrate via the router's register_facilitator seam.
        facade = register_gas_sponsoring_facilitator(gas_sponsor_network)
        registered["gas_sponsoring"] = {
            "network": gas_sponsor_network,
            EIP2612_KEY: declare_eip2612_gas_sponsoring_extension(),
            ERC20_APPROVAL_KEY: declare_erc20_approval_gas_sponsoring_extension(),
            "facilitator": facade,
        }

    return registered


__all__ = [
    "DEFAULT_DEPOSIT_MULTIPLIER",
    "DEFAULT_IDEMPOTENCY_TTL",
    "EIP2612_KEY",
    "ERC20_APPROVAL_KEY",
    "MAX_UINT256",
    "MIN_DEPOSIT_MULTIPLIER",
    "PAYMENT_IDENTIFIER",
    "PAYMENT_ID_MAX_LENGTH",
    "PAYMENT_ID_MIN_LENGTH",
    "PAYMENT_ID_PATTERN",
    "SCHEME_BATCH_SETTLEMENT",
    "SCHEME_FACTORIES",
    "SCHEME_UPTO",
    "BatchSettlementScheme",
    "GasSponsoringFacilitator",
    "PaymentIdReplay",
    "PaymentIdentifierStore",
    "UpToScheme",
    "UpToSettlement",
    "append_payment_identifier_to_extensions",
    "build_scheme",
    "declare_eip2612_gas_sponsoring_extension",
    "declare_erc20_approval_gas_sponsoring_extension",
    "declare_payment_identifier_extension",
    "extract_payment_identifier",
    "generate_payment_id",
    "is_valid_payment_id",
    "register_all_extensions",
    "register_gas_sponsoring_facilitator",
    "validate_payment_identifier",
]
