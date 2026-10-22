"""Prove the WIRED payment path end to end — no chain broadcast.

Everything after the header goes through production code: ``X402Middleware`` ->
``gate.authorize_payment`` (attestation -> budget reserve -> settler -> ledger)
-> attested 402 via ``challenge.build_attested_challenge``. The only double is
the facilitator, which performs real EIP-3009 signature recovery.

Transcript:

    1. POST /scan unpaid             -> 402 + attested PAYMENT-REQUIRED
    2. POST /scan signed (0.01 USDC) -> verify -> settle -> 200 + both receipt headers
    3. same payload again            -> 402 (tx replay burned)
    4. underpaid authorization       -> 402 (insufficient_amount)
    5. ledger rows                   -> settled / released / released

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
from httpx import ASGITransport, AsyncClient

from coffer._engine.adapters import X402Middleware
from coffer._engine.attestation import verify_payment_required
from coffer._engine.budgets import BudgetPolicy, KeyBudgetLedger
from coffer._engine.facilitator_router import FacilitatorRouter
from coffer._engine.gate import GatePolicy, make_handler_settler
from coffer._engine.handler import X402Handler
from coffer._engine.payment_headers import (
    HEADER_PAYMENT_SIGNATURE,
    decode_settlement_header,
)
from coffer._engine.self_facilitator import SelfFacilitator
from coffer._engine.usage_ledger import read_usage, summarize_usage

PAYER_KEY = "0x" + "33" * 32
PAY_TO = "0x" + "44" * 20
NETWORK = "eip155:84532"
REQUIRED_ATOMIC = 10_000  # 0.01 USDC
OPERATION = "api_access"
WORK = Path("/tmp/opencode/coffer-parity-proof")
LEDGER = WORK / "usage_ledger.jsonl"


def sign_authorization(*, value: int, nonce_byte: str) -> dict:
    account = Account.from_key(PAYER_KEY)
    auth = {
        "from": account.address,
        "to": PAY_TO,
        "value": value,
        "validAfter": 0,
        "validBefore": int(time.time()) + 600,
        "nonce": "0x" + nonce_byte * 32,
    }
    digest = SelfFacilitator(pay_to=PAY_TO)._digest(auth, 84532, USDC_BASE_SEPOLIA)
    signed = account.unsafe_sign_hash(digest)
    signature = getattr(signed, "signature", signed)
    auth["signature"] = "0x" + (signature.hex() if isinstance(signature, bytes) else str(signature))
    return auth


def envelope(payment_id: str, auth: dict) -> str:
    payload = {
        "x402Version": 2,
        "scheme": "exact",
        "network": NETWORK,
        "asset": USDC_BASE_SEPOLIA,
        "payment_id": payment_id,
        "key_id": "key_demo",
        "operation": OPERATION,
        "authorization": auth,
    }
    return base64.b64encode(json.dumps(payload).encode()).decode()


def build_app(policy: GatePolicy) -> FastAPI:
    app = FastAPI()

    @app.post("/scan")
    async def scan(request: Request) -> dict[str, str]:
        return {"status": "paid"}

    app.add_middleware(X402Middleware, policy=policy)
    return app


async def main() -> int:
    WORK.mkdir(parents=True, exist_ok=True)
    LEDGER.unlink(missing_ok=True)
    # Demo key so the transcript shows a SIGNED challenge — not a production secret.
    os.environ.setdefault("X402_ATTEST_SECRET", "demo-only-attest-secret")

    fake = FakeFacilitator(pay_to=PAY_TO, required_atomic=REQUIRED_ATOMIC)
    router = FacilitatorRouter()
    router.self_hosted = True
    router._self_facilitator = fake
    handler = X402Handler(wallet=PAY_TO, router=router)
    budgets = KeyBudgetLedger(policies={"key_demo": BudgetPolicy(limit_atomic=1_000_000)})
    policy = GatePolicy(
        budgets=budgets,
        ledger_path=LEDGER,
        settler=make_handler_settler(handler),
        operation=OPERATION,
    )
    app = build_app(policy)
    lines: list[str] = []

    def show(label: str, response) -> None:
        headers = {k.lower(): v for k, v in response.headers.items()}
        lines.append(f"{label:<38} -> {response.status_code}")
        if "payment-required" in headers:
            body = json.loads(base64.b64decode(headers["payment-required"]))
            att = verify_payment_required(body)
            lines.append(
                f"    PAYMENT-REQUIRED accepts={len(body.get('accepts', []))} reason={body.get('reason', '-')} "
                f"attested={att.attested} ok={att.ok} signer={att.signer[:10] or '-'}"
            )
        if "payment-response" in headers:
            receipt = decode_settlement_header({"PAYMENT-RESPONSE": headers["payment-response"]})
            tx = str(receipt.get("transaction") or receipt.get("txHash"))
            lines.append(
                f"    PAYMENT-RESPONSE transaction={tx[:18]}... simulated={fake.settlements[-1]['simulated']}"
            )
            lines.append(
                f"    X-PAYMENT-RESPONSE identical={headers['x-payment-response'] == headers['payment-response']}"
            )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://coffer.local") as client:
        show("1 unpaid", await client.post("/scan"))
        paid = sign_authorization(value=REQUIRED_ATOMIC, nonce_byte="ab")
        paid_envelope = envelope("pay_demo", paid)
        first = await client.post("/scan", headers={HEADER_PAYMENT_SIGNATURE: paid_envelope})
        show("2 signed payment", first)
        replay = await client.post("/scan", headers={HEADER_PAYMENT_SIGNATURE: paid_envelope})
        show("3 replay (same payload)", replay)
        under = sign_authorization(value=REQUIRED_ATOMIC - 1, nonce_byte="cd")
        short = await client.post("/scan", headers={HEADER_PAYMENT_SIGNATURE: envelope("pay_under", under)})
        show("4 underpaid authorization", short)

    rows = read_usage(LEDGER)
    summary = summarize_usage(LEDGER)
    lines.append(
        f"5 ledger rows={len(rows)} settled_atomic={summary['settled_atomic']} by_outcome={summary['by_outcome']}"
    )
    lines.append(f"    row[0]={json.dumps(rows[0], separators=(',', ':')) if rows else 'none'}")
    print("\n".join(lines))

    ok = (
        first.status_code == 200
        and replay.status_code == 402
        and short.status_code == 402
        and summary["settled_atomic"] == REQUIRED_ATOMIC
        and budgets.usage("key_demo")["spent_atomic"] == REQUIRED_ATOMIC
    )
    print(f"\nRESULT: {'PASS' if ok else 'FAIL'} (facilitator=local fake, no chain broadcast)")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
