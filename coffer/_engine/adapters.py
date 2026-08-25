"""HTTP adapters that gate requests behind x402 payments.

Owns the replay short-circuit (``X-Payment-ID``), the ASGI
:class:`X402Middleware`, and the ``require_payment`` decorator. Body/header
construction lives in :mod:`app.domain.x402.body`; the verified-payment store
lives in :mod:`app.domain.x402.store`.
"""

from __future__ import annotations

import functools
import inspect
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse

from .body import (
    HEADER_CONTENT_TYPE,
    payment_required_body,
    payment_required_headers,
)
from .store import PaymentStore, get_default_store

__all__ = [
    "ASGIApp",
    "HEADER_X_PAYMENT_ID",
    "X402Middleware",
    "caller_key_from_request",
    "caller_key_from_scope",
    "is_free_path",
    "require_payment",
]

log = logging.getLogger(__name__)

# Replay header a paid client presents on subsequent requests.
HEADER_X_PAYMENT_ID = "X-Payment-ID"


# ASGI structural types (framework-agnostic).
Scope = dict[str, Any]
Message = dict[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]


def caller_key_from_scope(scope: Scope) -> str:
    """Best-effort caller identity (client IP) from an ASGI scope."""
    client = scope.get("client")
    if isinstance(client, tuple | list) and client:
        return str(client[0])
    return ""


def caller_key_from_request(request: Request | None) -> str:
    """Best-effort caller identity for FastAPI route handlers."""
    if request is not None and request.client is not None and request.client.host:
        return str(request.client.host)
    return ""


def is_free_path(path: str, free_paths: set[str]) -> bool:
    """Prefix match: ``/v1/health`` also frees ``/v1/health/details``.

    Empty prefixes are rejected (they would match everything and disable
    the payment gate entirely).
    """
    return any(prefix and path.startswith(prefix) for prefix in free_paths)


class X402Middleware:
    """ASGI middleware gating routes behind x402 payments.

    Use with FastAPI / Starlette::

        app.add_middleware(X402Middleware, free_paths={"/health", "/docs"})

    Requests whose path starts with any ``free_paths`` entry pass through.
    Every other request must present a verified ``X-Payment-ID`` header;
    unpaid requests get HTTP 402 with the spec header set + JSON body.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        free_paths: set[str] | None = None,
        store: PaymentStore | None = None,
    ) -> None:
        self.app = app
        self.free_paths: set[str] = set(free_paths or set())
        self.store = store or get_default_store()

    def _is_free(self, path: str) -> bool:
        return is_free_path(path, self.free_paths)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        path = str(scope.get("path", ""))
        if self._is_free(path):
            await self.app(scope, receive, send)
            return
        raw_headers = scope.get("headers") or []
        headers = {
            str(name.decode("latin-1")).lower(): str(value.decode("latin-1"))
            for name, value in raw_headers
        }
        caller_key = caller_key_from_scope(scope)
        payment_id = headers.get(HEADER_X_PAYMENT_ID.lower(), "")
        if self.store.is_paid(payment_id, caller_key=caller_key):
            await self.app(scope, receive, send)
            return
        # Spec §2.2: ``resource`` is the FULL URL of the protected resource,
        # not the bare path (conformance sweep caught this).
        server = headers.get("host", "localhost")
        scheme = scope.get("scheme", "http")
        full_url = f"{scheme}://{server}{path}"
        body = payment_required_body(resource=full_url)
        response_headers = payment_required_headers(body)
        log.debug("x402_payment_required", extra={"path": path, "caller": caller_key})
        await send(
            {
                "type": "http.response.start",
                "status": 402,
                "headers": [
                    (HEADER_CONTENT_TYPE.encode("latin-1"), b"application/json"),
                    *[
                        (str(k).lower().encode("latin-1"), str(v).encode("latin-1"))
                        for k, v in response_headers.items()
                    ],
                ],
            }
        )
        await send({"type": "http.response.body", "body": json.dumps(body).encode()})


def _is_paid_for(request: Request, store: PaymentStore) -> bool:
    """True when the request carries a verified, caller-bound payment id."""
    payment_id = request.headers.get(HEADER_X_PAYMENT_ID.lower(), "")
    return store.is_paid(payment_id, caller_key=caller_key_from_request(request))


def require_payment(
    *,
    store: PaymentStore | None = None,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Decorator: gate a FastAPI route handler behind an x402 payment.

    Paid requests (valid ``X-Payment-ID``) pass through; unpaid requests get a
    spec-compliant ``JSONResponse(status_code=402)`` with the full header set.
    """

    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        def _gate(args: tuple[Any, ...], kwargs: dict[str, Any]) -> bool | JSONResponse:
            active_store = store or get_default_store()
            request = _find_request(args, kwargs)
            if request is not None and _is_paid_for(request, active_store):
                return True
            body = payment_required_body()
            log.debug("x402_payment_required", extra={"handler": getattr(fn, "__name__", "")})
            return JSONResponse(
                status_code=402, content=body, headers=payment_required_headers(body)
            )

        if inspect.iscoroutinefunction(fn):

            @functools.wraps(fn)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                result = _gate(args, kwargs)
                if result is True:
                    return await fn(*args, **kwargs)
                return result

            return async_wrapper

        @functools.wraps(fn)
        def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
            result = _gate(args, kwargs)
            if result is True:
                return fn(*args, **kwargs)
            return result

        return sync_wrapper

    return decorator


def _find_request(args: tuple[Any, ...], kwargs: dict[str, Any]) -> Request | None:
    """Locate a FastAPI/Starlette ``Request`` among handler args."""
    for candidate in (*args, *kwargs.values()):
        if isinstance(candidate, Request):
            return candidate
    return None
