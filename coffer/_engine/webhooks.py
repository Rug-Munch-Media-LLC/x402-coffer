"""Merchant webhooks — signed x402 payment-event notifications.

Merchants need to know when money arrives without polling. Coffer emits
``payment.verified`` / ``payment.settled`` events to registered HTTPS
endpoints, each delivery HMAC-SHA256-signed so receivers can authenticate it:

    X-Coffer-Event:  payment.settled
    X-Coffer-Signature: hex hmac-sha256(secret, raw_body)

Delivery is best-effort with bounded retries; webhook failures NEVER affect
payment processing.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac as hmac_mod
import json
import logging
import os
import time
from dataclasses import dataclass, field

import httpx

log = logging.getLogger(__name__)

MAX_RETRIES = 3
RETRY_BACKOFF_SECONDS = 1.0


def sign_payload(secret: str, raw_body: bytes) -> str:
    """HMAC-SHA256 hex digest of the exact bytes sent."""
    return hmac_mod.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()


@dataclass
class WebhookSubscriber:
    url: str
    secret: str = ""
    events: set[str] = field(default_factory=lambda: {"payment.verified", "payment.settled"})


class WebhookDispatcher:
    """Registry + async emitter. Fail-safe by contract."""

    def __init__(self, *, max_retries: int = MAX_RETRIES) -> None:
        self._subs: list[WebhookSubscriber] = []
        self._max_retries = int(max_retries)

    def register(
        self, url: str, *, secret: str = "", events: set[str] | None = None
    ) -> WebhookSubscriber:
        sub = WebhookSubscriber(
            url=url,
            secret=secret,
            events=events or {"payment.verified", "payment.settled"},
        )
        self._subs.append(sub)
        return sub

    def subscribers(self) -> list[WebhookSubscriber]:
        return list(self._subs)

    async def emit(self, event_type: str, payload: dict[str, object]) -> int:
        """Fan out to matching subscribers. Returns delivered count; never raises."""
        delivered = 0
        if not self._subs:
            return 0
        body = json.dumps({"event": event_type, "data": payload}).encode()
        async with httpx.AsyncClient(timeout=10.0) as client:
            for sub in self._subs:
                if event_type not in sub.events:
                    continue
                headers = {
                    "content-type": "application/json",
                    "X-Coffer-Event": event_type,
                }
                if sub.secret:
                    headers["X-Coffer-Signature"] = sign_payload(sub.secret, body)
                ok = False
                for attempt in range(self._max_retries):
                    try:
                        r = await client.post(sub.url, content=body, headers=headers)
                        if 200 <= r.status_code < 300:
                            ok = True
                            break
                    except httpx.HTTPError:  # noqa: PERF203 — retry loop
                        pass
                    if attempt < self._max_retries - 1:
                        await asyncio.sleep(RETRY_BACKOFF_SECONDS * (attempt + 1))
                if ok:
                    delivered += 1
                else:
                    log.warning(
                        "coffer_webhook_delivery_failed",
                        extra={"url": sub.url, "event": event_type},
                    )
        return delivered


_dispatcher: WebhookDispatcher | None = None


def get_dispatcher() -> WebhookDispatcher:
    """Global dispatcher; auto-registers env-configured default endpoint."""
    global _dispatcher
    if _dispatcher is None:
        _dispatcher = WebhookDispatcher()
        url = os.getenv("X402_WEBHOOK_URL", "").strip()
        if url:
            _dispatcher.register(url, secret=os.getenv("X402_WEBHOOK_SECRET", "").strip())
    return _dispatcher


def emit_payment_event(event_type: str, **data: object) -> None:
    """Sync-safe fire-and-forget used from sync contexts (never raises).

    Schedules delivery on the running loop when one exists; drops silently
    otherwise (webhooks are strictly best-effort).
    """
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(get_dispatcher().emit(event_type, dict(data)))
    except RuntimeError:
        pass  # no running loop — skip (observability ledger still has the record)


def webhook_delivery_deadline_ts() -> float:
    """Utility for tests/ops: monotonic deadline helper."""
    return time.time()
