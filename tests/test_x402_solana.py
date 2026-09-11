"""Solana SPL payment verifier tests (respx-mocked Solana RPC).

Covers the on-chain verifier (``app.domain.x402.solana_verifier``) and its
router wiring (``facilitator_router`` -> ``external_verify.verify_solana``):

- valid USDC ``transferChecked`` to the merchant's associated token account
  (the realistic shape — SPL destinations are token accounts, not wallets)
  and to a directly-configured recipient address;
- wrong mint / wrong destination / failed tx / underpayment rejections;
- transaction-not-found and unconfirmed rejections (no crash);
- RPC transport failure -> ordered fallback RPC; authoritative JSON-RPC
  errors are final and never retried against a fallback;
- USDT support via the mint table;
- router wiring for ``solana:mainnet`` on BOTH the self-hosted path and the
  external-facilitator fallback path (regression: the latter used to call a
  non-existent ``self._verify_solana`` and raise AttributeError).

All Solana RPC traffic is stubbed with ``respx`` — no network access.
"""

from __future__ import annotations

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

from coffer._engine import external_verify
from coffer._engine.facilitator_config import FacilitatorConfig
from coffer._engine.facilitator_router import FacilitatorRouter
from coffer._engine.solana_verifier import (
    SOLANA_USDC_MINT,
    SOLANA_USDT_MINT,
    SPL_TOKEN_PROGRAM_ID,
    verify_solana_payment,
)

RPC = "https://solana-rpc.test"
RPC_FALLBACK = "https://solana-rpc-fallback.test"

MERCHANT = "MerchantWallet11111111111111111111111111111111111"
MERCHANT_ATA = "MerchantTokenAcct11111111111111111111111111111"
PAYER = "PayerWallet11111111111111111111111111111111111111"
PAYER_ATA = "PayerTokenAcct111111111111111111111111111111111111"
OTHER = "OtherWallet111111111111111111111111111111111111111"
OTHER_ATA = "OtherTokenAcct1111111111111111111111111111111111111"
WRONG_MINT = "FakeMint111111111111111111111111111111111111111111"
SIG = "5" * 87 + "Z"  # signature-shaped string; the verifier does not b58-check


def _tx_result(
    *,
    mint: str = SOLANA_USDC_MINT,
    destination: str = MERCHANT_ATA,
    amount: str = "1000000",
    decimals: int = 6,
    owner: str = MERCHANT,
    authority: str = PAYER,
    err: Any = None,
    slot: int | None = 300_000_000,
    include_destination_balance: bool = True,
) -> dict[str, Any]:
    """A realistic jsonParsed getTransaction result (splToken transferChecked)."""
    post_token_balances: list[dict[str, Any]] = []
    if include_destination_balance:
        post_token_balances.append(
            {
                "accountIndex": 1,
                "address": destination,
                "mint": mint,
                "owner": owner,
                "programId": SPL_TOKEN_PROGRAM_ID,
                "uiTokenAmount": {
                    "uiAmount": 1.0,
                    "decimals": decimals,
                    "amount": amount,
                    "uiAmountString": "1",
                },
            }
        )
    return {
        "slot": slot,
        "blockTime": 1_750_000_000,
        "meta": {
            "err": err,
            "fee": 5000,
            "innerInstructions": [],
            "preTokenBalances": [],
            "postTokenBalances": post_token_balances,
        },
        "transaction": {
            "signatures": [SIG],
            "message": {
                "accountKeys": [
                    {
                        "pubkey": authority,
                        "signer": True,
                        "writable": True,
                        "source": "transaction",
                    },
                    {
                        "pubkey": destination,
                        "signer": False,
                        "writable": True,
                        "source": "transaction",
                    },
                ],
                "instructions": [
                    {
                        "program": "splToken",
                        "programId": SPL_TOKEN_PROGRAM_ID,
                        "parsed": {
                            "type": "transferChecked",
                            "info": {
                                "source": PAYER_ATA,
                                "mint": mint,
                                "destination": destination,
                                "authority": authority,
                                "tokenAmount": {
                                    "uiAmount": 1.0,
                                    "decimals": decimals,
                                    "amount": amount,
                                    "uiAmountString": "1",
                                },
                            },
                        },
                    }
                ],
                "recentBlockhash": "FakeBlockhash11111111111111111111111111111111",
            },
        },
    }


def _rpc_envelope(result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "result": result, "id": 1}


def _mock_rpc(route_url: str, result: Any) -> Any:
    return respx.post(route_url).mock(return_value=httpx.Response(200, json=_rpc_envelope(result)))


