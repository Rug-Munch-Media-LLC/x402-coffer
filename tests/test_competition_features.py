"""Tests for competition-sourced features: scheme negotiation, extension policy, typed errors.

Covers the dynamic payment-scheme negotiation added for client-capability
discovery, the scheme-extension mutation policy that prevents an enriched
offer from weakening the baseline, and the typed ``VerifyError`` /
``SettleError`` exceptions raised during verify/settle orchestration.
"""

from __future__ import annotations

from coffer._engine.handler import SettleError, VerifyError
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
