"""Regression tests for the C1/H1 payment-bypass fixes.

Covers the Critical payment bypasses from the 2026-09-06 audit:

1. ``router`` (``_amount`` + verify/settle): a MISSING or ``<= 0`` client
   amount is an HTTP 400 — it never silently defaults to a valid zero, and a
   claim below the gated operation's server-authoritative price (the 402
   quote) is an underpayment 400.
2. ``facilitator_orchestration.verify`` / ``SelfFacilitator.verify`` /
   ``_eip7702_matches``: zero/missing amounts fail closed (defense in depth).
3. ``PaymentStore.is_paid``: a record with no positive, finite
   server-confirmed amount is never honoured (fail closed at the gate).

Dust-exploit shape under test: client asserts ``amount_usd=0``/absent (or a
sub-minimum dust claim) so ``asset_to_atomic`` yields a 0/dust required
amount and a worthless on-chain transfer would verify — all of these are now
rejected before any facilitator is consulted.
"""

from __future__ import annotations

from typing import Any

import pytest
from conftest import ASSET, CAIP2_BASE, SERVICE_PRICE_USD, X402E2EHarness

SENDER_IP = "1.1.1.1"

#: Sentinel: the amount key is omitted entirely from the payload.
_MISSING = object()


def _pid(tag: str) -> str:
    return f"pay_{tag}_{'0' * 16}"


def _verify_payload(*, amount: Any) -> dict[str, Any]:
    """Wire /v1/x402/verify payload carrying ``amount`` (or omitted key)."""
    payload: dict[str, Any] = {
        "payment_id": _pid("amt"),
        "tx_hash": "0xtx_amt",
        "network": CAIP2_BASE,
        "asset": ASSET,
    }
    if amount is not _MISSING:
        payload["amount_usd"] = amount
    return payload


# ── Fix 1 — wire /v1/x402/verify: missing / <=0 / below-quote => HTTP 400 ────


class TestVerifyRejectsInvalidAmounts:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "amount",
        [_MISSING, 0, 0.0, -1, -0.01],
        ids=["missing", "zero", "zero-float", "negative", "negative-fraction"],
    )
    async def test_verify_missing_or_nonpositive_amount_is_400(
        self, e2e: X402E2EHarness, amount: Any
    ) -> None:
        async with e2e.caller(SENDER_IP) as client:
            r = await client.post("/v1/x402/verify", json=_verify_payload(amount=amount))
        assert r.status_code == 400
        assert "amount" in str(r.json().get("error", "")).lower()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "amount",
        ["abc", "nan", "inf", "-inf", None],
        ids=["string", "nan", "inf", "-inf", "null"],
    )
    async def test_verify_unparseable_amount_is_400(self, e2e: X402E2EHarness, amount: Any) -> None:
        async with e2e.caller(SENDER_IP) as client:
            r = await client.post("/v1/x402/verify", json=_verify_payload(amount=amount))
        assert r.status_code == 400

    @pytest.mark.asyncio
    async def test_verify_dust_claim_below_operation_minimum_is_400(self, e2e: X402E2EHarness) -> None:
        """The C1 dust shape: >0 but far below the $0.01 api_access quote."""
        # Even though the amount is positive, it is below the server price for
        # the gated operation, so it is rejected BEFORE any facilitator runs.
        async with e2e.caller(SENDER_IP) as client:
            r = await client.post("/v1/x402/verify", json=_verify_payload(amount=SERVICE_PRICE_USD / 10))
        assert r.status_code == 400
        assert "underpayment" in str(r.json().get("error", "")).lower()

    @pytest.mark.asyncio
    async def test_verify_exact_operation_minimum_still_verifies(self, e2e: X402E2EHarness) -> None:
        """An honest payment AT the quoted minimum still round-trips."""
        from conftest import REQUIRED_ATOMIC

        tx = "0xtx_min_ok"
        async with e2e.caller(SENDER_IP) as client:
            e2e.facilitator.record_payment(tx, amount_atomic=REQUIRED_ATOMIC)
            r = await client.post(
                "/v1/x402/verify",
                json={
                    "payment_id": _pid("minok"),
                    "tx_hash": tx,
                    "network": CAIP2_BASE,
                    "asset": ASSET,
                    "amount_usd": SERVICE_PRICE_USD,
                },
            )
        assert r.status_code == 200
        assert r.json()["verified"] is True


# ── Fix 1 — wire /v1/x402/settle: missing / <=0 => HTTP 400 ──────────────────


class TestSettleRejectsInvalidAmounts:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("amount", [_MISSING, 0, -1], ids=["missing", "zero", "negative"])
    async def test_settle_missing_or_nonpositive_amount_is_400(
        self, e2e: X402E2EHarness, amount: Any
    ) -> None:
        payload = _verify_payload(amount=amount)
        async with e2e.caller(SENDER_IP) as client:
            r = await client.post("/v1/x402/settle", json=payload)
        assert r.status_code == 400
        assert "amount" in str(r.json().get("error", "")).lower()


# ── Fix 1 (defense in depth) — orchestration + SelfFacilitator + EIP-7702 ────


