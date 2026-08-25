"""Framework middleware adapters for x402 payments.

Gate HTTP endpoints behind x402 payments in any framework:

- :class:`X402Middleware` — ASGI middleware (FastAPI / Starlette / plain ASGI)::

      app.add_middleware(X402Middleware, free_paths={"/health", "/docs"})

- :class:`X402WSGIMiddleware` — plain WSGI middleware (any WSGI server)::

      app.wsgi_app = X402WSGIMiddleware(app.wsgi_app, free_paths={"/health"})

- :func:`init_flask` — one-line Flask wiring around the WSGI adapter::

      init_flask(app, free_paths={"/health"})

- :func:`require_payment` — per-route decorator (FastAPI/Starlette/Flask)::

      @app.get("/paid")
      @require_payment(asset="USDC", network="base", amount=0.001, max_deadline=300)
      async def paid_route(request: Request): ...

Payment flow: unpaid requests receive HTTP 402 with the spec-compliant
``PAYMENT-REQUIRED`` header (Base64 JSON) and JSON body. After paying via a
facilitator, the client replays the request with the ``X-Payment-ID`` header
(verified payment ids stay valid for ``PAYMENT_VALIDITY_SECONDS`` and are
bound to the caller when recorded with a ``caller_key``).

FastAPI/Starlette and Flask are OPTIONAL dependencies — this module imports
neither at module level, so it works in bare ASGI/WSGI deployments too.
"""
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Rug Munch Media LLC
#
# Standalone x402 implementation — https://git.rugmunch.io/RugMunchMedia/x402-py
# Licensed under MIT. See LICENSE.

from __future__ import annotations

import functools
import inspect
import json
import logging
from collections.abc import Awaitable, Callable, Iterable
from typing import Any

from x402 import (
    X402_NETWORK_WALLETS,
    X402_WALLET,
    X402Handler,
    _encode_payment_required_body,
    asset_to_atomic,
    build_payment_required,
    normalize_network_id,
)
from x402.scheme import X402Scheme

logger = logging.getLogger(__name__)

# ASGI/WSGI structural type aliases (framework-agnostic — no starlette/flask
# import at module level so the adapters work with either, or neither).
Scope = dict[str, Any]
Message = dict[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]
StartResponse = Callable[..., Any]
WSGIApp = Callable[[dict[str, Any], StartResponse], Iterable[bytes]]

# Spec headers used on the 402 response.
_HEADER_PAYMENT_REQUIRED = "PAYMENT-REQUIRED"
_HEADER_X_PAYMENT_REQUIRED = "X-Payment-Required"
_HEADER_X_PAYMENT_OPTIONS = "X-Payment-Options"


class PaymentRequired(Exception):  # noqa: N818 (x402 spec name: HTTP 402 Payment Required)
    """Raised when a payment is due and no framework adapter can render it.

    Carries the spec-compliant 402 ``body`` (JSON-serializable dict) and
    ``headers`` so callers can render the response in any framework.
    """

    def __init__(self, body: dict[str, Any], headers: dict[str, str]) -> None:
        super().__init__("payment required")
        self.body = body
        self.headers = headers


