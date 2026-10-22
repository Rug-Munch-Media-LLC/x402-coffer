"""HTTP adapters that gate requests behind x402 payments.

One gate (``gate.authorize_payment``), one challenge builder
(``challenge.build_attested_challenge``). Request: ``PAYMENT-SIGNATURE`` (v2) /
``X-PAYMENT`` (legacy) / ``X-Payment-ID`` (replay). 402: ``PAYMENT-REQUIRED`` +
machine-readable ``reason`` (+ ``Retry-After``). 2xx: ``PAYMENT-RESPONSE`` and
``X-PAYMENT-RESPONSE``, identical value.
"""

from __future__ import annotations

import functools
import inspect
import json
import logging
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse

from .body import HEADER_CONTENT_TYPE
from .challenge import build_attested_challenge
from .gate import GateDecision, GatePolicy, authorize_payment
from .store import PaymentStore, get_default_store

__all__ = [
    "HEADER_X_PAYMENT_ID",
    "ASGIApp",
    "X402Middleware",
    "caller_key_from_request",
    "caller_key_from_scope",
    "is_free_path",
    "require_payment",
]

log = logging.getLogger(__name__)

# Legacy replay header a paid client presents on subsequent requests.
HEADER_X_PAYMENT_ID = "X-Payment-ID"

Scope = dict[str, Any]
Message = dict[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]


def caller_key_from_scope(scope: Scope) -> str:
    """Best-effort caller identity (client IP) from an ASGI scope."""
    client = scope.get("client")
    return str(client[0]) if isinstance(client, tuple | list) and client else ""


def caller_key_from_request(request: Request | None) -> str:
    """Best-effort caller identity for FastAPI route handlers."""
    if request is not None and request.client is not None and request.client.host:
        return str(request.client.host)
    return ""


def is_free_path(path: str, free_paths: set[str]) -> bool:
    """Prefix match: ``/v1/health`` also frees ``/v1/health/details``."""
    return any(prefix and path.startswith(prefix) for prefix in free_paths)


def _request_headers(scope: Scope) -> dict[str, str]:
    def text(value: Any) -> str:
        return value.decode("latin-1") if isinstance(value, bytes | bytearray) else str(value)

    return {text(k).lower(): text(v) for k, v in (scope.get("headers") or [])}


def _injecting_send(send: Send, extra: Mapping[str, str]) -> Send:
    """Wrap ``send`` so a 2xx carries the settlement receipt headers."""

    async def _send(message: Message) -> None:
        if message.get("type") == "http.response.start" and extra:
            headers = list(message.get("headers") or [])
            headers.extend(
                (str(k).lower().encode("latin-1"), str(v).encode("latin-1")) for k, v in extra.items()
            )
            message = {**message, "headers": headers}
        await send(message)

    return _send


class X402Middleware:
    """ASGI middleware gating routes behind x402 payments."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        free_paths: set[str] | None = None,
        store: PaymentStore | None = None,
        policy: GatePolicy | None = None,
        operation: str = "api_access",
    ) -> None:
        self.app = app
        self.free_paths: set[str] = set(free_paths or set())
        self.store = store or get_default_store()
        self.policy = policy or GatePolicy(store=self.store, operation=operation)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        path = str(scope.get("path", ""))
        if is_free_path(path, self.free_paths):
            await self.app(scope, receive, send)
            return
        headers = _request_headers(scope)
        decision = await authorize_payment(
            headers, policy=self.policy, path=path, caller_key=caller_key_from_scope(scope)
        )
        if decision.paid:
            await self.app(scope, receive, _injecting_send(send, decision.headers))
            return
        server = headers.get("host", "localhost")
        await self._send_challenge(send, f"{scope.get('scheme', 'http')}://{server}{path}", decision)

    async def _send_challenge(self, send: Send, resource: str, decision: GateDecision) -> None:
        body, response_headers = build_attested_challenge(
            resource=resource, operation=self.policy.operation, reason=decision.reason
        )
        if decision.retry_after:
            response_headers["Retry-After"] = str(decision.retry_after)
        try:
            from .observability import record_challenge

            record_challenge(resource, decision.reason)
        except Exception:  # noqa: BLE001, S110 — observability never breaks payments
            pass
        encoded = [
            (str(k).lower().encode("latin-1"), str(v).encode("latin-1")) for k, v in response_headers.items()
        ]
        await send(
            {
                "type": "http.response.start",
                "status": decision.status,
                "headers": [(HEADER_CONTENT_TYPE.encode("latin-1"), b"application/json"), *encoded],
            }
        )
        await send({"type": "http.response.body", "body": json.dumps(body).encode()})


def require_payment(
    *,
    store: PaymentStore | None = None,
    policy: GatePolicy | None = None,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Gate a FastAPI route behind x402 (async handlers use the full gate).

    Sync handlers keep the legacy ``X-Payment-ID`` check — a synchronous call
    cannot await a settler, and pretending otherwise would serve unpaid.
    """

    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        def _refusal(request: Request | None, reason: str) -> JSONResponse:
            resource = str(request.url) if request is not None else ""
            body, headers = build_attested_challenge(resource=resource, reason=reason)
            return JSONResponse(status_code=402, content=body, headers=headers)

        if inspect.iscoroutinefunction(fn):

            @functools.wraps(fn)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                request = _find_request(args, kwargs)
                active = policy or GatePolicy(store=store or get_default_store())
                decision = await authorize_payment(
                    dict(request.headers) if request is not None else {},
                    policy=active,
                    path=str(request.url.path) if request is not None else "",
                    caller_key=caller_key_from_request(request),
                )
                return await fn(*args, **kwargs) if decision.paid else _refusal(request, decision.reason)

            return async_wrapper

        @functools.wraps(fn)
        def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
            request = _find_request(args, kwargs)
            active_store = store or get_default_store()
            payment_id = request.headers.get(HEADER_X_PAYMENT_ID.lower(), "") if request is not None else ""
            if request is not None and active_store.is_paid(
                payment_id, caller_key=caller_key_from_request(request)
            ):
                return fn(*args, **kwargs)
            return _refusal(request, "payment_required")

        return sync_wrapper

    return decorator


def _find_request(args: tuple[Any, ...], kwargs: dict[str, Any]) -> Request | None:
    """Locate a FastAPI/Starlette ``Request`` among handler args."""
    for candidate in (*args, *kwargs.values()):
        if isinstance(candidate, Request):
            return candidate
    return None
