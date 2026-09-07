# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Rug Munch Media LLC
"""PART B audit fixes (2026-09-06-x402-coffer-audit.md): hermetic tests.

Covers #53 offline-mode guard, #54 TTL constants, #57 env casing/deprecated
aliases, #61 trusted-proxy payer, #62 single quote source, #73 finalized
commitment state machine.
"""

from __future__ import annotations

import logging
from typing import Any, ClassVar, Self

import pytest

# ── #53 offline-mode guard ───────────────────────────────────────────────────


def test_offline_mode_requires_dev_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    import importlib
    import os

    os.environ.pop("X402_OFFLINE_MODE", None)
    os.environ.pop("X402_DEV_MODE", None)
    mod = importlib.reload(importlib.import_module("coffer._engine.facilitator_config"))
    assert mod.X402_OFFLINE_MODE is False

    os.environ["X402_OFFLINE_MODE"] = "1"
    mod = importlib.reload(importlib.import_module("coffer._engine.facilitator_config"))
    # offline flag WITHOUT dev mode -> ignored (fail closed)
    assert mod.X402_OFFLINE_MODE is False

    os.environ["X402_DEV_MODE"] = "1"
    mod = importlib.reload(importlib.import_module("coffer._engine.facilitator_config"))
    assert mod.X402_OFFLINE_MODE is True
    os.environ.pop("X402_OFFLINE_MODE", None)
    os.environ.pop("X402_DEV_MODE", None)
    importlib.reload(importlib.import_module("coffer._engine.facilitator_config"))


def test_offline_mode_without_dev_mode_logs_warning(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    import importlib
    import os

    os.environ["X402_OFFLINE_MODE"] = "1"
    os.environ.pop("X402_DEV_MODE", None)
    with caplog.at_level(logging.WARNING, logger="coffer._engine.facilitator_config"):
        importlib.reload(importlib.import_module("coffer._engine.facilitator_config"))
    assert any("offline_mode_ignored" in r.message for r in caplog.records)
    os.environ.pop("X402_OFFLINE_MODE", None)
    importlib.reload(importlib.import_module("coffer._engine.facilitator_config"))


def test_settings_offline_mode_requires_dev_mode() -> None:
    from coffer._engine.config import X402Settings

    s = X402Settings.from_env({"X402_OFFLINE_MODE": "1"})
    assert s.offline_mode is False  # no dev mode -> ignored
    s = X402Settings.from_env({"X402_OFFLINE_MODE": "1", "X402_DEV_MODE": "1"})
    assert s.offline_mode is True


# ── #54 TTL constants ────────────────────────────────────────────────────────


def test_ttl_defaults_are_single_sourced() -> None:
    from coffer._engine import config
    from coffer._engine.facilitator_config import PAYMENT_VALIDITY_SECONDS

    # store TTL and router TTL must agree on the same default
    assert config.DEFAULT_PAYMENT_TTL == 3600
    assert config.DEFAULT_NONCE_TTL == 86400
    assert PAYMENT_VALIDITY_SECONDS == config.DEFAULT_PAYMENT_TTL


def test_payment_ttl_env_override() -> None:
    from coffer._engine.config import X402Settings

    s = X402Settings.from_env({"X402_PAYMENT_TTL": "120"})
    assert s.payment_ttl == 120


# ── #57 env casing / deprecated aliases ─────────────────────────────────────


def test_burst_env_casing_normalized() -> None:
    from coffer._engine.config import X402Settings

    s = X402Settings.from_env({"X402_BURST_LIMIT": "9", "X402_BURST_WINDOW": "120"})
    assert s.burst_limit == 9
    assert s.burst_window == 120


def test_deprecated_alias_warns_once(caplog: pytest.LogCaptureFixture) -> None:
    from coffer._engine import config
    from coffer._engine.config import X402Settings

    config._deprecated_env_warned.clear()
    with caplog.at_level(logging.WARNING, logger="coffer._engine.config"):
        X402Settings.from_env({"X402_burst_limit": "7"})
        X402Settings.from_env({"X402_burst_limit": "7"})  # second use: no warn
    warns = [r for r in caplog.records if "deprecated" in r.message]
    assert len(warns) == 1
    config._deprecated_env_warned.clear()


def test_coffer_alias_accepted_but_deprecated(caplog: pytest.LogCaptureFixture) -> None:
    from coffer._engine import config
    from coffer._engine.config import X402Settings

    config._deprecated_env_warned.clear()
    with caplog.at_level(logging.WARNING, logger="coffer._engine.config"):
        s = X402Settings.from_env({"COFFER_BURST_LIMIT": "3"})
    assert s.burst_limit == 3
    assert any("deprecated" in r.message for r in caplog.records)
    config._deprecated_env_warned.clear()


# ── #61 trusted proxy payer ──────────────────────────────────────────────────


class _FakeRequest:
    def __init__(self, headers: dict[str, str] | None = None) -> None:
        self.headers = headers or {}


def test_trusted_payer_prefers_server_signer() -> None:
    from coffer._engine.router import _trusted_payer

    signer = "0x" + "a" * 40
    req = _FakeRequest({"x-payer": "0x" + "b" * 40})
    assert _trusted_payer(req, {"signer": signer}) == signer


def test_trusted_payer_rejects_client_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    from coffer._engine.router import _trusted_payer

    # no trusted proxy configured -> header ignored even if present
    monkeypatch.delenv("X402_TRUSTED_PROXY", raising=False)
    assert _trusted_payer(_FakeRequest({"x-payer": "0x" + "b" * 40}), {}) == ""


def test_trusted_payer_accepts_header_only_with_trusted_proxy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from coffer._engine.router import _trusted_payer

    monkeypatch.setenv("X402_TRUSTED_PROXY", "1")
    addr = "0x" + "c" * 40
    assert _trusted_payer(_FakeRequest({"x-payer": addr}), {}) == addr


def test_trusted_payer_validates_address_format(monkeypatch: pytest.MonkeyPatch) -> None:
    from coffer._engine.router import _trusted_payer

    monkeypatch.setenv("X402_TRUSTED_PROXY", "1")
    # malformed addresses are rejected even from a trusted proxy
    assert _trusted_payer(_FakeRequest({"x-payer": "0x123"}), {}) == ""
    assert _trusted_payer(_FakeRequest({"x-payer": "not-an-address"}), {}) == ""
    assert _trusted_payer(_FakeRequest({"x-payer": "0x" + "z" * 40}), {}) == ""


# ── #62 single quote source ──────────────────────────────────────────────────


def test_resolve_quote_returns_atomic() -> None:
    from coffer._engine.facilitator_orchestration import _resolve_quote

    atomic, reason = _resolve_quote(1.0, "USDC", "eip155:8453")
    assert atomic is not None and atomic > 0
    assert reason == ""


def test_resolve_quote_fails_closed_on_unknown_asset() -> None:
    from coffer._engine.facilitator_orchestration import _resolve_quote

    atomic, reason = _resolve_quote(1.0, "NOPE", "eip155:8453")
    assert atomic is None
    assert reason


def test_assert_quote_consistency_detects_mismatch() -> None:
    from coffer._engine.facilitator_orchestration import _assert_quote_consistency

    assert _assert_quote_consistency(100, 100, "p1") == ""
    assert _assert_quote_consistency(None, 100, "p1") == ""
    assert "mismatch" in _assert_quote_consistency(100, 200, "p1")


def test_settle_uses_single_quote_source(monkeypatch: pytest.MonkeyPatch) -> None:
    """The settle payload amount must come through _resolve_quote."""
    import coffer._engine.facilitator_orchestration as orch

    captured: dict[str, Any] = {}

    class _Resp:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, Any]:
            return {"success": True, "transaction": "0xtx"}

    class _Client:
        def __init__(self, *a: Any, **k: Any) -> None:
            pass

        async def __aenter__(self) -> Self:
            return self

        async def __aexit__(self, *a: object) -> None:
            return None

        async def post(self, url: str, json: Any) -> _Resp:
            captured.update(json)
            return _Resp()

    monkeypatch.setattr(orch.httpx, "AsyncClient", _Client)

    class _Health:
        def is_healthy(self, f: Any) -> bool:
            return True

        def health_score(self, f: Any) -> int:
            return 1

        def mark_success(self, f: Any, t: Any = None) -> None:
            return None

    class _Fac:
        name = "t"
        settle_url = "http://x/settle"

    class _Router:
        facilitators: ClassVar[list[Any]] = [_Fac()]
        health = _Health()
        self_hosted = False

        def _impl_for(self, n: str) -> None:
            return None

    monkeypatch.setattr(orch, "X402_OFFLINE_MODE", False)
    res = (
        __import__("asyncio")
        .get_event_loop_policy()
        .new_event_loop()
        .run_until_complete(orch.settle(_Router(), "pid", "0xtx", "eip155:8453", "USDC", 1.0))
    )
    assert captured["amount"].isdigit()
    assert int(captured["amount"]) > 0
    assert res["success"] is True


