"""Wiring tests: the real ASGI middleware + decorator drive the parity modules.

Every assertion goes through ``X402Middleware`` (production adapters), the gate
(``authorize_payment``), the attested challenge builder and the usage ledger.
Only the settler is a double — it stands in for the chain.
"""

from __future__ import annotations

import base64
import json
from typing import Any

import pytest
from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient

from coffer._engine.adapters import X402Middleware, require_payment
from coffer._engine.attestation import attest_payment_required, verify_payment_required
from coffer._engine.budgets import BudgetPolicy, KeyBudgetLedger
from coffer._engine.gate import GatePolicy
from coffer._engine.payment_headers import HEADER_PAYMENT_SIGNATURE
from coffer._engine.store import PaymentStore
from coffer._engine.usage_ledger import read_usage

PAY_TO = "0x" + "44" * 20


def _eth_key():
    from eth_keys import keys as eth_keys

    return eth_keys.PrivateKey(bytes.fromhex("33" * 32))


def _envelope(*, value: int = 10_000, challenge: dict | None = None) -> str:
    payload: dict[str, Any] = {
        "x402Version": 2,
        "scheme": "exact",
        "network": "eip155:84532",
        "asset": "0x036CbD53842c5426634e7929541eC2318f3dCF7e",
        "payment_id": "pay_1",
        "key_id": "k1",
        "authorization": {"from": "0x" + "33" * 20, "value": value, "nonce": "0x" + "ab" * 32},
    }
    if challenge is not None:
        payload["challenge"] = challenge
    return base64.b64encode(json.dumps(payload).encode()).decode()


class Settler:
    """Records calls; refuses under a floor amount (stands in for the chain)."""

    def __init__(self, *, floor: int = 10_000, fail: bool = False) -> None:
        self.floor = floor
        self.fail = fail
        self.calls: list[dict] = []

    async def __call__(self, payload: dict) -> dict:
        self.calls.append(payload)
        if self.fail:
            raise RuntimeError("facilitator exploded")
        value = int((payload.get("authorization") or {}).get("value", 0))
        if value < self.floor:
            return {"success": False, "reason": "insufficient_amount"}
        return {
            "success": True,
            "transaction": "0x" + "cd" * 32,
            "network": payload.get("network", ""),
        }


def _app(policy: GatePolicy, *, decorated: bool = False) -> FastAPI:
    app = FastAPI()

    @app.post("/scan")
    async def scan(request: Request) -> dict[str, str]:
        return {"status": "paid"}

    @app.post("/decorated")
    @require_payment(policy=policy)
    async def decorated_route(request: Request) -> dict[str, str]:
        return {"status": "paid"}

    if not decorated:
        app.add_middleware(X402Middleware, policy=policy)
    return app


def _policy(tmp_path, **kwargs: Any) -> tuple[GatePolicy, KeyBudgetLedger]:
    budgets = kwargs.pop("budgets", None) or KeyBudgetLedger(
        policies={"k1": BudgetPolicy(limit_atomic=20_000, max_per_payment=15_000)}
    )
    return (
        GatePolicy(
            store=PaymentStore(),
            budgets=budgets,
            ledger_path=tmp_path / "usage.jsonl",
            **kwargs,
        ),
        budgets,
    )


def _post(app: FastAPI, path: str = "/scan", headers: dict | None = None):
    async def _run():
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
            return await client.post(path, headers=headers or {})

    import asyncio

    return asyncio.run(_run())


