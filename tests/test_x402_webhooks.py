"""Tests for merchant webhooks (HMAC signing, retry, fail-safety)."""

from __future__ import annotations

import hashlib
import hmac as hmac_mod

import httpx
import pytest

from coffer._engine.webhooks import (
    WebhookDispatcher,
    sign_payload,
)


def test_signature_is_hmac_sha256_of_exact_bytes() -> None:
    body = b'{"event":"payment.settled"}'
    expected = hmac_mod.new(b"sekrit", body, hashlib.sha256).hexdigest()
    assert sign_payload("sekrit", body) == expected


@pytest.mark.asyncio
async def test_delivery_signed_and_delivered() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200)

    d = WebhookDispatcher()
    d.register("https://hooks.test/x", secret="sekrit")
    transport = httpx.MockTransport(handler)
    # Patch the client used inside emit by monkeypatching module attr.
    import coffer._engine.webhooks as wh

    real_client = httpx.AsyncClient

    class _Patched(real_client):  # type: ignore[type-arg,misc]
        def __init__(self, **kw: object) -> None:
            super().__init__(transport=transport)

    monkey_patch = pytest.MonkeyPatch()
    monkey_patch.setattr(wh.httpx, "AsyncClient", _Patched)
    delivered = await d.emit("payment.settled", {"payment_id": "p1"})
    monkey_patch.undo()

    assert delivered == 1
    assert seen[0].headers["X-Coffer-Event"] == "payment.settled"
    expected_sig = sign_payload("sekrit", seen[0].content)
    assert seen[0].headers["X-Coffer-Signature"] == expected_sig


@pytest.mark.asyncio
async def test_event_filter_skips_non_matching() -> None:
    d = WebhookDispatcher()
    d.register("https://hooks.test/only-verify", events={"payment.verified"})
    delivered = await d.emit("payment.settled", {"payment_id": "p1"})
    assert delivered == 0


@pytest.mark.asyncio
async def test_retry_then_success() -> None:
    attempts: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        return httpx.Response(200 if len(attempts) >= 3 else 500)

    import coffer._engine.webhooks as wh

    transport = httpx.MockTransport(handler)
    real_client = httpx.AsyncClient

    class _Patched(real_client):  # type: ignore[type-arg,misc]
        def __init__(self, **kw: object) -> None:
            super().__init__(transport=transport)

    mp = pytest.MonkeyPatch()
    mp.setattr(wh.httpx, "AsyncClient", _Patched)
    mp.setattr(wh, "RETRY_BACKOFF_SECONDS", 0.01)
    d = WebhookDispatcher()
    d.register("https://hooks.test/flaky")
    delivered = await d.emit("payment.verified", {})
    mp.undo()
    assert len(attempts) == 3 and delivered == 1


@pytest.mark.asyncio
async def test_persistent_failure_never_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    import coffer._engine.webhooks as wh

    transport = httpx.MockTransport(handler)
    real_client = httpx.AsyncClient

    class _Patched(real_client):  # type: ignore[type-arg,misc]
        def __init__(self, **kw: object) -> None:
            super().__init__(transport=transport)

    mp = pytest.MonkeyPatch()
    mp.setattr(wh.httpx, "AsyncClient", _Patched)
    mp.setattr(wh, "RETRY_BACKOFF_SECONDS", 0.001)
    d = WebhookDispatcher(max_retries=2)
    d.register("https://hooks.test/dead")
    delivered = await d.emit("payment.settled", {})
    mp.undo()
    assert delivered == 0


def test_env_auto_registration(monkeypatch: pytest.MonkeyPatch) -> None:
    import coffer._engine.webhooks as wh

    monkeypatch.setattr(wh, "_dispatcher", None)
    monkeypatch.setenv("X402_WEBHOOK_URL", "https://merchant.example/hook")
    monkeypatch.setenv("X402_WEBHOOK_SECRET", "s3cret")
    d = wh.get_dispatcher()
    subs = d.subscribers()
    assert len(subs) == 1 and subs[0].url == "https://merchant.example/hook"
    assert subs[0].secret == "s3cret"