class _PaymentGate:
    """Shared pay/pass decision logic for the middleware and the decorator."""

    def __init__(
        self,
        *,
        asset: str,
        network: str,
        amount: float,
        max_deadline: int,
        operation: str,
        multi_asset: bool,
        handler: X402Handler | None,
    ) -> None:
        self.asset = asset
        self.network = network
        self.amount = amount
        self.max_deadline = max_deadline
        self.operation = operation
        self.multi_asset = multi_asset
        self.handler = handler or get_default_handler()

    def is_paid(self, payment_id: str, caller_key: str) -> bool:
        """True when the payment id is verified, unexpired and caller-bound."""
        if not payment_id:
            return False
        return self.handler.is_paid(payment_id, caller_key=caller_key)

    def payment_required_body(self, resource: str = "") -> dict[str, Any]:
        """Build the spec-compliant PaymentRequired body for this gate."""
        if self.multi_asset:
            return build_payment_required(self.operation or "api_access", resource=resource, multi_asset=True)
        network_id = normalize_network_id(self.network)
        accepts = [
            {
                "scheme": X402Scheme.EXACT.value,
                "network": self.network,
                "maxAmountRequired": str(asset_to_atomic(self.amount, self.asset, self.network)),
                "resource": resource or self.operation or "api_access",
                "description": self.operation or "x402 payment required",
                "mimeType": "application/json",
                "payToAddress": X402_NETWORK_WALLETS.get(network_id) or X402_WALLET,
                "asset": self.asset,
                "extra": {
                    "name": self.asset,
                    "version": "2",
                    "maxDeadline": self.max_deadline,
                },
            }
        ]
        return build_payment_required(self.operation or "api_access", resource=resource, accepts=accepts)

    @staticmethod
    def payment_required_headers(body: dict[str, Any]) -> dict[str, str]:
        """Spec headers for a 402 response carrying ``body``."""
        return {
            _HEADER_PAYMENT_REQUIRED: _encode_payment_required_body(body),
            _HEADER_X_PAYMENT_REQUIRED: "true",
            _HEADER_X_PAYMENT_OPTIONS: "x402",
        }


_default_handler: X402Handler | None = None


def get_default_handler() -> X402Handler:
    """Lazily create the process-wide default handler (payment records)."""
    global _default_handler
    if _default_handler is None:
        _default_handler = X402Handler()
    return _default_handler


def set_default_handler(handler: X402Handler | None) -> None:
    """Override (or clear, with ``None``) the process-wide default handler."""
    global _default_handler
    _default_handler = handler


def _is_free_path(path: str, free_paths: set[str]) -> bool:
    """Prefix match: ``/v1/sports`` frees ``/v1/sports/odds`` too."""
    return any(path.startswith(prefix) for prefix in free_paths)


def _operation_from_path(path: str) -> str:
    """Derive a default operation name from the request path (last segment)."""
    segments = [s for s in path.split("/") if s]
    return segments[-1] if segments else "api_access"


def _caller_key_from_scope(scope: Scope) -> str:
    """Best-effort caller identity (client IP) from an ASGI scope."""
    client = scope.get("client")
    if isinstance(client, tuple | list) and client:
        return str(client[0])
    return ""


