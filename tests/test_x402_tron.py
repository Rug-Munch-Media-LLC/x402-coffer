"""TRON TRC-20 payment reception — mocked Trongrid RPC, full failure matrix.

Uses topic-based decoding (same approach as EVM log decoding):
- events must have topics[0] == TRANSFER_EVENT_TOPIC0
- topics[1] = from (32-byte, TRON prefix 41)
- topics[2] = to (32-byte, TRON prefix 41)
- data = value (uint256)
"""

from __future__ import annotations

import json

import pytest
import respx
from httpx import Response

from coffer._engine.tron_verifier import (
    DEFAULT_TRON_RPC_URL,
    TRANSFER_EVENT_TOPIC0,
    verify_tron_payment,
)

MERCHANT_HEX = "41" + "a" * 40  # TRON hex prefix + 20 bytes
RPC = "https://trongrid.test"


def _tx_info(
    *,
    block: int = 42_000_000,
    to_hex: str = MERCHANT_HEX,
    value: int = 1_000_000,
    timestamp: int = 1755700000000,
) -> dict:
    """Build a mock Trongrid gettransactioninfobyid response with topic-based logs."""
    return {
        "id": "0xtx",
        "blockNumber": block,
        "blockTimeStamp": timestamp,
        "blockHash": "0x" + "ab" * 32,
        "log": [
            {
                "topics": [
                    TRANSFER_EVENT_TOPIC0,
                    "0x000000000000000000000000" + "b" * 40,  # from
                    "0x000000000000000000000000" + to_hex[2:],  # to (strip 41 prefix for topic)
                ],
                "data": "0x" + value.to_bytes(32, "big").hex(),
                "address": "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t",
            },
        ],
    }


@respx.mock
@pytest.mark.asyncio
async def test_valid_trc20_transfer_verified() -> None:
    respx.post(f"{RPC}/walletsolidity/gettransactioninfobyid").mock(
        return_value=Response(200, json=_tx_info())
    )
    out = await verify_tron_payment("0xtx", MERCHANT_HEX, 1.0, rpc_url=RPC)
    assert out["verified"] is True, out
    assert out["amount_raw"] == 1_000_000
    assert out["amount_decimals"] == 6
    assert out["amount_usd"] == 1.0


@respx.mock
@pytest.mark.asyncio
async def test_wrong_recipient_rejected() -> None:
    respx.post(f"{RPC}/walletsolidity/gettransactioninfobyid").mock(
        return_value=Response(200, json=_tx_info(to_hex="41" + "c" * 40))
    )
    out = await verify_tron_payment("0xtx", MERCHANT_HEX, 1.0, rpc_url=RPC)
    assert out["verified"] is False and "wrong_recipient" in out["reason"]


@respx.mock
@pytest.mark.asyncio
async def test_underpayment_rejected() -> None:
    respx.post(f"{RPC}/walletsolidity/gettransactioninfobyid").mock(
        return_value=Response(200, json=_tx_info(value=100))  # 0.0001 USDT < $1
    )
    out = await verify_tron_payment("0xtx", MERCHANT_HEX, 1.0, rpc_url=RPC)
    assert out["verified"] is False and "insufficient" in out["reason"]


@respx.mock
@pytest.mark.asyncio
async def test_tx_not_found_rejected_not_crash() -> None:
    respx.post(f"{RPC}/walletsolidity/gettransactioninfobyid").mock(return_value=Response(200, json={}))
    out = await verify_tron_payment("0xmissing", MERCHANT_HEX, 1.0, rpc_url=RPC)
    assert out["verified"] is False and "not_found" in out["reason"]


@respx.mock
@pytest.mark.asyncio
async def test_unconfirmed_tx_rejected() -> None:
    info = _tx_info()
    info.pop("blockNumber")
    respx.post(f"{RPC}/walletsolidity/gettransactioninfobyid").mock(return_value=Response(200, json=info))
    out = await verify_tron_payment("0xtx", MERCHANT_HEX, 1.0, rpc_url=RPC)
    assert out["verified"] is False and "unconfirmed" in out["reason"]


