"""Tests for competition-sourced features: scheme negotiation, extension policy, typed errors.

Covers the dynamic payment-scheme negotiation added for client-capability
discovery, the scheme-extension mutation policy that prevents an enriched
offer from weakening the baseline, and the typed ``VerifyError`` /
``SettleError`` exceptions raised during verify/settle orchestration.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from coffer._engine.batch import (
    claim_channel,
    create_channel,
    get_channel,
    refund_channel,
)
from coffer._engine.handler import SettleError, VerifyError, X402Handler
from coffer._engine.scheme import PaymentInfo, negotiate_scheme, validate_scheme_extension

# --- scheme negotiation -----------------------------------------------------


def test_negotiate_scheme_exact_default() -> None:
    """No client schemes advertised -> fall back to 'exact'."""
    assert negotiate_scheme() == "exact"
    assert negotiate_scheme(client_schemes=None) == "exact"
    assert negotiate_scheme(client_schemes=[]) == "exact"


def test_negotiate_scheme_native_match() -> None:
    """Client supports the network-native scheme -> prefer it."""
    assert negotiate_scheme(client_schemes=["exact"], network="eip155:8453") == "exact"


def test_negotiate_scheme_fallback() -> None:
    """Client only supports a non-native scheme -> use the first client scheme."""
    assert negotiate_scheme(client_schemes=["custom"], network="eip155:8453") == "custom"


# --- scheme extension policy ------------------------------------------------


def test_validate_scheme_extension_ok() -> None:
    """Baseline identical to enriched -> no violation."""
    baseline = {"scheme": "exact", "network": "eip155:8453", "accepts": [{"asset": "USDC"}]}
    enriched = dict(baseline)
    assert validate_scheme_extension(baseline, enriched, extension_key="test") is None


def test_validate_scheme_extension_rejects_field_change() -> None:
    """Changing an immutable field (network) -> error string."""
    baseline = {"scheme": "exact", "network": "eip155:8453", "accepts": [{"asset": "USDC"}]}
    enriched = dict(baseline)
    enriched["network"] = "eip155:1"
    err = validate_scheme_extension(baseline, enriched, extension_key="test")
    assert err is not None
    assert "network" in err


def test_validate_scheme_extension_rejects_accepts_length() -> None:
    """Changing the accepts[] length -> error string."""
    baseline = {"scheme": "exact", "network": "eip155:8453", "accepts": [{"asset": "USDC"}]}
    enriched = dict(baseline)
    enriched["accepts"] = [{"asset": "USDC"}, {"asset": "USDT"}]
    err = validate_scheme_extension(baseline, enriched, extension_key="test")
    assert err is not None
    assert "accepts length" in err


# --- typed errors -----------------------------------------------------------


def test_verify_error_class() -> None:
    """VerifyError exposes the failure reason."""
    err = VerifyError("test")
    assert err.reason == "test"


def test_settle_error_class() -> None:
    """SettleError exposes the failure reason."""
    err = SettleError("test")
    assert err.reason == "test"


# --- 6-point lifecycle hooks -------------------------------------------------


def _handler(tmp_path, router):
    """A handler with audit files redirected into tmp_path."""
    h = X402Handler(router=router)
    h.payments_file = tmp_path / "payments.jsonl"
    h.settlements_file = tmp_path / "settlements.jsonl"
    return h


@pytest.mark.asyncio
async def test_pre_verify_hook_aborts(tmp_path) -> None:
    """A pre-verify hook returning abort=True short-circuits verification."""
    router = AsyncMock()
    h = _handler(tmp_path, router)
    h.on_pre_verify(lambda **kw: {"abort": True, "reason": "blocked"})
    res = await h.verify_payment("pid_h1", "0xtx_h1", amount_usd=1.0)
    assert res["verified"] is False
    assert res["reason"] == "blocked"
    router.verify.assert_not_awaited()


@pytest.mark.asyncio
async def test_post_verify_hook_fires(tmp_path) -> None:
    """A post-verify hook fires after a successful verification."""
    router = AsyncMock()
    router.verify.return_value = {"verified": True}
    h = _handler(tmp_path, router)
    calls: list[dict] = []
    h.on_post_verify(calls.append)
    res = await h.verify_payment("pid_h2", "0xtx_h2", amount_usd=1.0)
    assert res["verified"] is True
    assert len(calls) == 1
    assert calls[0]["verified"] is True


@pytest.mark.asyncio
async def test_on_verify_failure_hook_fires(tmp_path) -> None:
    """An on-verify-failure hook fires after a failed verification."""
    router = AsyncMock()
    router.verify.return_value = {"verified": False, "reason": "test"}
    h = _handler(tmp_path, router)
    calls: list[dict] = []
    h.on_verify_failure(calls.append)
    res = await h.verify_payment("pid_h3", "0xtx_h3", amount_usd=1.0)
    assert res["verified"] is False
    assert len(calls) == 1
    assert calls[0]["reason"] == "test"


@pytest.mark.asyncio
async def test_pre_settle_hook_aborts(tmp_path) -> None:
    """A pre-settle hook returning abort=True short-circuits settlement."""
    router = AsyncMock()
    h = _handler(tmp_path, router)
    h.on_pre_settle(lambda **kw: {"abort": True})
    res = await h.settle_payment("pid_h4", "0xtx_h4", amount_usd=1.0)
    assert res["success"] is False
    router.settle.assert_not_awaited()


@pytest.mark.asyncio
async def test_post_settle_hook_fires(tmp_path) -> None:
    """A post-settle hook fires after a successful settlement."""
    router = AsyncMock()
    router.settle.return_value = {"success": True}
    h = _handler(tmp_path, router)
    calls: list[dict] = []
    h.on_post_settle(calls.append)
    res = await h.settle_payment("pid_h5", "0xtx_h5", amount_usd=1.0)
    assert res["success"] is True
    assert len(calls) == 1
    assert calls[0]["success"] is True


# --- PaymentInfo escrow expiry + fee validation ------------------------------


def _payment_info(**overrides) -> PaymentInfo:
    """A PaymentInfo with sensible defaults, overridable per test."""
    defaults = {
        "payer": "0xpayer",
        "payee": "0xpayee",
        "amount_usd": 10.0,
        "asset": "USDC",
        "network": "eip155:8453",
        "pre_approval_expiry": 1000,
        "authorization_expiry": 1000,
        "refund_expiry": 1000,
    }
    defaults.update(overrides)
    return PaymentInfo(**defaults)


def test_payment_info_not_expired() -> None:
    """Authorization expiry in the future -> not expired."""
    info = _payment_info(authorization_expiry=200)
    assert info.is_expired(100) is False


def test_payment_info_expired() -> None:
    """Authorization expiry in the past -> expired."""
    info = _payment_info(authorization_expiry=50)
    assert info.is_expired(100) is True


def test_payment_info_can_refund() -> None:
    """Refund expiry in the past -> refundable."""
    info = _payment_info(refund_expiry=50)
    assert info.can_refund(100) is True


def test_payment_info_cannot_refund() -> None:
    """Refund expiry in the future -> not yet refundable."""
    info = _payment_info(refund_expiry=200)
    assert info.can_refund(100) is False


def test_payment_info_validate_fee_ok() -> None:
    """Fee within [min_fee_bps, max_fee_bps] -> valid."""
    info = _payment_info(min_fee_bps=0, max_fee_bps=100)
    assert info.validate_fee(50) is True


def test_payment_info_validate_fee_out_of_bounds() -> None:
    """Fee above max_fee_bps -> invalid."""
    info = _payment_info(min_fee_bps=0, max_fee_bps=100)
    assert info.validate_fee(150) is False


# --- BatchChannel -----------------------------------------------------------


def test_create_channel() -> None:
    """create_channel opens a channel with a 16-char id and 'open' status."""
    channel = create_channel("eip155:8453", "0x1234")
    assert len(channel.channel_id) == 16
    assert channel.status == "open"
    assert get_channel(channel.channel_id) is channel


def test_claim_channel_success() -> None:
    """Claiming 50 from a fresh channel succeeds with 50 remaining."""
    channel = create_channel("eip155:8453", "0x1234")
    res = claim_channel(channel.channel_id, 50)
    assert res["success"] is True
    assert res["remaining"] == 50


def test_claim_channel_overflow() -> None:
    """Claiming more than max_claims_per_batch fails."""
    channel = create_channel("eip155:8453", "0x1234")
    res = claim_channel(channel.channel_id, 150)
    assert res["success"] is False
    assert res["max_claims_per_batch"] == 100


def test_refund_channel() -> None:
    """Refunding after a 30 claim returns 70 and marks the channel refunded."""
    channel = create_channel("eip155:8453", "0x1234")
    claim_channel(channel.channel_id, 30)
    res = refund_channel(channel.channel_id)
    assert res["success"] is True
    assert res["refunded"] == 70
    stored = get_channel(channel.channel_id)
    assert stored is not None
    assert stored.status == "refunded"


def test_claim_after_refund_fails() -> None:
    """Claiming a refunded channel fails."""
    channel = create_channel("eip155:8453", "0x1234")
    refund_channel(channel.channel_id)
    res = claim_channel(channel.channel_id, 10)
    assert res["success"] is False
