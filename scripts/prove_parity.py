"""Prove the Pry-parity payment path end to end — no chain broadcast.

Transcript produced (all through production code, only the facilitator is a
local double that performs real EIP-3009 signature recovery):

    1. POST /scan unpaid            -> 402 + PAYMENT-REQUIRED (attested)
    2. POST /scan signed (0.01 USDC)-> verify -> settle -> 200 + PAYMENT-RESPONSE
    3. same payload again           -> 402 (tx replay burned)
    4. underpaid authorization      -> 402 (insufficient amount)
    5. ledger rows                  -> one settled row for the key

Run: .venv/bin/python scripts/prove_parity.py
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests"))

from eth_account import Account
from facilitator_fake import USDC_BASE_SEPOLIA, FakeFacilitator
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient

from coffer._engine.attestation import attest_payment_required, verify_payment_required
from coffer._engine.budgets import BudgetExceeded, BudgetPolicy, KeyBudgetLedger
from coffer._engine.facilitator_router import FacilitatorRouter
from coffer._engine.handler import X402Handler
from coffer._engine.payment_headers import (
    HEADER_PAYMENT_SIGNATURE,
    PaymentHeaderError,
    decode_payment_header,
    decode_settlement_header,
    encode_payment_required,
    settlement_header_pairs,
)
from coffer._engine.self_facilitator import SelfFacilitator
from coffer._engine.usage_ledger import read_usage, record_usage, summarize_usage

PAYER_KEY = "0x" + "33" * 32
PAY_TO = "0x" + "44" * 20
NETWORK = "eip155:84532"
REQUIRED_ATOMIC = 10_000  # 0.01 USDC
OPERATION = "api_access"
WORK = Path("/tmp/opencode/coffer-parity-proof")
LEDGER = WORK / "usage_ledger.jsonl"


def sign_authorization(*, value: int, nonce_byte: str) -> dict:
    account = Account.from_key(PAYER_KEY)
    nonce = "0x" + nonce_byte * 32
    auth = {
        "from": account.address,
        "to": PAY_TO,
        "value": value,
        "validAfter": 0,
        "validBefore": int(time.time()) + 600,
        "nonce": nonce,
    }
    digest = SelfFacilitator(pay_to=PAY_TO)._digest(auth, 84532, USDC_BASE_SEPOLIA)
    signed = account.unsafe_sign_hash(digest)
    signature = getattr(signed, "signature", signed)
    auth["signature"] = "0x" + (signature.hex() if isinstance(signature, bytes) else str(signature))
    return auth


def payment_envelope(payment_id: str, auth: dict) -> str:
    payload = {
        "x402Version": 2,
        "scheme": "exact",
        "network": NETWORK,
        "asset": USDC_BASE_SEPOLIA,
        "payment_id": payment_id,
        "key_id": "key_demo",
        "authorization": auth,
    }
    return base64.b64encode(json.dumps(payload).encode()).decode()


def build_app(fake: FakeFacilitator, handler: X402Handler, budgets: KeyBudgetLedger) -> FastAPI:
    app = FastAPI()

    @app.post("/scan")
    async def scan(request: Request):
        from eth_utils import keccak

        try:
            payload = decode_payment_header(dict(request.headers))
        except PaymentHeaderError:
            body, _ = handler.create_payment_required(OPERATION, resource="/scan")
            attest_payment_required(body)
            return JSONResponse(
                body, status_code=402, headers={"PAYMENT-REQUIRED": encode_payment_required(body)}
            )

        payment_id = str(payload.get("payment_id", ""))
        key_id = str(payload.get("key_id", ""))
        auth = payload.get("authorization") or {}
        tx_hash = (
            "0x" + keccak(str(auth.get("nonce", "")).encode() + str(auth.get("signature", "")).encode()).hex()
        )

        def unpaid() -> JSONResponse:
            body, _ = handler.create_payment_required(OPERATION, resource="/scan")
            attest_payment_required(body)
            return JSONResponse(
                body, status_code=402, headers={"PAYMENT-REQUIRED": encode_payment_required(body)}
            )

        try:
            reservation = budgets.check_and_reserve(key_id, int(auth.get("value", 0) or 0), route="/scan")
        except BudgetExceeded as exc:
            record_usage(
                LEDGER,
                key_id=key_id,
                outcome="failed",
                route="/scan",
                amount_atomic=0,
                payment_id=payment_id,
                extra={"reason": exc.reason},
            )
            return unpaid()

        handler.router.submit_authorization(payment_id, auth)
        verified = await handler.verify_payment(
            payment_id,
            tx_hash,
            network=NETWORK,
            asset=USDC_BASE_SEPOLIA,
            operation=OPERATION,
            caller_key=key_id,
        )
        if not verified.get("verified"):
            budgets.release(reservation.reservation_id)
            record_usage(
                LEDGER,
                key_id=key_id,
                outcome="failed",
                route="/scan",
                amount_atomic=int(auth.get("value", 0) or 0),
                payment_id=payment_id,
                tx_hash=tx_hash,
                extra={"reason": verified.get("reason", "")},
            )
            return unpaid()

        handler.router.submit_authorization(payment_id, auth)
        settled = await handler.settle_payment(
            payment_id,
            tx_hash,
            network=NETWORK,
            asset=USDC_BASE_SEPOLIA,
            operation=OPERATION,
            user_id=key_id,
        )
        if not settled.get("success"):
            budgets.release(reservation.reservation_id)
            record_usage(
                LEDGER,
                key_id=key_id,
                outcome="failed",
                route="/scan",
                amount_atomic=int(auth.get("value", 0) or 0),
                payment_id=payment_id,
                tx_hash=tx_hash,
                extra={"reason": "settle_failed"},
            )
            return unpaid()

        budgets.settle(reservation.reservation_id)
        record_usage(
            LEDGER,
            key_id=key_id,
            outcome="settled",
            payment_id=payment_id,
            route="/scan",
            network=NETWORK,
            asset=USDC_BASE_SEPOLIA,
            amount_atomic=int(auth.get("value", 0) or 0),
            tx_hash=str(settled.get("transaction", "")),
            reservation_id=reservation.reservation_id,
        )
        return JSONResponse(
            {"resource": "scan", "status": "paid", "payment_id": payment_id},
            status_code=200,
            headers=settlement_header_pairs(settled),
        )

    return app


async def main() -> int:
    WORK.mkdir(parents=True, exist_ok=True)
    # Demo attestation key so the transcript shows a SIGNED challenge. Real
    # deployments set X402_ATTEST_KEY (or the facilitator key); nothing here
    # is a production secret.
    os.environ.setdefault("X402_ATTEST_SECRET", "demo-only-attest-secret")
    LEDGER.unlink(missing_ok=True)

    fake = FakeFacilitator(pay_to=PAY_TO, required_atomic=REQUIRED_ATOMIC)
    router = FacilitatorRouter()
    router.self_hosted = True
    router._self_facilitator = fake
    handler = X402Handler(wallet=PAY_TO, router=router)
    budgets = KeyBudgetLedger(policies={"key_demo": BudgetPolicy(limit_atomic=1_000_000)})
    app = build_app(fake, handler, budgets)

    lines: list[str] = []

    def show(label: str, response) -> None:
        headers = {k.lower(): v for k, v in response.headers.items()}
        lines.append(f"{label:<38} -> {response.status_code}")
        if "payment-required" in headers:
            body = json.loads(base64.b64decode(headers["payment-required"]))
            att = verify_payment_required(body)
            lines.append(
                f"    PAYMENT-REQUIRED accepts={len(body.get('accepts', []))} "
                f"attested={att.attested} ok={att.ok} signer={att.signer[:10] or '-'}"
            )
        if "payment-response" in headers:
            receipt = decode_settlement_header({"PAYMENT-RESPONSE": headers["payment-response"]})
            lines.append(
                f"    PAYMENT-RESPONSE transaction={receipt['transaction'][:18]}... "
                f"simulated={fake.settlements[-1]['simulated']}"
            )
            lines.append(
                f"    X-PAYMENT-RESPONSE identical={headers['x-payment-response'] == headers['payment-response']}"
            )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://coffer.local") as client:
        show("1 unpaid", await client.post("/scan"))

        paid = sign_authorization(value=REQUIRED_ATOMIC, nonce_byte="ab")
        envelope = payment_envelope("pay_demo", paid)
        first = await client.post("/scan", headers={HEADER_PAYMENT_SIGNATURE: envelope})
        show("2 signed payment", first)
        replay = await client.post("/scan", headers={HEADER_PAYMENT_SIGNATURE: envelope})
        show("3 replay (same payload)", replay)
        under = sign_authorization(value=REQUIRED_ATOMIC - 1, nonce_byte="cd")
        short = await client.post(
            "/scan", headers={HEADER_PAYMENT_SIGNATURE: payment_envelope("pay_under", under)}
        )
        show("4 underpaid authorization", short)

    rows = read_usage(LEDGER)
    summary = summarize_usage(LEDGER)
    lines.append(
        f"5 ledger rows={len(rows)} settled_atomic={summary['settled_atomic']} "
        f"by_outcome={summary['by_outcome']}"
    )
    lines.append(f"    last settled row={json.dumps(rows[0], separators=(',', ':')) if rows else 'none'}")

    print("\n".join(lines))
    ok = (
        first.status_code == 200
        and replay.status_code == 402
        and short.status_code == 402
        and summary["settled_atomic"] == REQUIRED_ATOMIC
    )
    print(f"\nRESULT: {'PASS' if ok else 'FAIL'} (facilitator=local fake, no chain broadcast)")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
