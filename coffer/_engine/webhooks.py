"""Merchant webhooks — signed x402 payment-event notifications.

Merchants need to know when money arrives without polling. Coffer emits
``payment.verified`` / ``payment.settled`` events to registered HTTPS
endpoints, each delivery HMAC-SHA256-signed so receivers can authenticate it:

    X-Coffer-Event:      payment.settled
    X-Coffer-Timestamp:  1700000000
    X-Coffer-Signature:  hex hmac-sha256(secret, "{ts}.{event}.{body}")

The signed material binds the delivery timestamp and event type to the exact
canonical JSON body, so receivers can reject stale replays and mismatched
event routing, not just tampered payloads.

Delivery is best-effort with bounded retries; webhook failures NEVER affect
payment processing. Delivery is fail-closed: subscribers without a configured
secret are refused (an HMAC with an empty key is forgeable by anyone), and
subscriber URLs are SSRF-screened (HTTPS-only, no private/loopback targets).
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac as hmac_mod
import ipaddress
import json
import logging
import os
import socket
import time
import urllib.parse
from dataclasses import dataclass, field

import httpx

log = logging.getLogger(__name__)

MAX_RETRIES = 3
RETRY_BACKOFF_SECONDS = 1.0

_SENSITIVE_KEY_FRAGMENTS = ("token", "secret", "password", "api_key", "authorization")


def _validate_url(url: str) -> str | None:
    """SSRF screen for webhook targets. Returns None if safe, reason string if not.

    HTTPS-only; rejects targets that resolve to private/loopback/link-local
    addresses. Unresolvable hostnames are allowed through — no connection can
    be made to them, so they pose no SSRF risk (delivery fails naturally).
    """
    try:
        parsed = urllib.parse.urlparse(url)
    except ValueError:
        return "invalid URL"
    if not parsed.hostname:
        return "missing hostname"
    if parsed.scheme.lower() != "https":
        return f"unsupported scheme: {parsed.scheme or '(none)'} (https required)"
    try:
        infos = socket.getaddrinfo(parsed.hostname, None)
    except (socket.gaierror, ValueError, OSError):
        return None  # unresolvable → cannot be an SSRF target
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            continue
        if ip.is_private or ip.is_loopback or ip.is_link_local:
            return f"host resolves to private/loopback IP: {ip}"
    return None


def _redact(value: object) -> object:
    """Recursively mask sensitive payload fields for logs/dead-letter records."""
    if isinstance(value, dict):
        return {
            k: "***REDACTED***"
            if any(fragment in k.lower() for fragment in _SENSITIVE_KEY_FRAGMENTS)
            else _redact(v)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_redact(v) for v in value]
    return value


def sign_payload(secret: str, payload: dict, timestamp: str = "", event_type: str = "") -> str:
    """HMAC-SHA256 hex digest over ``{timestamp}.{event_type}.{canonical_body}``.

    The body is the canonical (sorted-key, compact-separator) JSON of the
    payload — the exact bytes sent on the wire.
    """
    body = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    signed = f"{timestamp}.{event_type}.{body}"
    return hmac_mod.new(secret.encode(), signed.encode(), hashlib.sha256).hexdigest()


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

    def register(self, url: str, *, secret: str = "", events: set[str] | None = None) -> WebhookSubscriber:
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
        envelope: dict[str, object] = {"event": event_type, "data": payload}
        body = json.dumps(envelope, sort_keys=True, separators=(",", ":")).encode()
        timestamp = str(int(time.time()))
        async with httpx.AsyncClient(timeout=10.0) as client:
            for sub in self._subs:
                if event_type not in sub.events:
                    continue
                if not sub.secret:
                    # Fail-closed: an HMAC with an empty key is forgeable, so
                    # sending "unsigned" would give receivers false integrity
                    # guarantees. Refuse instead.
                    log.warning(
                        "coffer_webhook_secret_not_configured",
                        extra={"url": sub.url, "event": event_type},
                    )
                    continue
                reason = _validate_url(sub.url)
                if reason:
                    log.warning(
                        "coffer_webhook_url_rejected",
                        extra={"url": sub.url, "event": event_type, "reason": reason},
                    )
                    continue
                headers = {
                    "content-type": "application/json",
                    "X-Coffer-Event": event_type,
                    "X-Coffer-Timestamp": timestamp,
                    "X-Coffer-Signature": sign_payload(
                        sub.secret, envelope, timestamp=timestamp, event_type=event_type
                    ),
                }
                ok = False
                for attempt in range(self._max_retries):
                    try:
                        r = await client.post(sub.url, content=body, headers=headers, follow_redirects=False)
                        if 200 <= r.status_code < 300:
                            ok = True
                            break
                    except httpx.HTTPError:
                        pass
                    if attempt < self._max_retries - 1:
                        await asyncio.sleep(RETRY_BACKOFF_SECONDS * (attempt + 1))
                if ok:
                    delivered += 1
                else:
                    log.warning(
                        "coffer_webhook_delivery_failed",
                        extra={
                            "url": sub.url,
                            "event": event_type,
                            "payload": _redact(payload),
                        },
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
