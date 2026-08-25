"""V2 wire-format conformance tests (x402-specification-v2 §5.1)."""

from __future__ import annotations

import pytest

from coffer._engine.body import (
    build_payment_required,
    build_payment_required_v2,
    payment_required_body,
    protocol_version,
)

URL = "https://api.test/premium"


def test_v2_top_level_shape() -> None:
    body = build_payment_required_v2("op", resource_url=URL)
    assert body["x402Version"] == 2
    res = body["resource"]
    assert res["url"] == URL
    assert res["mimeType"] == "application/json"
    assert isinstance(body["accepts"], list) and body["accepts"]
    assert body["extensions"] == {}


def test_v2_accepts_entry_field_names() -> None:
    body = build_payment_required_v2("op", resource_url=URL)
    for opt in body["accepts"]:
        # v2 keys — NOT the v1 names.
        assert "amount" in opt and "maxAmountRequired" not in opt
        assert "payTo" in opt and "payToAddress" not in opt
        assert isinstance(opt["amount"], str)  # atomic units, string
        assert opt["scheme"] == "exact"
        assert ":" in opt["network"]  # CAIP-2
        assert isinstance(opt["maxTimeoutSeconds"], int)
        assert opt["extra"]["name"]


def test_v2_asset_uses_contract_address_where_available() -> None:
    body = build_payment_required_v2("op", resource_url=URL)
    by_key = {(o["network"], o["extra"]["name"].upper()): o for o in body["accepts"]}
    base_usdc = by_key[("eip155:8453", "BASE USDC")]
    assert base_usdc["asset"].startswith("0x")  # contract address, per spec
    tron_usdt = by_key.get(("tron:mainnet", "TRON USDT"))
    if tron_usdt:
        assert tron_usdt["asset"] == "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"
    native = by_key[("eip155:8453", "BASE ETH")]
    assert native["asset"] == "ETH"  # no contract -> honest symbol fallback


def test_v1_builder_unchanged() -> None:
    body = build_payment_required("op", resource=URL, multi_asset=False)
    assert body["x402Version"] == 1
    opt = body["accepts"][0]
    assert "maxAmountRequired" in opt and "payToAddress" in opt
    assert "resource" not in body


def test_version_switch_via_env(monkeypatch: pytest.MonkeyPatch) -> None:

    monkeypatch.delenv("X402_PROTOCOL_VERSION", raising=False)
    assert protocol_version() == 1
    assert payment_required_body(resource=URL)["x402Version"] == 1
    monkeypatch.setenv("X402_PROTOCOL_VERSION", "2")
    assert protocol_version() == 2
    switched = payment_required_body(resource=URL)
    assert switched["x402Version"] == 2
    assert switched["resource"]["url"] == URL


def test_v2_amount_matches_v1_pricing_for_same_option() -> None:
    """Pricing authority is identical across versions — only field NAMES change."""
    v1 = build_payment_required("op", multi_asset=True)
    v2 = build_payment_required_v2("op", resource_url=URL)
    v1_amounts = {o["network"]: o["maxAmountRequired"] for o in v1["accepts"]}
    v2_amounts = {o["network"]: o["amount"] for o in v2["accepts"]}
    for net, amount in v1_amounts.items():
        if net in v2_amounts:
            assert v2_amounts[net] == amount
