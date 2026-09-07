"""EVM payment reception via REAL EIP-3009 signatures (no mocks on crypto).

Builds a genuine EIP-712 ``TransferWithAuthorization`` digest exactly as
``SelfFacilitator._digest`` composes it, signs it with a funded-on-nothing
test key, and asserts the facilitator recovers the payer — plus forged /
expired / wrong-recipient rejections.
"""

from __future__ import annotations

import time

import pytest

from coffer._engine.self_facilitator import SelfFacilitator

# Deterministic test key (funded-on-nothing; safe to commit — not a secret).
PAYER_KEY = "0x" + "33" * 32

BASE = "eip155:8453"
USDC_BASE = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"


def _payer_address() -> str:
    from eth_account import Account

    return Account.from_key(PAYER_KEY).address


def _sign_digest(digest: bytes) -> str:
    from eth_account import Account

    signed = Account.from_key(PAYER_KEY).unsafe_sign_hash(digest)
    sig = getattr(signed, "signature", signed)
    return ("0x" + sig.hex()) if isinstance(sig, bytes) else str(sig)


def _auth(*, pay_to: str, value: int = 1000, expired: bool = False) -> dict:
    now = int(time.time())
    return {
        "from": _payer_address(),
        "to": pay_to,
        "value": value,
        "validAfter": 0,
        "validBefore": now - 10 if expired else now + 600,
        "nonce": "0x" + "n0".replace("n", "0") * 16 if False else "0x" + "00" * 32,
        "signature": "",  # filled by caller after digest computation
    }


def _facilitator(pay_to: str) -> SelfFacilitator:
    return SelfFacilitator(pay_to=pay_to)


@pytest.fixture()
def _clean_eip712_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("X402_EIP712_NAME", "USD Coin")
    monkeypatch.setenv("X402_EIP712_VERSION", "2")


@pytest.mark.asyncio
async def test_real_signature_recovers_payer(_clean_eip712_env: None) -> None:
    pay_to = "0x742d35Cc6634C0532925a3b844Bc9e7595f2bD18"
    fac = _facilitator(pay_to)
    auth = _auth(pay_to=pay_to, value=1000)
    digest = fac._digest(auth, chain_id=8453, token=USDC_BASE)
    auth["signature"] = _sign_digest(digest)

    result = await fac.verify(
        {
            "network": BASE,
            "asset": "USDC",
            "amount_usd": 0.001,
            "authorization": auth,
        }
    )
    assert result.verified is True, result


@pytest.mark.asyncio
async def test_forged_signature_rejected(_clean_eip712_env: None) -> None:
    pay_to = "0x742d35Cc6634C0532925a3b844Bc9e7595f2bD18"
    fac = _facilitator(pay_to)
    auth = _auth(pay_to=pay_to)
    digest = fac._digest(auth, chain_id=8453, token=USDC_BASE)
    # Sign with a DIFFERENT key than auth["from"] claims.
    from eth_account import Account

    other = Account.from_key("0x" + "44" * 32)
    signed = other.unsafe_sign_hash(digest)
    sig = getattr(signed, "signature", signed)
    auth["signature"] = ("0x" + sig.hex()) if isinstance(sig, bytes) else str(sig)

    result = await fac.verify(
        {"network": BASE, "asset": "USDC", "amount_usd": 0.001, "authorization": auth}
    )
    assert result.verified is False


@pytest.mark.asyncio
async def test_expired_authorization_rejected(_clean_eip712_env: None) -> None:
    pay_to = "0x742d35Cc6634C0532925a3b844Bc9e7595f2bD18"
    fac = _facilitator(pay_to)
    auth = _auth(pay_to=pay_to, expired=True)
    digest = fac._digest(auth, chain_id=8453, token=USDC_BASE)
    auth["signature"] = _sign_digest(digest)
    result = await fac.verify(
        {"network": BASE, "asset": "USDC", "amount_usd": 0.001, "authorization": auth}
    )
    assert result.verified is False


@pytest.mark.asyncio
async def test_wrong_recipient_rejected(_clean_eip712_env: None) -> None:
    # Authorization pays attacker wallet; facilitator expects OUR wallet.
    attacker = "0x9999999999999999999999999999999999999999"
    fac = _facilitator("0x742d35Cc6634C0532925a3b844Bc9e7595f2bD18")
    auth = _auth(pay_to=attacker)
    digest = fac._digest(auth, chain_id=8453, token=USDC_BASE)
    auth["signature"] = _sign_digest(digest)
    result = await fac.verify(
        {"network": BASE, "asset": "USDC", "amount_usd": 0.001, "authorization": auth}
    )
    assert result.verified is False


@pytest.mark.asyncio
async def test_missing_authorization_fail_closed(_clean_eip712_env: None) -> None:
    fac = _facilitator("0x742d35Cc6634C0532925a3b844Bc9e7595f2bD18")
    result = await fac.verify({"network": BASE, "asset": "USDC", "amount_usd": 0.001})
    assert result.verified is False and "eip-3009" in result.reason.lower()