# ── verifier core ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_valid_usdc_transfer_to_merchant_ata_verified() -> None:
    """Realistic payment: destination is the merchant's ATA (owner resolved
    from postTokenBalances) -> verified with exact amount/mint/payer."""
    with respx.mock:
        _mock_rpc(RPC, _tx_result())
        res = await verify_solana_payment(SIG, MERCHANT, 1.0, rpc_url=RPC)
    assert res["verified"] is True, res
    assert res["mint"] == SOLANA_USDC_MINT
    assert res["amount_raw"] == 1_000_000
    assert res["amount_decimals"] == 6
    assert res["amount_usd"] == 1.0
    assert res["recipient"] == MERCHANT
    assert res["payer"] == PAYER
    assert res["slot"] == 300_000_000
    assert res["confirmed_at"] != ""


@pytest.mark.asyncio
async def test_valid_transfer_direct_destination_verified() -> None:
    """Back-compat: recipient configured as the token account itself (direct
    destination equality) still verifies without token-balance entries."""
    with respx.mock:
        _mock_rpc(RPC, _tx_result(destination=MERCHANT, include_destination_balance=False))
        res = await verify_solana_payment(SIG, MERCHANT, 1.0, rpc_url=RPC)
    assert res["verified"] is True, res


@pytest.mark.asyncio
async def test_wrong_mint_rejected() -> None:
    """USDC-sized transfer of a NON-USDC mint to the merchant's account."""
    with respx.mock:
        _mock_rpc(RPC, _tx_result(mint=WRONG_MINT))
        res = await verify_solana_payment(SIG, MERCHANT, 1.0, rpc_url=RPC)
    assert res["verified"] is False, res
    assert res["reason"].startswith("wrong_mint"), res


@pytest.mark.asyncio
async def test_wrong_destination_rejected() -> None:
    """Genuine USDC transfer, but to someone else's token account."""
    with respx.mock:
        _mock_rpc(RPC, _tx_result(destination=OTHER_ATA, owner=OTHER))
        res = await verify_solana_payment(SIG, MERCHANT, 1.0, rpc_url=RPC)
    assert res["verified"] is False, res
    assert res["reason"].startswith("wrong_recipient"), res


@pytest.mark.asyncio
async def test_transaction_not_found_rejected() -> None:
    """result:null (unknown signature / not yet visible) -> clean rejection."""
    with respx.mock:
        _mock_rpc(RPC, None)
        res = await verify_solana_payment(SIG, MERCHANT, 1.0, rpc_url=RPC)
    assert res["verified"] is False, res
    assert res["reason"] == "transaction_not_found", res


@pytest.mark.asyncio
async def test_unconfirmed_transaction_rejected() -> None:
    """A result without a confirmed slot must not verify."""
    with respx.mock:
        _mock_rpc(RPC, _tx_result(slot=None))
        res = await verify_solana_payment(SIG, MERCHANT, 1.0, rpc_url=RPC)
    assert res["verified"] is False, res
    assert res["reason"] == "transaction_not_confirmed", res


@pytest.mark.asyncio
async def test_failed_transaction_rejected() -> None:
    """meta.err present (failed tx) -> rejected even with a parsed transfer."""
    with respx.mock:
        _mock_rpc(RPC, _tx_result(err={"InstructionError": [0, {"Custom": 1}]}))
        res = await verify_solana_payment(SIG, MERCHANT, 1.0, rpc_url=RPC)
    assert res["verified"] is False, res
    assert res["reason"].startswith("transaction_failed"), res


@pytest.mark.asyncio
async def test_underpayment_rejected() -> None:
    with respx.mock:
        _mock_rpc(RPC, _tx_result(amount="999999"))
        res = await verify_solana_payment(SIG, MERCHANT, 1.0, rpc_url=RPC)
    assert res["verified"] is False, res
    assert res["reason"].startswith("underpayment"), res
    assert res["amount_raw"] == 999_999


@pytest.mark.asyncio
async def test_usdt_transfer_verified() -> None:
    """USDT is SPL-verifiable too — same flow, USDT mint."""
    with respx.mock:
        _mock_rpc(RPC, _tx_result(mint=SOLANA_USDT_MINT))
        res = await verify_solana_payment(SIG, MERCHANT, 1.0, rpc_url=RPC, expected_mint=SOLANA_USDT_MINT)
    assert res["verified"] is True, res
    assert res["mint"] == SOLANA_USDT_MINT


# ── RPC resilience / fallback ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_primary_rpc_down_falls_back_to_secondary() -> None:
    with respx.mock:
        respx.post(RPC).mock(return_value=httpx.Response(503))
        _mock_rpc(RPC_FALLBACK, _tx_result())
        res = await verify_solana_payment(SIG, MERCHANT, 1.0, rpc_url=RPC, fallback_rpc_urls=[RPC_FALLBACK])
    assert res["verified"] is True, res


