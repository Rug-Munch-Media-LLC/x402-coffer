"""Honesty-invariant tests for the a0fe50c fixes (settlement/verify honesty).

These assert BEHAVIOUR, not the shape of a specific refactor: they pin the
honest invariants that must hold on any tree where a settlement or
verification is NOT actually executed on-chain:

    C1/C2  ``FacilitatorRouter.settle`` NEVER reports ``success:True`` when no
           external facilitator settled AND no self-hosted settle key exists —
           it fails closed with ``success:False`` + ``scaffolded:True`` for
           EVM (C1) and Solana (C2). A previous version fabricated success via
           the EIP-7702 / Solana SPL RPC self-VERIFY path.
    H3     ``_verify_external`` does NOT treat a bare ``{"success":true}`` from
           a facilitator as proof of payment — ``verified:False`` unless an
           explicit ``isValid``/``verified`` signal is present.
    M2     ``_normalize_settlement`` does NOT fabricate success on any HTTP-200
           body — only an explicit settlement/verification signal yields
           ``success:True``.
    H2     ``ReplayStore.check_and_burn`` is atomic within a process — the
           same key returns True once, then False (no double-use window).
    H1     gas-sponsoring ``prepare_payment`` with a real, locally-verified
           EIP-2612 permit but NO sponsor key reports ``signature_verified:True``
           yet ``verified:False`` + ``scaffolded:needs_facilitator`` + a null
           ``tx_hash`` — the on-chain step is never claimed verified.

The existing ``StubFacilitator`` / ``set_facilitator_router`` injection pattern
is honored: the settle path (C1/C2) runs through a real ``FacilitatorRouter``
over ``httpx.AsyncClient``, and ``_verify_external`` (H3) hits a mocked
facilitator URL via ``respx``.
"""

from __future__ import annotations

import asyncio
import secrets
import sys
from typing import Any

import httpx
import pytest
import respx

# Same namespace fix as conftest: resolve THIS repo's app/vendor ahead of cwd.
_REPO_ROOT = __file__.rsplit("/", 2)[0]
for _p in (f"{_REPO_ROOT}/vendor", _REPO_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)


# ── settle() with no facilitator + no self-host settle key: C1/C2 ────────────


def _no_settler_router() -> Any:
    """A router with NO external facilitators and self-hosting disabled.

    ● external facilitators = [] (nothing can be POSTed to settle)
    ● self_hosted=False -> the EIP-7702/Solana-SPL self-VERIFY branch is NOT
      treated as a settler (that is exactly the C1/C2 honesty gap).
    """
    from coffer._engine.service import FacilitatorRouter

    return FacilitatorRouter(facilitators=[], self_hosted=False)


@pytest.mark.asyncio
async def test_settle_evm_no_settler_fails_closed_scaffolded() -> None:
    """C1: EVM settle with nobody to settle -> success:False + scaffolded:True.

    Regression: a previous build returned success:True via EIP-7702 self-VERIFY,
    fabricating a settlement record when nothing was broadcast.
    """
    router = _no_settler_router()
    res = await router.settle("pid_c1", "0xtx_evm", "eip155:8453", "USDC", 0.001)
    assert res["success"] is False, res
    assert res["scaffolded"] is True, res
    # Never a fabricated success transaction.
    assert "transaction" not in res or res.get("transaction") is None


@pytest.mark.asyncio
async def test_settle_solana_no_settler_fails_closed_scaffolded() -> None:
    """C2: Solana settle with nobody to settle -> success:False + scaffolded:True.

    Regression: a previous build returned success:True via the Solana SPL RPC
    self-VERIFY path — verifying a receipt is NOT broadcasting a settlement.
    """
    router = _no_settler_router()
    res = await router.settle("pid_c2", "0xtx_sol", "solana:mainnet", "USDC", 0.001)
    assert res["success"] is False, res
    assert res["scaffolded"] is True, res
    assert "transaction" not in res or res.get("transaction") is None


@pytest.mark.asyncio
async def test_settle_no_settler_never_reports_success_true() -> None:
    """C1+C2 umbrella: success:True is simply impossible without a settler.

    Directly asserts the invariant across both network families the trajectory
    must never regress to.
    """
    router = _no_settler_router()
    for network in ("eip155:8453", "solana:mainnet"):
        res = await router.settle(f"pid_{network.split(':')[0]}", "0xtx", network, "USDC", 0.001)
        assert res["success"] is not True, f"{network} reported fake success: {res}"


# ── _verify_external with a bare {"success":true}: H3 ────────────────────────


@pytest.mark.asyncio
async def test_verify_external_bare_success_not_verified() -> None:
    """H3: a facilitator returning only {"success":true} is NOT proof of payment.

    Requires an explicit verification field (isValid/verified). Regression: a
    previous build accepted ``data.get("success")`` as a verification signal.
    """
    from coffer._engine.external_verify import verify_external
    from coffer._engine.facilitator_config import FacilitatorConfig
    from coffer._engine.facilitator_router import FacilitatorRouter

    cfg = FacilitatorConfig(
        name="stub-bare-success",
        verify_url="https://stub.example/verify",
        settle_url="https://stub.example/settle",
    )
    router = FacilitatorRouter(facilitators=[cfg], self_hosted=False)
    with respx.mock:
        respx.post("https://stub.example/verify").mock(
            return_value=httpx.Response(200, json={"success": True})
        )
        res = await verify_external(router, "pid_h3", "0xtx_h3", "eip155:8453", "USDC", 0.001)
    assert res["verified"] is False, res
    assert res["settlement"] is None, res


