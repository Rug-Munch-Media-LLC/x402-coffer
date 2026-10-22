"""Pry-parity layer tests: wire headers, attestation, budgets, usage ledger,
and a local facilitator double driving verify -> settle -> replay -> underpay.

Everything runs through Coffer's own seams (``FacilitatorRouter``,
``register_facilitator``, ``X402Handler``). Only the chain is replaced.
"""

from __future__ import annotations

import asyncio
import time

import pytest
from facilitator_fake import USDC_BASE_SEPOLIA, FakeFacilitator

from coffer._engine.attestation import (
    ATTEST_MAX_AGE_SECONDS,
    attest_payment_required,
    verify_payment_required,
)
from coffer._engine.budgets import BudgetExceeded, BudgetPolicy, KeyBudgetLedger
from coffer._engine.facilitator_router import FacilitatorRouter
from coffer._engine.payment_headers import (
    HEADER_PAYMENT_SIGNATURE,
    HEADER_X_PAYMENT,
    PaymentHeaderError,
    decode_payment_header,
    decode_settlement_header,
    has_payment_header,
    settlement_header_pairs,
)
from coffer._engine.usage_ledger import read_usage, record_usage, summarize_usage

PAYER_KEY = "0x" + "33" * 32
PAY_TO = "0x" + "44" * 20
NETWORK = "eip155:84532"
REQUIRED_ATOMIC = 10_000  # 0.01 USDC (6 decimals)


def _b64(payload: dict) -> str:
    import base64
    import json

    return base64.b64encode(json.dumps(payload).encode()).decode()


def _authorization(*, value: int = REQUIRED_ATOMIC, valid_for: int = 300) -> dict:
    from eth_account import Account

    account = Account.from_key(PAYER_KEY)
    now = int(time.time())
    auth = {
        "from": account.address,
        "to": PAY_TO,
        "value": value,
        "validAfter": 0,
        "validBefore": now + valid_for,
        "nonce": "0x" + "ab" * 32,
    }
    from coffer._engine.self_facilitator import SelfFacilitator

    digest = SelfFacilitator(pay_to=PAY_TO)._digest(auth, 84532, USDC_BASE_SEPOLIA)
    signed = account.unsafe_sign_hash(digest)
    signature = getattr(signed, "signature", signed)
    auth["signature"] = "0x" + (signature.hex() if isinstance(signature, bytes) else str(signature))
    auth["valid_after"] = auth["validAfter"]
    auth["valid_before"] = auth["validBefore"]
    return auth


# ── wire headers ─────────────────────────────────────────────────────────────
def test_payment_header_prefers_v2_then_legacy_alias() -> None:
    payload = {"x402Version": 2, "scheme": "exact"}
    assert decode_payment_header({HEADER_PAYMENT_SIGNATURE: _b64(payload)}) == payload
    assert decode_payment_header({HEADER_X_PAYMENT: _b64(payload)}) == payload
    # Case-insensitive, as ASGI/WSGI headers arrive lowercased.
    assert decode_payment_header({"x-payment": _b64(payload)}) == payload
    assert has_payment_header({"Payment-Signature": _b64(payload)}) is True


@pytest.mark.parametrize("headers", [{}, {"X-PAYMENT": "not-base64!!"}, {"X-PAYMENT": _b64(["list"])}])
def test_payment_header_rejects_missing_or_malformed(headers: dict) -> None:
    with pytest.raises(PaymentHeaderError) as exc:
        decode_payment_header(headers)
    assert exc.value.status_code == 402


def test_settlement_headers_echo_identical_receipt_under_both_names() -> None:
    receipt = {"success": True, "txHash": "0xdead", "network": NETWORK}
    headers = settlement_header_pairs(receipt)
    assert headers["PAYMENT-RESPONSE"] == headers["X-PAYMENT-RESPONSE"]
    assert decode_settlement_header({"x-payment-response": headers["X-PAYMENT-RESPONSE"]}) == receipt


# ── attestation ──────────────────────────────────────────────────────────────
def _key() -> object:
    from eth_keys import keys as eth_keys

    return eth_keys.PrivateKey(bytes.fromhex(PAYER_KEY.removeprefix("0x")))


def _challenge() -> dict:
    return {
        "x402Version": 2,
        "accepts": [
            {
                "scheme": "exact",
                "network": NETWORK,
                "payTo": PAY_TO,
                "maxAmountRequired": str(REQUIRED_ATOMIC),
                "asset": USDC_BASE_SEPOLIA,
                "resource": "/scan",
                "operation": "api_access",
            }
        ],
    }


def test_attestation_signs_and_verifies() -> None:
    body = attest_payment_required(_challenge(), private_key=_key(), nonce="n1")
    result = verify_payment_required(body)
    assert result.attested and result.ok, result.reason
    assert result.signer.lower() == _key().public_key.to_checksum_address().lower()


def test_attestation_rejects_tampered_pay_to() -> None:
    body = attest_payment_required(_challenge(), private_key=_key(), nonce="n2")
    body["accepts"][0]["payTo"] = "0x" + "99" * 20  # MITM swaps the treasury
    assert verify_payment_required(body).reason == "digest_mismatch"