@pytest.mark.asyncio
async def test_all_rpcs_down_fails_closed() -> None:
    with respx.mock:
        respx.post(RPC).mock(return_value=httpx.Response(503))
        respx.post(RPC_FALLBACK).mock(return_value=httpx.Response(503))
        res = await verify_solana_payment(SIG, MERCHANT, 1.0, rpc_url=RPC, fallback_rpc_urls=[RPC_FALLBACK])
    assert res["verified"] is False, res
    assert res["reason"].startswith("rpc_http_error"), res


@pytest.mark.asyncio
async def test_rpc_error_envelope_is_final_not_retried() -> None:
    """An authoritative JSON-RPC error must not be overridden by a fallback."""
    with respx.mock:
        respx.post(RPC).mock(
            return_value=httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "error": {"code": -32602, "message": "Invalid param"},
                    "id": 1,
                },
            )
        )
        fallback_route = _mock_rpc(RPC_FALLBACK, _tx_result())
        res = await verify_solana_payment(SIG, MERCHANT, 1.0, rpc_url=RPC, fallback_rpc_urls=[RPC_FALLBACK])
    assert res["verified"] is False, res
    assert res["reason"].startswith("rpc_error_response"), res
    assert fallback_route.call_count == 0


# ── router wiring ─────────────────────────────────────────────────────────────


@pytest.fixture
def solana_wallet(monkeypatch: pytest.MonkeyPatch) -> None:
    """Configure the merchant Solana wallet + RPC for the wiring tests."""
    monkeypatch.setitem(external_verify.X402_NETWORK_WALLETS, "solana:mainnet", MERCHANT)
    monkeypatch.setenv("X402_SOLANA_RPC", RPC)
    monkeypatch.delenv("X402_SOLANA_RPC_FALLBACKS", raising=False)


@pytest.mark.asyncio
async def test_router_verify_solana_mainnet_wiring(solana_wallet: None) -> None:
    """Self-hosted path: solana:mainnet reaches the SPL RPC verifier and the
    on-chain result is returned (facilitator=solana-rpc)."""
    router = FacilitatorRouter(facilitators=[], self_hosted=True)
    with respx.mock:
        _mock_rpc(RPC, _tx_result())
        res = await router.verify("pid_sol_wiring", SIG, "solana:mainnet", "USDC", 1.0)
    assert res["verified"] is True, res
    assert res["facilitator"] == "solana-rpc", res


@pytest.mark.asyncio
async def test_router_verify_solana_usdt_wiring(solana_wallet: None) -> None:
    router = FacilitatorRouter(facilitators=[], self_hosted=True)
    with respx.mock:
        _mock_rpc(RPC, _tx_result(mint=SOLANA_USDT_MINT))
        res = await router.verify("pid_sol_usdt", SIG, "solana:mainnet", "USDT", 1.0)
    assert res["verified"] is True, res
    assert res.get("mint") == SOLANA_USDT_MINT, res


@pytest.mark.asyncio
async def test_external_path_solana_fallback_no_attribute_error(
    solana_wallet: None,
) -> None:
    """Regression: with self_hosted=False and all external facilitators down,
    verify_external used to call the non-existent ``self._verify_solana``
    (AttributeError). It must fall back to the module-level verifier."""
    cfg = FacilitatorConfig(
        name="stub-down",
        verify_url="https://fac.example/verify",
        settle_url="https://fac.example/settle",
    )
    router = FacilitatorRouter(facilitators=[cfg], self_hosted=False)
    with respx.mock:
        respx.post("https://fac.example/verify").mock(return_value=httpx.Response(503))
        _mock_rpc(RPC, _tx_result())
        res = await router.verify("pid_sol_ext", SIG, "solana:mainnet", "USDC", 1.0)
    assert res["verified"] is True, res
    assert res["facilitator"] == "solana-rpc", res


@pytest.mark.asyncio
async def test_verify_solana_missing_wallet_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """No X402_WALLET_SOLANA -> fail closed BEFORE any RPC call."""
    monkeypatch.setitem(external_verify.X402_NETWORK_WALLETS, "solana:mainnet", "")
    with respx.mock:
        route = respx.post(RPC).mock(return_value=httpx.Response(200, json=_rpc_envelope(_tx_result())))
        res = await external_verify.verify_solana("pid_nowallet", SIG, "solana:mainnet", "USDC", 1.0)
    assert res["verified"] is False, res
    assert "X402_WALLET_SOLANA" in res["reason"], res
    assert route.call_count == 0