# ── _normalize_settlement: M2 ────────────────────────────────────────────────


def _normalize(data: dict[str, Any]) -> dict[str, Any]:
    from coffer._engine.external_verify import normalize_settlement

    return normalize_settlement(data, "pid_m2", "0xtx_m2", "eip155:8453", "stub")


def test_normalize_settlement_empty_body_is_not_success() -> None:
    """M2: an HTTP-200 with an empty body is NOT a settlement."""
    res = _normalize({})
    assert res["success"] is False
    assert res["transaction"] == "0xtx_m2"  # passthrough, never claimed settled


def test_normalize_settlement_ok_only_is_not_success() -> None:
    """M2: a bare {"ok":1} body carries no settlement signal -> success:False."""
    res = _normalize({"ok": 1})
    assert res["success"] is False


def test_normalize_settlement_txhash_is_success() -> None:
    """M2: an explicit transactionHash IS a genuine settlement signal."""
    res = _normalize({"transactionHash": "0xbeef"})
    assert res["success"] is True
    assert res["transaction"] == "0xbeef"


# ── ReplayStore.check_and_burn atomicity: H2 ────────────────────────────────


def test_replay_store_same_key_burns_once() -> None:
    """H2: check_and_burn is atomic — first call True, second False.

    Regression: the in-memory fallback previously did a non-atomic check-then-add
    that a concurrent re-use could slip through. Same-key double spend must be
    blocked on the second burn regardless of interleaving.
    """
    from coffer._engine.service import ReplayStore

    store = ReplayStore()
    key = "x402:test:h2:atomic"
    assert asyncio.run(store.check_and_burn(key)) is True
    assert asyncio.run(store.check_and_burn(key)) is False
    # non-destructive check still sees it as used
    assert asyncio.run(store.check(key)) is True


# ── gas-sponsoring prepare_payment without sponsor key: H1 ──────────────────


def _valid_permit_payload() -> dict[str, Any]:
    """Craft a GENUINE EIP-2612 permit signed by a fresh local key."""
    from eth_keys import keys as ek

    from coffer._engine.extensions.gas_sponsoring import (
        PERMIT2_ADDRESS,
        eip2612_digest,
    )

    priv = ek.PrivateKey(secrets.token_bytes(32))
    owner = priv.public_key.to_checksum_address()
    token = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"  # Base USDC
    value, nonce, deadline = 1000, 0, 9999999999
    digest = eip2612_digest(
        owner=owner,
        spender=PERMIT2_ADDRESS,
        value=value,
        nonce=nonce,
        deadline=deadline,
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
        "amount": str(value),
        "nonce": str(nonce),
        "deadline": str(deadline),
        "signature": "0x" + sig.hex(),
    }


@pytest.mark.asyncio
async def test_gas_sponsoring_no_sponsor_honest_status() -> None:
    """H1: a real EIP-2612 permit without a sponsor key is signature-verified
    but NOT surrendered as an on-chain settlement.

    Asserts signature_verified:True (ecrecover OK) yet verified:False,
    scaffolded:needs_facilitator, and tx_hash is None — the honest status for
    the permit the scaffold authenticated but did not (and cannot) broadcast.
    """
    from coffer._engine.extensions.gas_sponsoring import (
        EIP2612_KEY,
        GasSponsoringFacilitator,
    )

    fac = GasSponsoringFacilitator(
        key="eip155:8453",
        payment={"extensions": {EIP2612_KEY: _valid_permit_payload()}},
    )
    prepped = await fac.prepare_payment()
    assert prepped["signature_verified"] is True, prepped
    assert prepped["verified"] is False, prepped  # H1: not settled w/o sponsor key
    assert prepped["scaffolded"] == "needs_facilitator", prepped
    assert prepped["tx_hash"] is None, prepped


@pytest.mark.asyncio
async def test_gas_sponsoring_erc20_no_sponsor_never_tx_hash() -> None:
    """H1 (ERC-20 approval variant): without a sponsor key, the on-chain step
    is scaffolded and carries no fabricated tx_hash.
    """
    from coffer._engine.extensions.gas_sponsoring import (
        ERC20_APPROVAL_KEY,
        GasSponsoringFacilitator,
    )

    fac = GasSponsoringFacilitator(
        key="eip155:8453",
        payment={
            "extensions": {
                ERC20_APPROVAL_KEY: {
                    "token": "0xUSDC",
                    "signedTransaction": {
                        # Validation requires the approval target to be (or
                        # mention) the canonical Permit2 contract by name.
                        "to": "0xPermit2CanonicalPermissionGateway",
                        "from": "0xme",
                        "value": "0x0",
                    },
                }
            }
        },
    )
    prepped = await fac.prepare_payment()
    assert prepped["scaffolded"] == "needs_facilitator"
    assert prepped["tx_hash"] is None
