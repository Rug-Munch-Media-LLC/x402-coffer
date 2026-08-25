"""Coffer facade (public API) + extensions lifecycle.

The public surface a user actually imports is ``coffer`` — one facade that
proxies the domain, the vendored spec and the optional extensions. These tests
assert that facade RESOLVES and BEHAVES, not just that it imports:

* ``coffer.require_payment`` / ``coffer.build_payment_required`` /
  ``coffer.get_facilitator_router`` are the real objects a user calls, and a
  powered call produces a real spec ``{x402Version, accepts[]}`` body.
* Extension lifecycle is honest and deterministic:
  - payment-identifier: same id + same fingerprint is idempotent; same id +
    a CONTRASTING fingerprint yields a 409 ``conflict`` BEFORE any payment.
  - gas-sponsoring: a real locally-signed EIP-2612 permit reports
    ``signature_verified:true`` yet ``verified:false`` + ``needs_facilitator``
    (no sponsor key -> nothing broadcast, no fabricated tx_hash).
  - upto / batch-settlement: structured offers are ``scaffolded:needs_facilitator``
    and their on-chain settle/claim RAISE rather than fake a settlement.
"""

from __future__ import annotations

import secrets
from typing import Any

import pytest

# conftest already fixes sys.path so `coffer` resolves to THIS repo's facade.


def _require_coffer_module() -> Any:
    import coffer

    return coffer


def test_coffer_facade_exports_payment_api():
    """The public facade re-exports the payment API a user calls."""
    coffer = _require_coffer_module()
    for name in ("require_payment", "build_payment_required", "get_facilitator_router"):
        assert callable(getattr(coffer, name)), f"coffer.{name} not callable"
    # Data tables users read at startup.
    assert coffer.SUPPORTED_ASSETS
    assert coffer.VERIFICATION_STATUS["evm"] == "live"


def test_coffer_build_payment_required_produces_real_offer():
    """coffer.build_payment_required('api_access') builds a spec body with a
    valid, client-readable accepts[] surface (not a mock/placeholder)."""
    coffer = _require_coffer_module()
    body = coffer.build_payment_required("api_access")
    assert body["x402Version"] == 1
    accepts = body["accepts"]
    assert accepts
    for e in accepts:
        assert e["scheme"] in ("exact", "upto")
        assert e["network"].count(":") == 1
        assert e["maxAmountRequired"].isdigit()
        assert e["payToAddress"].strip()
        assert e["asset"]


def test_coffer_get_facilitator_router_matches_domain():
    """coffer.get_facilitator_router() returns the process-wide FacilitatorRouter
    (same singleton the /v1/x402 endpoints use)."""
    coffer = _require_coffer_module()
    router = coffer.get_facilitator_router()
    from coffer._engine.service import get_facilitator_router as domain_get

    assert router is domain_get()
    assert callable(router.verify) and callable(router.settle)


# ── extensions lifecycle: payment-identifier ─────────────────────────────────


def test_payment_identifier_same_id_different_fingerprint_conflicts():
    """Idempotency: first use of an id is accepted and bonded to its fingerprint;
    reusing the same id for a DIFFERENT request must yield a 409-style conflict
    BEFORE a second payment can be taken."""
    from coffer._engine.extensions.payment_identifier import PaymentIdentifierStore

    store = PaymentIdentifierStore()
    pid = "pay_" + "a" * 30
    fp_a = store.fingerprint(
        {"scheme": "exact", "network": "eip155:8453", "asset": "USDC", "amount": "0.01"}
    )
    fp_b = store.fingerprint(
        {"scheme": "exact", "network": "eip155:8453", "asset": "USDC", "amount": "99.00"}
    )

    first = pytest_asyncio_run(store.check_before_payment(pid, "caller", fp_a))
    assert first.state == "accepted", first

    replay = pytest_asyncio_run(store.check_before_payment(pid, "caller", fp_b))
    assert replay.state == "conflict", replay
    assert pid == replay.payment_id


def test_payment_identifier_same_id_same_fingerprint_cached():
    """same id + same fingerprint is idempotent (cached within TTL, not a 409)."""
    from coffer._engine.extensions.payment_identifier import PaymentIdentifierStore

    store = PaymentIdentifierStore()
    pid = "pay_" + "b" * 30
    fp = store.fingerprint({"scheme": "exact", "network": "solana:mainnet", "asset": "USDC"})
    assert pytest_asyncio_run(store.check_before_payment(pid, "caller", fp)).state == "accepted"
    assert pytest_asyncio_run(store.check_before_payment(pid, "caller", fp)).state == "cached"