class TestOrchestrationRejectsZeroAmount:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("amount", [0, 0.0, -5.0], ids=["zero", "zero-float", "negative"])
    async def test_router_verify_rejects_nonpositive_before_dispatch(self, amount: float) -> None:
        from coffer._engine.facilitator_router import FacilitatorRouter

        router = FacilitatorRouter(self_hosted=False)
        res = await router.verify(_pid("orch"), "0xtx_orch", CAIP2_BASE, ASSET, amount)
        assert res["verified"] is False
        assert "amount" in str(res.get("reason", "")).lower()
        assert res.get("http_status") is None  # internal rejection, not a replay 410


class TestSelfFacilitatorFailsClosedOnBadAmount:
    @pytest.mark.asyncio
    async def test_verify_missing_amount_fails_closed(self) -> None:
        from coffer._engine.self_facilitator import SelfFacilitator

        fac = SelfFacilitator(pay_to="0x742d35Cc6634C0532925a3b844Bc9e7595f2bD18")
        res = await fac.verify({"network": CAIP2_BASE, "asset": ASSET})
        assert res.verified is False
        assert "amount" in (res.reason or "").lower()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("amount", [0, -1], ids=["zero", "negative"])
    async def test_verify_nonpositive_amount_fails_closed(self, amount: float) -> None:
        from coffer._engine.self_facilitator import SelfFacilitator

        fac = SelfFacilitator(pay_to="0x742d35Cc6634C0532925a3b844Bc9e7595f2bD18")
        res = await fac.verify({"network": CAIP2_BASE, "asset": ASSET, "amount_usd": amount})
        assert res.verified is False
        assert "positive" in (res.reason or "").lower()


class TestEip7702MatchesFailsClosedOnBadAmount:
    def test_zero_amount_transfer_never_matches(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from coffer._engine import evm_verify

        monkeypatch.setattr(evm_verify, "X402_WALLET", "0x742d35Cc6634C0532925a3b844Bc9e7595f2bD18")
        # An ERC-20 transfer() of 0 to the server wallet: the receipt fallback
        # must refuse it outright instead of comparing against a $0 minimum.
        zero_transfer = (
            "0xa9059cbb"
            "000000000000000000000000742d35cc6634c0532925a3b844bc9e7595f2bd18"
            "0000000000000000000000000000000000000000000000000000000000000000"
        )
        tx = {"to": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913", "input": zero_transfer}
        ok, reason = evm_verify._eip7702_matches(tx, CAIP2_BASE, ASSET, 0.0)
        assert ok is False
        assert "amount" in reason.lower()

    def test_missing_amount_never_matches(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from coffer._engine import evm_verify

        monkeypatch.setattr(evm_verify, "X402_WALLET", "0x742d35Cc6634C0532925a3b844Bc9e7595f2bD18")
        tx = {"to": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913", "input": "0x"}
        ok, reason = evm_verify._eip7702_matches(tx, CAIP2_BASE, ASSET, 0.0)
        assert ok is False
        assert "amount" in reason.lower()


# ── Fix 2 — PaymentStore gate fails closed on zero/absent amounts ─────────────


class TestPaymentStoreAmountGate:
    def test_record_without_amount_never_paid(self) -> None:
        from coffer._engine.store import PaymentStore

        store = PaymentStore()
        store.record(payment_id="p0", caller_key="c1")  # amount defaults to 0.0
        assert store.is_paid("p0", "c1") is False

    @pytest.mark.parametrize(
        "amount",
        [0.0, -1.0, float("nan"), float("inf"), float("-inf")],
        ids=["zero", "negative", "nan", "inf", "-inf"],
    )
    def test_nonpositive_or_nonfinite_amount_never_paid(self, amount: float) -> None:
        from coffer._engine.store import PaymentStore

        store = PaymentStore()
        store.record(payment_id="p1", caller_key="c1", amount_usd=amount, verified=True)
        assert store.is_paid("p1", "c1") is False

    def test_positive_server_confirmed_amount_is_paid(self) -> None:
        from coffer._engine.store import PaymentStore

        store = PaymentStore()
        store.record(payment_id="p2", caller_key="c1", amount_usd=0.01, verified=True)
        assert store.is_paid("p2", "c1") is True
        # Caller binding is unaffected by the amount gate.
        assert store.is_paid("p2", "someone-else") is False

    def test_amount_gate_survives_redis_rehydration(self) -> None:
        """A poisoned zero-amount record hydrated from Redis still fails closed."""
        from coffer._engine.store import PaymentStore

        class _FakeRedis:
            def __init__(self) -> None:
                self.data: dict[str, str] = {}

            def setex(self, key: str, ttl: int, value: str) -> None:
                self.data[key] = value

            def get(self, key: str) -> str | None:
                return self.data.get(key)

            def ping(self) -> bool:
                return True

        fake = _FakeRedis()
        store = PaymentStore()
        store._redis = fake  # record pushes the poisoned row into the mirror
        store.record(payment_id="pid-zero", caller_key="9.9.9.9", amount_usd=0.0, verified=True)

        fresh = PaymentStore()
        fresh._redis = fake
        # Fresh instance: memory empty, mirror holds a zero-amount record.
        assert fresh._records == {}
        assert fresh.is_paid("pid-zero", "9.9.9.9") is False