class X402Middleware:
    """ASGI middleware gating routes behind x402 payments.

    Works with FastAPI, Starlette, or any plain ASGI app::

        app.add_middleware(X402Middleware, free_paths={"/health", "/docs"})

    Requests whose path starts with any ``free_paths`` entry pass through.
    Every other request must present a verified ``X-Payment-ID`` header;
    unpaid requests get an HTTP 402 response with the spec-compliant
    ``PAYMENT-REQUIRED`` header and JSON body.

    ``operation_for_path`` optionally maps a request path to a priced
    operation name from ``X402_PRICING`` (default: last path segment).
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        free_paths: set[str] | None = None,
        handler: X402Handler | None = None,
        multi_asset: bool = True,
        operation_for_path: Callable[[str], str] | None = None,
    ) -> None:
        self.app = app
        self.free_paths: set[str] = set(free_paths or set())
        self.multi_asset = multi_asset
        self._operation_for_path = operation_for_path
        self._gate = _PaymentGate(
            asset="USDC",
            network="base",
            amount=0.001,
            max_deadline=300,
            operation="",
            multi_asset=multi_asset,
            handler=handler,
        )

    def _is_free(self, path: str) -> bool:
        return _is_free_path(path, self.free_paths)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        path = str(scope.get("path", ""))
        if self._is_free(path):
            await self.app(scope, receive, send)
            return
        raw_headers = scope.get("headers") or []
        headers = {str(name.decode("latin-1")).lower(): str(value.decode("latin-1")) for name, value in raw_headers}
        caller_key = _caller_key_from_scope(scope)
        if self._gate.is_paid(headers.get("x-payment-id", ""), caller_key):
            await self.app(scope, receive, send)
            return
        operation = self._operation_for_path(path) if self._operation_for_path else _operation_from_path(path)
        self._gate.operation = operation
        body = self._gate.payment_required_body(resource=path)
        response_headers = self._gate.payment_required_headers(body)
        await send(
            {
                "type": "http.response.start",
                "status": 402,
                "headers": [
                    (b"content-type", b"application/json"),
                    *[
                        (str(k).lower().encode("latin-1"), str(v).encode("latin-1"))
                        for k, v in response_headers.items()
                    ],
                ],
            }
        )
        await send({"type": "http.response.body", "body": json.dumps(body).encode()})


class X402WSGIMiddleware:
    """Plain WSGI middleware gating routes behind x402 payments.

    Works with Flask and any WSGI framework/server::

        app.wsgi_app = X402WSGIMiddleware(app.wsgi_app, free_paths={"/health"})

    Same semantics as :class:`X402Middleware` (prefix ``free_paths``,
    ``X-Payment-ID`` replay, spec-compliant 402 responses).
    """

    def __init__(
        self,
        app: WSGIApp,
        *,
        free_paths: set[str] | None = None,
        handler: X402Handler | None = None,
        multi_asset: bool = True,
        operation_for_path: Callable[[str], str] | None = None,
    ) -> None:
        self.app = app
        self.free_paths: set[str] = set(free_paths or set())
        self.multi_asset = multi_asset
        self._operation_for_path = operation_for_path
        self._gate = _PaymentGate(
            asset="USDC",
            network="base",
            amount=0.001,
            max_deadline=300,
            operation="",
            multi_asset=multi_asset,
            handler=handler,
        )

    def _is_free(self, path: str) -> bool:
        return _is_free_path(path, self.free_paths)

    def __call__(self, environ: dict[str, Any], start_response: StartResponse) -> Iterable[bytes]:
        path = str(environ.get("PATH_INFO") or "/")
        if self._is_free(path):
            return self.app(environ, start_response)
        payment_id = str(environ.get("HTTP_X_PAYMENT_ID") or "")
        caller_key = str(environ.get("REMOTE_ADDR") or "")
        if self._gate.is_paid(payment_id, caller_key):
            return self.app(environ, start_response)
        operation = self._operation_for_path(path) if self._operation_for_path else _operation_from_path(path)
        self._gate.operation = operation
        body = self._gate.payment_required_body(resource=path)
        payload = json.dumps(body).encode()
        start_response(
            "402 Payment Required",
            [
                ("Content-Type", "application/json"),
                ("Content-Length", str(len(payload))),
                *self._gate.payment_required_headers(body).items(),
            ],
        )
        return [payload]


def init_flask(
    app: Any,
    *,
    free_paths: set[str] | None = None,
    handler: X402Handler | None = None,
    multi_asset: bool = True,
) -> Any:
    """Attach x402 payment gating to a Flask app (wraps ``app.wsgi_app``).

    Duck-typed on purpose: Flask is an optional dependency, so this does not
    import it. Returns the app for chaining.
    """
    app.wsgi_app = X402WSGIMiddleware(app.wsgi_app, free_paths=free_paths, handler=handler, multi_asset=multi_asset)
    return app


def _extract_request_headers(args: tuple[Any, ...], kwargs: dict[str, Any]) -> dict[str, str]:
    """Find request headers from handler arguments or the Flask context.

    Order: (1) any argument with a mapping-like ``.headers`` attribute
    (Starlette/FastAPI ``Request``, Flask ``request`` proxy, …); (2) an
    active Flask request context; (3) a plain mapping passed as the first
    positional argument.
    """
    for candidate in (*args, *kwargs.values()):
        headers = getattr(candidate, "headers", None)
        if headers is not None and hasattr(headers, "items"):
            return {str(k).lower(): str(v) for k, v in headers.items()}
    try:
        from flask import has_request_context, request
    except ImportError:
        pass
    else:
        if has_request_context():
            return {str(k).lower(): str(v) for k, v in request.headers.items()}
    if args and isinstance(args[0], dict):
        return {str(k).lower(): str(v) for k, v in args[0].items()}
    return {}


def _extract_caller_key(args: tuple[Any, ...], kwargs: dict[str, Any]) -> str:
    """Best-effort caller identity from the request object or Flask context."""
    for candidate in (*args, *kwargs.values()):
        client = getattr(candidate, "client", None)
        host = getattr(client, "host", None)
        if host:
            return str(host)
    try:
        from flask import has_request_context, request
    except ImportError:
        return ""
    if has_request_context():
        return str(request.remote_addr or "")
    return ""


def _build_402_response(
    body: dict[str, Any],
    headers: dict[str, str],
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
) -> Any:
    """Render the 402 in the caller's framework, or raise PaymentRequired.

    - Starlette/FastAPI request detected → ``JSONResponse(status_code=402)``
    - active Flask context → ``(body, 402, headers)`` view return tuple
    - otherwise → raise :class:`PaymentRequired` for the caller to render.
    """
    for candidate in (*args, *kwargs.values()):
        if type(candidate).__module__.startswith(("starlette", "fastapi")) and hasattr(candidate, "headers"):
            from starlette.responses import JSONResponse

            return JSONResponse(status_code=402, content=body, headers=headers)
    try:
        from flask import has_request_context
    except ImportError:
        pass
    else:
        if has_request_context():
            return body, 402, headers
    raise PaymentRequired(body=body, headers=headers)


def require_payment(
    asset: str = "USDC",
    network: str = "base",
    amount: float = 0.001,
    max_deadline: int = 300,
    *,
    operation: str = "",
    handler: X402Handler | None = None,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Decorator: gate a request handler behind an x402 payment.

    Works with FastAPI/Starlette handlers (a ``Request`` among the
    args/kwargs), Flask handlers (active request context), and plain
    callables receiving a headers mapping as the first argument. Paid
    requests pass through; unpaid requests get a framework-appropriate HTTP
    402 response carrying the spec-compliant ``PAYMENT-REQUIRED`` header (or
    :class:`PaymentRequired` is raised when no framework is detected).

    Args:
        asset: payment asset symbol (e.g. ``"USDC"``).
        network: payment network (legacy name or CAIP-2 id, e.g. ``"base"``).
        amount: server-side price in USD.
        max_deadline: payment validity window in seconds advertised to payers.
        operation: optional priced operation name from ``X402_PRICING``.
        handler: optional custom :class:`X402Handler` (shared default used otherwise).
    """

    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        gate = _PaymentGate(
            asset=asset,
            network=network,
            amount=amount,
            max_deadline=max_deadline,
            operation=operation,
            multi_asset=False,
            handler=handler,
        )

        def _check(args: tuple[Any, ...], kwargs: dict[str, Any]) -> tuple[bool, Any]:
            headers = _extract_request_headers(args, kwargs)
            caller_key = _extract_caller_key(args, kwargs)
            if gate.is_paid(headers.get("x-payment-id", ""), caller_key):
                return True, None
            body = gate.payment_required_body()
            return False, _build_402_response(body, gate.payment_required_headers(body), args, kwargs)

        if inspect.iscoroutinefunction(fn):

            @functools.wraps(fn)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                paid, response = _check(args, kwargs)
                if paid:
                    return await fn(*args, **kwargs)
                return response

            return async_wrapper

        @functools.wraps(fn)
        def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
            paid, response = _check(args, kwargs)
            if paid:
                return fn(*args, **kwargs)
            return response

        return sync_wrapper

    return decorator


__all__ = [
    "ASGIApp",
    "PaymentRequired",
    "WSGIApp",
    "X402Middleware",
    "X402WSGIMiddleware",
    "_caller_key_from_scope",
    "get_default_handler",
    "init_flask",
    "require_payment",
    "set_default_handler",
]