# ── extensions lifecycle: gas-sponsoring (real EIP-2612) ─────────────────────


def _sign_permit() -> dict[str, Any]:
    """A GENUINE EIP-2612 permit signed by a fresh local key (Base USDC)."""
    from eth_keys import keys as ek

    from coffer._engine.extensions.gas_sponsoring import PERMIT2_ADDRESS, eip2612_digest

    priv = ek.PrivateKey(secrets.token_bytes(32))
    owner = priv.public_key.to_checksum_address()
    token = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"  # Base USDC
    digest = eip2612_digest(
        owner=owner,
        spender=PERMIT2_ADDRESS,
        value=1000,
        nonce=0,
        deadline=9_999_999_999,
        chain_id=8453,
        token_address=token,
        name="USD Coin",
        version="1",
    )
    sig = priv.sign_msg_hash(digest).to_bytes()
    return {
        "token": token,
        "owner": owner,
        "spender": PERMIT2_ADDRESS,
        "amount": "1000",
        "nonce": "0",
        "deadline": "9999999999",
        "signature": "0x" + sig.hex(),
    }


@pytest.mark.asyncio
async def test_gas_sponsoring_valid_permit_is_signature_verified_not_settled():
    """A real EIP-2612 permit WITHOUT a sponsor key is cryptographically verified
    (signature_verified:true) but the on-chain step is NOT claimed (verified:false,
    scaffolded:needs_facilitator, tx_hash null)."""
    from coffer._engine.extensions.gas_sponsoring import EIP2612_KEY, GasSponsoringFacilitator

    fac = GasSponsoringFacilitator(
        key="eip155:8453",
        payment={"extensions": {EIP2612_KEY: _sign_permit()}},
    )
    prepped = await fac.prepare_payment()
    assert prepped["signature_verified"] is True
    assert prepped["verified"] is False
    assert prepped["scaffolded"] == "needs_facilitator"
    assert prepped["tx_hash"] is None


@pytest.mark.asyncio
async def test_gas_sponsoring_forged_permit_fails_closed():
    """A tampered/forged permit is rejected BEFORE broadcast (never 'verified')."""
    from coffer._engine.extensions.gas_sponsoring import EIP2612_KEY, GasSponsoringFacilitator

    bad = _sign_permit()
    bad["signature"] = "0x" + ("00" * 65)
    fac = GasSponsoringFacilitator(
        key="eip155:8453",
        payment={"extensions": {EIP2612_KEY: bad}},
    )
    with pytest.raises(ValueError):
        await fac.prepare_payment()


# ── extensions lifecycle: upto / batch-settlement ────────────────────────────


def test_upto_option_is_scaffolded_and_settle_raises():
    from coffer._engine.extensions.schemes import UpToScheme

    scheme = UpToScheme(network="eip155:8453")
    option = scheme.payment_option("r", "d", "0xPayTo")
    assert option["extra"]["scaffolded"] == "needs_facilitator"
    with pytest.raises(NotImplementedError):
        pytest_asyncio_run(lambda: scheme.settle({}, override="10%"))


def test_batch_settlement_option_scaffolded_and_claim_sets_raise():
    from coffer._engine.extensions.schemes import BatchSettlementScheme

    scheme = BatchSettlementScheme(receiver_address="0xRCV")
    option = scheme.open_channel_option("r", "d", "0xPayTo")
    assert option["extra"]["scaffolded"] == "needs_facilitator"
    assert scheme.verify_voucher({"channelAddress": "0xchan", "totalClaimable": 5}) is True
    assert scheme.verify_voucher({"channelAddress": "0xchan"}) is False  # missing total
    with pytest.raises(NotImplementedError):
        pytest_asyncio_run(scheme.claim_and_settle)


def test_build_scheme_factory_resolves_both_schemes():
    from coffer._engine.extensions.schemes import build_scheme

    assert build_scheme("upto").scheme == "upto"
    assert build_scheme("batch-settlement").scheme == "batch-settlement"
    with pytest.raises(ValueError):
        build_scheme("does-not-exist")


def pytest_asyncio_run(value: Any) -> Any:
    """Run an async coroutine (or a callable returning one) synchronously,
    since these extension-lifecycle tests are sync for deterministic state."""
    import asyncio
    import inspect

    result: dict[str, Any] = {}

    async def _go() -> None:
        coro = await value if inspect.isawaitable(value) else value()
        if inspect.isawaitable(coro):
            coro = await coro
        result["v"] = coro

    asyncio.run(_go())
    return result.get("v")
