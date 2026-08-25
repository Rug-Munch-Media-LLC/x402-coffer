"""x402 payment middleware — spec-compliant HTTP 402 gating (facade).

Thin re-export facade so ``from coffer._engine.middleware import
require_payment, X402Middleware, PaymentStore, build_payment_required`` keeps
resolving. The implementation is split across:

- :mod:`app.domain.x402.body`     — 402 offer body + header builders.
- :mod:`app.domain.x402.store`    — caller-bound, TTL'd verified-payment store.
- :mod:`app.domain.x402.adapters` — ASGI middleware + router decorator.

Implements the OFFICIAL x402 402-flow per the repo's X402-SPEC-CONTRACT §2 and
the HTTP transport specs: unpaid requests for a gated path receive::

      HTTP/1.1 402 Payment Required
      Content-Type: application/json
      PAYMENT-REQUIRED: <base64(JSON body)>
      X-Payment-Required: true
      X-Payment-Options: x402

and a JSON body ``{x402Version: 1, accepts: [...]}`` (multi-asset, CAIP-2
network ids, atomic ``maxAmountRequired`` strings, asset SYMBOL +
``extra.contract`` address, configured ``payToAddress``). After paying, the
client replays with ``X-Payment-ID`` checked against a caller-bound, TTL'd
store (mirrors the canonical ``X402Handler`` semantics).

Header convention (fleet-canonical, ONE convention across the domain layer):

- replay  : ``X-Payment-ID: <verified payment id>`` (contract §2.3)
- offer   : ``PAYMENT-REQUIRED`` (base64 body) + ``X-Payment-Required: true``
  + ``X-Payment-Options: x402`` (contract §2.1)

The legacy ``X-Payment: base64({tx_hash, signature})`` client header and the
v2 ``PAYMENT-SIGNATURE`` header are NOT consumed on the v1 wire — payment
proofs go through ``POST /v1/x402/verify`` and replay uses ``X-Payment-ID``.
TODO(v2): additive — accept ``PAYMENT-SIGNATURE`` payloads once the gateway
migrates to x402Version=2 (see ``models.X402_VERSION``).

Single source of truth: network/asset metadata comes from
``models.SUPPORTED_NETWORKS`` / ``models.SUPPORTED_ASSETS`` (canonical,
mirrors ``x402-py:x402/scheme.py``), pricing + wallets from ``service``
(``resolve_price_usd``, ``X402_NETWORK_WALLETS``) — so middleware, router and
service can never drift apart.
"""

from __future__ import annotations

from .adapters import (
    HEADER_X_PAYMENT_ID,  # noqa: F401  (re-export)
    ASGIApp,  # noqa: F401  (re-export)
    X402Middleware,  # noqa: F401  (re-export)
    caller_key_from_request,  # noqa: F401  (re-export)
    is_free_path,  # noqa: F401  (re-export)
    require_payment,  # noqa: F401  (re-export)
)
from .body import (
    HEADER_PAYMENT_REQUIRED,  # noqa: F401  (re-export)
    HEADER_X_PAYMENT_OPTIONS,  # noqa: F401  (re-export)
    HEADER_X_PAYMENT_REQUIRED,  # noqa: F401  (re-export)
    LEGACY_TO_CAIP2,  # noqa: F401  (re-export)
    NETWORK_ASSETS,  # noqa: F401  (re-export)
    X402_VERSION,  # noqa: F401  (re-export)
    asset_to_atomic,  # noqa: F401  (re-export)
    build_multi_asset_accepts,  # noqa: F401  (re-export)
    build_payment_required,  # noqa: F401  (re-export)
    normalize_network_id,  # noqa: F401  (re-export)
    payment_required_body,  # noqa: F401  (re-export)
    payment_required_headers,  # noqa: F401  (re-export)
    resolve_price_usd,  # noqa: F401  (re-export)
)
from .store import (  # noqa: F401  (re-export)
    PAYMENT_VALIDITY_SECONDS,
    PaymentStore,
    get_default_store,
    set_default_store,
)

__all__ = [
    "ASGIApp",
    "HEADER_PAYMENT_REQUIRED",
    "HEADER_X_PAYMENT_ID",
    "HEADER_X_PAYMENT_OPTIONS",
    "HEADER_X_PAYMENT_REQUIRED",
    "LEGACY_TO_CAIP2",
    "NETWORK_ASSETS",
    "PAYMENT_VALIDITY_SECONDS",
    "PaymentStore",
    "X402Middleware",
    "X402_VERSION",
    "asset_to_atomic",
    "build_multi_asset_accepts",
    "build_payment_required",
    "caller_key_from_request",
    "get_default_store",
    "is_free_path",
    "normalize_network_id",
    "payment_required_body",
    "payment_required_headers",
    "require_payment",
    "resolve_price_usd",
    "set_default_store",
]