# ── #73 finalized commitment ────────────────────────────────────────────────


def test_status_transitions_verify_settle_finalized() -> None:
    from coffer._engine.store import PaymentStatus, PaymentStore

    store = PaymentStore()
    store.record(payment_id="p73", amount_usd=1.0)
    rec = store.get("p73")
    assert rec is not None and rec["status"] == PaymentStatus.VERIFIED

    assert store.mark_settled("p73") is True
    assert store.get("p73")["status"] == PaymentStatus.SETTLED

    assert store.finalize("p73") is True
    assert store.get("p73")["status"] == PaymentStatus.FINALIZED


def test_finalize_requires_settled_state() -> None:
    from coffer._engine.store import PaymentStatus, PaymentStore

    store = PaymentStore()
    store.record(payment_id="q", amount_usd=1.0)
    # VERIFIED -> FINALIZED directly is rejected
    assert store.finalize("q") is False
    assert store.get("q")["status"] == PaymentStatus.VERIFIED


def test_verify_rejected_after_finalize() -> None:
    from coffer._engine.store import PaymentStore

    store = PaymentStore()
    store.record(payment_id="pfin", amount_usd=1.0)
    store.mark_settled("pfin")
    store.finalize("pfin")
    with pytest.raises(ValueError, match="FINALIZED"):
        store.record(payment_id="pfin", amount_usd=1.0)


def test_settle_outcome_finalizes_record() -> None:
    from coffer._engine.store import PaymentStatus, PaymentStore

    store = PaymentStore()
    store.record(payment_id="pidem", amount_usd=1.0)
    store.record_settle_outcome(payment_id="pidem", outcome={"success": True, "transaction": "0xtx"})
    assert store.get("pidem")["status"] == PaymentStatus.FINALIZED
    assert store.get_settle_outcome("pidem") is not None