def test_unpaid_challenge_is_attested_and_reasoned(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("X402_ATTEST_SECRET", "test-attest-secret")
    policy, _ = _policy(tmp_path)
    response = _post(_app(policy))
    assert response.status_code == 402
    body = json.loads(base64.b64decode(response.headers["payment-required"]))
    assert body["reason"] == "payment_required"
    result = verify_payment_required(body)
    assert result.attested and result.ok


def test_paid_request_carries_both_receipt_headers_and_ledger_row(tmp_path) -> None:
    settler = Settler()
    policy, budgets = _policy(tmp_path, settler=settler)
    response = _post(_app(policy), headers={HEADER_PAYMENT_SIGNATURE: _envelope()})
    assert response.status_code == 200
    assert response.headers["payment-response"] == response.headers["x-payment-response"]
    receipt = json.loads(base64.b64decode(response.headers["payment-response"]))
    assert receipt["transaction"].startswith("0x")
    assert len(settler.calls) == 1
    assert budgets.usage("k1")["spent_atomic"] == 10_000
    rows = read_usage(tmp_path / "usage.jsonl")
    assert [row["outcome"] for row in rows] == ["settled"]
    assert rows[0]["key_id"] == "k1" and rows[0]["amount_atomic"] == 10_000


def test_refusal_releases_reservation_and_writes_released_row(tmp_path) -> None:
    policy, budgets = _policy(tmp_path, settler=Settler(floor=20_000))
    response = _post(_app(policy), headers={HEADER_PAYMENT_SIGNATURE: _envelope(value=10_000)})
    assert response.status_code == 402
    body = json.loads(base64.b64decode(response.headers["payment-required"]))
    assert body["reason"] == "insufficient_amount"
    assert budgets.usage("k1") == {"spent_atomic": 0, "reserved_atomic": 0, "limit_atomic": 20_000}
    assert [row["outcome"] for row in read_usage(tmp_path / "usage.jsonl")] == ["released"]


def test_budget_exhaustion_returns_retry_after_and_never_settles(tmp_path) -> None:
    settler = Settler()
    budgets = KeyBudgetLedger(policies={"k1": BudgetPolicy(limit_atomic=5_000)})
    policy, _ = _policy(tmp_path, settler=settler, budgets=budgets)
    response = _post(_app(policy), headers={HEADER_PAYMENT_SIGNATURE: _envelope(value=10_000)})
    assert response.status_code == 402
    assert int(response.headers["retry-after"]) > 0
    assert json.loads(base64.b64decode(response.headers["payment-required"]))["reason"] == "budget_exhausted"
    assert settler.calls == []


def test_missing_settler_fails_closed(tmp_path) -> None:
    policy, budgets = _policy(tmp_path)
    response = _post(_app(policy), headers={HEADER_PAYMENT_SIGNATURE: _envelope()})
    assert response.status_code == 402
    assert (
        json.loads(base64.b64decode(response.headers["payment-required"]))["reason"] == "settler_unavailable"
    )
    assert budgets.usage("k1")["reserved_atomic"] == 0


def test_settler_exception_is_failed_row_not_200(tmp_path) -> None:
    policy, _ = _policy(tmp_path, settler=Settler(fail=True))
    response = _post(_app(policy), headers={HEADER_PAYMENT_SIGNATURE: _envelope()})
    assert response.status_code == 402
    assert [row["outcome"] for row in read_usage(tmp_path / "usage.jsonl")] == ["failed"]


def test_tampered_challenge_attestation_is_rejected(tmp_path) -> None:
    challenge = attest_payment_required(
        {"accepts": [{"payTo": PAY_TO, "maxAmountRequired": "10000", "network": "eip155:84532"}]},
        private_key=_eth_key(),
        nonce="n9",
    )
    challenge["accepts"][0]["payTo"] = "0x" + "99" * 20  # MITM swap after signing
    settler = Settler()
    policy, _ = _policy(tmp_path, settler=settler, require_attestation=True)
    response = _post(_app(policy), headers={HEADER_PAYMENT_SIGNATURE: _envelope(challenge=challenge)})
    assert response.status_code == 402
    assert (
        json.loads(base64.b64decode(response.headers["payment-required"]))["reason"] == "invalid_attestation"
    )
    assert settler.calls == []


def test_legacy_x_payment_id_still_passes(tmp_path) -> None:
    store = PaymentStore()
    store.record(payment_id="pay_legacy", caller_key="", amount_usd=0.01, verified=True)
    policy = GatePolicy(store=store, ledger_path=tmp_path / "usage.jsonl")
    response = _post(_app(policy), headers={"X-Payment-ID": "pay_legacy"})
    assert response.status_code == 200


def test_async_decorator_uses_the_gate(tmp_path) -> None:
    settler = Settler()
    policy, budgets = _policy(tmp_path, settler=settler)
    response = _post(
        _app(policy, decorated=True), path="/decorated", headers={HEADER_PAYMENT_SIGNATURE: _envelope()}
    )
    assert response.status_code == 200
    assert budgets.usage("k1")["spent_atomic"] == 10_000
    unpaid = _post(_app(policy, decorated=True), path="/decorated")
    assert unpaid.status_code == 402


def test_malformed_payment_header_is_402_with_reason(tmp_path) -> None:
    policy, _ = _policy(tmp_path, settler=Settler())
    response = _post(_app(policy), headers={HEADER_PAYMENT_SIGNATURE: "!!!not-base64"})
    assert response.status_code == 402
    assert "base64" in json.loads(base64.b64decode(response.headers["payment-required"]))["reason"]


@pytest.mark.parametrize("value,expected", [(10_000, 200), (9_999, 402)])
def test_amount_boundary(tmp_path, value: int, expected: int) -> None:
    policy, _ = _policy(tmp_path, settler=Settler())
    response = _post(_app(policy), headers={HEADER_PAYMENT_SIGNATURE: _envelope(value=value)})
    assert response.status_code == expected