@respx.mock
@pytest.mark.asyncio
async def test_rpc_down_falls_back_then_fails_closed() -> None:
    primary = respx.post("https://downrpc.test/walletsolidity/gettransactioninfobyid").mock(
        return_value=Response(503)
    )
    fallback = respx.post(f"{RPC}/walletsolidity/gettransactioninfobyid").mock(
        return_value=Response(200, json=_tx_info())
    )
    out = await verify_tron_payment(
        "0xtx",
        MERCHANT_HEX,
        1.0,
        rpc_url="https://downrpc.test",
        fallback_rpc_urls=[RPC],
    )
    assert primary.called and fallback.called
    assert out["verified"] is True


@respx.mock
@pytest.mark.asyncio
async def test_all_rpcs_down_fail_closed() -> None:
    respx.post(host="downa.test", path__regex=".*").mock(return_value=Response(503))
    respx.post(host="downb.test", path__regex=".*").mock(return_value=Response(503))
    out = await verify_tron_payment(
        "0xtx",
        MERCHANT_HEX,
        1.0,
        rpc_url="https://downa.test",
        fallback_rpc_urls=["https://downb.test"],
    )
    assert out["verified"] is False


@respx.mock
@pytest.mark.asyncio
async def test_router_routes_tron_to_real_verifier() -> None:
    """network_family tron must reach the LIVE verifier, not the scaffold."""
    from coffer._engine.facilitator_config import X402_NETWORK_WALLETS
    from coffer._engine.facilitator_router import FacilitatorRouter

    wallet = X402_NETWORK_WALLETS.get("tron:mainnet") or MERCHANT_HEX
    respx.post(f"{DEFAULT_TRON_RPC_URL}/walletsolidity/gettransactioninfobyid").mock(
        return_value=Response(200, json=_tx_info(to_hex=wallet if wallet.startswith("41") else MERCHANT_HEX))
    )
    router = FacilitatorRouter(facilitators=[], self_hosted=False)
    out = await router.verify("pid", "0xtx", "tron:mainnet", "USDT", 1.0)
    assert out.get("facilitator") == "tron-rpc"
    assert "scaffolded" not in json_dumps(out)


def json_dumps(o: object) -> str:
    return json.dumps(o)


@respx.mock
@respx.mock
@pytest.mark.asyncio
async def test_invalid_amount_rejected() -> None:
    out = await verify_tron_payment("0xtx", MERCHANT_HEX, -1.0, rpc_url=RPC)
    assert out["verified"] is False and "invalid" in out["reason"]


@respx.mock
@pytest.mark.asyncio
async def test_missing_tx_hash() -> None:
    out = await verify_tron_payment("", MERCHANT_HEX, 1.0, rpc_url=RPC)
    assert out["verified"] is False and "missing" in out["reason"]


@respx.mock
@pytest.mark.asyncio
async def test_missing_recipient() -> None:
    out = await verify_tron_payment("0xtx", "", 1.0, rpc_url=RPC)
    assert out["verified"] is False and "missing" in out["reason"]


@respx.mock
@pytest.mark.asyncio
async def test_wrong_topic0_not_matched() -> None:
    """Transfer with wrong topic0 (not a Transfer event) is rejected."""
    info = {
        "id": "0xtx",
        "blockNumber": 42_000_000,
        "blockTimeStamp": 1755700000000,
        "log": [
            {
                "topics": [
                    "0x0000000000000000000000000000000000000000000000000000000000000000",
                    "0x000000000000000000000000" + "b" * 40,
                    "0x000000000000000000000000" + MERCHANT_HEX[2:],
                ],
                "data": "0x00000000000000000000000000000000000000000000000000000000000f4240",
                "address": "TScamToken444444444444444444444444",
            },
        ],
    }
    respx.post(f"{RPC}/walletsolidity/gettransactioninfobyid").mock(return_value=Response(200, json=info))
    out = await verify_tron_payment("0xtx", MERCHANT_HEX, 1.0, rpc_url=RPC)
    assert out["verified"] is False and "no_trc20_transfer_found" in out["reason"]


@respx.mock
@pytest.mark.asyncio
async def test_success_returns_tx_hash() -> None:
    respx.post(f"{RPC}/walletsolidity/gettransactioninfobyid").mock(
        return_value=Response(200, json=_tx_info())
    )
    out = await verify_tron_payment("0xtx", MERCHANT_HEX, 1.0, rpc_url=RPC)
    assert out["verified"] is True
    assert out.get("tx_hash") == "0xtx"
    assert out.get("signer") == "41" + "b" * 40
