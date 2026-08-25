"""Framework adapters beyond FastAPI/ASGI — Flask (WSGI) + Litestar.

* :func:`init_flask` wraps any Flask/WSGI app with the same 402 gate the ASGI
  middleware applies: free paths pass through; a valid caller-bound
  ``X-Payment-ID`` passes through; everything else gets a spec-canonical 402
  (``PAYMENT-REQUIRED`` base64 header + JSON body).
* :func:`litestar_middleware` returns Coffer's standard ASGI middleware
  configured for Litestar (Litestar is ASGI-native, so the same gate applies).

Honest scope: the WSGI gate validates replay tokens only (no on-chain calls);
verification still happens via ``POST /v1/x402/verify`` exactly as in ASGI.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from typing import Any

from coffer._engine.body import payment_required_headers
from coffer._engine.store import PaymentStore

__all__ = ["Flask402Gate", "init_flask", "litestar_middleware"]


def _wsgi_402(
    start_response: Callable[..., Any],
    *,
    resource_url: str,
    operation: str,
) -> list[Any]:
    """Emit a spec-canonical 402 over WSGI."""
    from coffer._engine.body import payment_required_body

    body = payment_required_body(resource=resource_url, operation=operation)
    hdrs = payment_required_headers(body)
    payload = json.dumps(body).encode()
    headers = [(k.lower(), v) for k, v in hdrs.items()]
    headers.append(("content-type", "application/json"))
    headers.append(("content-length", str(len(payload))))
    start_response("402 Payment Required", headers)
    return [payload]


class Flask402Gate:
    """WSGI middleware applying the x402 gate to a Flask (or any WSGI) app."""

    def __init__(
        self,
        wsgi_app: Callable[..., Any],
        *,
        free_paths: set[str] | None = None,
        store: PaymentStore | None = None,
        pay_to_address: str = "",
        network: str = "eip155:8453",
        asset: str = "USDC",
        max_amount_required: str = "1000",
        description: str = "Paid API access",
    ) -> None:
        self.wsgi_app = wsgi_app
        self.free_paths = {p for p in (free_paths or set()) if p}
        self.store = store if store is not None else PaymentStore()
        self.pay_to_address = pay_to_address
        self.network = network
        self.asset = asset
        self.max_amount_required = max_amount_required
        self.description = description

    def __call__(self, environ: dict[str, Any], start_response: Callable[..., Any]) -> Any:
        path = environ.get("PATH_INFO", "")
        # Free paths pass through (empty prefixes rejected — same as ASGI gate).
        if any(p and path.startswith(p) for p in self.free_paths):
            return self.wsgi_app(environ, start_response)
        payment_id = environ.get("HTTP_X_PAYMENT_ID", "").strip()
        if payment_id:
            caller_key = environ.get("REMOTE_ADDR", "")
            if self.store.is_paid(payment_id, caller_key):
                return self.wsgi_app(environ, start_response)
        host = environ.get("HTTP_HOST", "localhost")
        scheme = environ.get("wsgi.url_scheme", "http")
        return _wsgi_402(
            start_response,
            resource_url=f"{scheme}://{host}{path}",
            operation=path.strip("/") or "root",
        )


def init_flask(
    app: Any,
    *,
    free_paths: set[str] | None = None,
    store: PaymentStore | None = None,
    **gate_kw: Any,
) -> Flask402Gate:
    """Wrap a Flask app in-place: ``app.wsgi_app = init_flask(app, ...)``.

    Returns the gate so callers can record verified payments on it
    (``gate.store.record(...)``) after their own verify flow.
    """
    gate = Flask402Gate(app.wsgi_app, free_paths=free_paths, store=store, **gate_kw)
    app.wsgi_app = gate  # type: ignore[method-assign]
    return gate


def litestar_middleware(**mw_kw: Any) -> Any:
    """Coffer's ASGI gate configured for Litestar (ASGI-native framework).

    Usage::

        from litestar import Litestar
        from coffer._engine.adapters_wsgi import litestar_middleware

        app = Litestar(middleware=[litestar_middleware(free_paths={"/health"})])
    """
    from functools import partial

    from coffer._engine.adapters import X402Middleware

    return partial(X402Middleware, **mw_kw)


def _iter_environ_keys(environ: dict[str, Any]) -> Iterable[str]:
    yield from environ.keys()
