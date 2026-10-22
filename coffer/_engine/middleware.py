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

Header convention (ONE implementation across the domain layer):

- pay     : ``PAYMENT-SIGNATURE`` (v2) or legacy ``X-PAYMENT`` — both decoded by
  ``payment_headers.decode_payment_header`` (no TODO, no second code path).
- replay  : ``X-Payment-ID: <verified payment id>`` (still accepted).
- offer   : ``PAYMENT-REQUIRED`` (attested Base64 body) + ``X-Payment-Required``
  + ``X-Payment-Options``.
- receipt : ``PAYMENT-RESPONSE`` and ``X-PAYMENT-RESPONSE``, identical value.

Every decision goes through ``gate.authorize_payment`` (attestation ->
budget reserve -> settle -> ledger) and every 402 through
``challenge.build_attested_challenge``.

Single source of truth: network/asset metadata comes from
``models.SUPPORTED_NETWORKS`` / ``models.SUPPORTED_ASSETS`` (canonical,
mirrors ``x402-py:x402/scheme.py``), pricing + wallets from ``service``
(``resolve_price_usd``, ``X402_NETWORK_WALLETS``) — so middleware, router and
service can never drift apart.
"""

from __future__ import annotations

from .adapters import (
    HEADER_X_PAYMENT_ID,
    ASGIApp,
    X402Middleware,
    caller_key_from_request,
    is_free_path,
    require_payment,
)
from .body import (
    HEADER_PAYMENT_REQUIRED,
    HEADER_X_PAYMENT_OPTIONS,
    HEADER_X_PAYMENT_REQUIRED,
    LEGACY_TO_CAIP2,
    NETWORK_ASSETS,
    X402_VERSION,
    asset_to_atomic,
    build_multi_asset_accepts,
    build_payment_required,
    normalize_network_id,
    payment_required_body,
    payment_required_headers,
    resolve_price_usd,
)
from .challenge import build_attested_challenge
from .gate import GatePolicy, authorize_payment
from .store import (
    PAYMENT_VALIDITY_SECONDS,
    PaymentStore,
    get_default_store,
    set_default_store,
)

__all__ = [
    "HEADER_PAYMENT_REQUIRED",
    "HEADER_X_PAYMENT_ID",
    "HEADER_X_PAYMENT_OPTIONS",
    "HEADER_X_PAYMENT_REQUIRED",
    "LEGACY_TO_CAIP2",
    "NETWORK_ASSETS",
    "PAYMENT_VALIDITY_SECONDS",
    "X402_VERSION",
    "ASGIApp",
    "GatePolicy",
    "PaymentStore",
    "X402Middleware",
    "asset_to_atomic",
    "authorize_payment",
    "build_attested_challenge",
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