def test_attestation_rejects_expired_and_unattested() -> None:
    body = attest_payment_required(_challenge(), private_key=_key(), now=1_000_000, nonce="n3")
    result = verify_payment_required(body, now=1_000_000 + ATTEST_MAX_AGE_SECONDS + 1)
    assert result.attested and not result.ok and result.reason == "expired"
    assert verify_payment_required(_challenge()).attested is False


# ── budgets ──────────────────────────────────────────────────────────────────
def test_budget_reserve_settle_release_and_ceiling() -> None:
    ledger = KeyBudgetLedger(policies={"k1": BudgetPolicy(limit_atomic=1_000, max_per_payment=800)})
    first = ledger.check_and_reserve("k1", 600, route="/scan")
    assert ledger.remaining("k1") == 400
    with pytest.raises(BudgetExceeded) as exc:
        ledger.check_and_reserve("k1", 600, route="/scan")
    assert exc.value.retry_after > 0 and exc.value.remaining_atomic == 400
    ledger.settle(first.reservation_id)
    assert ledger.usage("k1")["spent_atomic"] == 600
    assert ledger.settle(first.reservation_id)["outcome"] == "settled"  # idempotent
    second = ledger.check_and_reserve("k1", 400)
    assert ledger.release(second.reservation_id) is True
    assert ledger.usage("k1") == {"spent_atomic": 600, "reserved_atomic": 0, "limit_atomic": 1_000}
    with pytest.raises(BudgetExceeded):
        ledger.check_and_reserve("k1", 801)  # per-payment cap


def test_budget_unconfigured_key_is_unrestricted() -> None:
    ledger = KeyBudgetLedger()
    reservation = ledger.check_and_reserve("anon", 10**18)
    assert ledger.remaining("anon") is None
    ledger.settle(reservation.reservation_id)


# ── usage ledger ─────────────────────────────────────────────────────────────
def test_usage_ledger_appends_reads_and_summarises(tmp_path) -> None:
    path = tmp_path / "usage.jsonl"
    record_usage(
        path,
        key_id="k1",
        outcome="settled",
        payment_id="p1",
        amount_atomic=10_000,
        route="/scan",
        tx_hash="0xaa",
    )
    record_usage(path, key_id="k1", outcome="released", payment_id="p2", amount_atomic=5_000)
    record_usage(path, key_id="k2", outcome="settled", payment_id="p3", amount_atomic=20_000)
    assert len(read_usage(path)) == 3
    assert len(read_usage(path, key_id="k2")) == 1
    summary = summarize_usage(path)
    assert summary["rows"] == 3 and summary["settled_atomic"] == 30_000
    assert summary["by_outcome"] == {"settled": 2, "released": 1}
    with pytest.raises(ValueError):
        record_usage(path, key_id="k1", outcome="bogus")


# ── facilitator double: verify -> settle -> replay -> underpay ───────────────
def _flow(*, value: int = REQUIRED_ATOMIC) -> dict[str, object]:
    from coffer._engine.handler import X402Handler

    fake = FakeFacilitator(pay_to=PAY_TO, required_atomic=REQUIRED_ATOMIC)
    router = FacilitatorRouter()
    # Self-facilitator seam: the double implements verify(payment)/settle(payment),
    # exactly the contract SelfFacilitator exposes to the orchestrator.
    router.self_hosted = True
    router._self_facilitator = fake
    handler = X402Handler(wallet=PAY_TO, router=router)
    auth = _authorization(value=value)
    router.submit_authorization("pay_1", auth)
    verified = asyncio.run(
        handler.verify_payment(
            "pay_1",
            "0x" + "cd" * 32,
            network=NETWORK,
            asset=USDC_BASE_SEPOLIA,
            operation="api_access",
            caller_key="key_1",
        )
    )
    # _build_payment pops the authorization on verify, so a settle attempt
    # re-submits it (same signed payload, same nonce — replay stays detectable).
    router.submit_authorization("pay_1", auth)
    settled = asyncio.run(
        handler.settle_payment(
            "pay_1",
            "0x" + "cd" * 32,
            network=NETWORK,
            asset=USDC_BASE_SEPOLIA,
            operation="api_access",
            user_id="key_1",
        )
    )
    return {"verified": verified, "settled": settled, "fake": fake}


def test_verify_then_settle_then_replay_and_underpay() -> None:
    outcome = _flow()
    assert outcome["verified"]["verified"] is True
    assert outcome["settled"]["success"] is True
    headers = settlement_header_pairs(outcome["settled"])
    assert str(decode_settlement_header(headers)["transaction"]).startswith("0x")

    # Replay: the burned authorization nonce is refused.
    fake: FakeFacilitator = outcome["fake"]  # type: ignore[assignment]
    replay = asyncio.run(fake.verify({"network": NETWORK, "authorization": _authorization()}))
    assert replay.verified is False and replay.reason == "authorization_already_used"

    # Underpayment: refused before any settle result could be produced.
    underpay = _flow(value=REQUIRED_ATOMIC - 1)
    assert underpay["verified"]["verified"] is False
    assert underpay["settled"]["success"] is False
    # Coffer's settle path collapses the facilitator's reason into a generic
    # message, so the precise refusal is asserted on the facilitator double.
    assert underpay["fake"].refusals[-1]["reason"] == "insufficient_amount"  # type: ignore[index]
