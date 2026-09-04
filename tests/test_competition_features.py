"""Tests for competition-sourced features: scheme negotiation, extension policy, typed errors.

Covers the dynamic payment-scheme negotiation added for client-capability
discovery, the scheme-extension mutation policy that prevents an enriched
offer from weakening the baseline, and the typed ``VerifyError`` /
``SettleError`` exceptions raised during verify/settle orchestration.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from coffer._engine.handler import SettleError, VerifyError, X402Handler
from coffer._engine.scheme import negotiate_scheme, validate_scheme_extension

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
