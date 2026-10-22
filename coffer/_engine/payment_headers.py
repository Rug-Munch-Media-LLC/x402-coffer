"""Coffer — x402 wire-header codec (v2 ``PAYMENT-*`` + legacy ``X-PAYMENT`` aliases).

Parity reference (Pry, the more advanced implementation):
    - ``pryscraper/src/pry/payments/x402_middleware/enforcement.py`` L70-71 —
      402 replies carry ``PAYMENT-REQUIRED: <Base64(JSON PaymentRequired)>`` and
      2xx replies carry ``PAYMENT-RESPONSE: <Base64(JSON settlement receipt)>``.
    - same file L146 — the client retry header is read as ``payment-signature``.

Coffer gap this closes: ``coffer/_engine/middleware.py`` L33-35 documents v2
``PAYMENT-SIGNATURE`` as *not consumed* on the v1 wire (``TODO(v2)``), so a
spec-current client that paid and retried got a second 402 instead of the
resource. Coffer also emitted only ``PAYMENT-RESPONSE``; agents written against
the older ``X-PAYMENT-RESPONSE`` name saw no receipt.

Contract:
    * :func:`decode_payment_header` accepts ``PAYMENT-SIGNATURE`` (v2) first,
      then the legacy ``X-PAYMENT`` alias. Malformed input raises
      :class:`PaymentHeaderError` (status 402) — never a silent 200.
    * :func:`settlement_header_pairs` emits ``PAYMENT-RESPONSE`` *and*
      ``X-PAYMENT-RESPONSE`` with the identical Base64 value: one truth, two
      names, so neither client generation diverges.
"""

from __future__ import annotations

import base64
import binascii
import json
from collections.abc import Mapping
from typing import Any

HEADER_PAYMENT_REQUIRED = "PAYMENT-REQUIRED"
HEADER_PAYMENT_RESPONSE = "PAYMENT-RESPONSE"
HEADER_PAYMENT_SIGNATURE = "PAYMENT-SIGNATURE"
HEADER_X_PAYMENT = "X-PAYMENT"
HEADER_X_PAYMENT_RESPONSE = "X-PAYMENT-RESPONSE"

# Canonical precedence: v2 scheme header wins over the legacy alias.
_PAYMENT_HEADERS: tuple[str, ...] = (HEADER_PAYMENT_SIGNATURE, HEADER_X_PAYMENT)


class PaymentHeaderError(ValueError):
    """A payment header was present but unusable. Callers answer 402."""

    status_code = 402

    def __init__(self, reason: str, header: str = "") -> None:
        self.reason = reason
        self.header = header
        super().__init__(f"{reason} ({header})" if header else reason)


def _lookup(headers: Mapping[str, str], name: str) -> str:
    """Case-insensitive header lookup (ASGI/WSGI/plain dicts all differ)."""
    direct = headers.get(name)
    if direct:
        return str(direct).strip()
    lowered = name.lower()
    for key, value in headers.items():
        if str(key).lower() == lowered and value:
            return str(value).strip()
    return ""


def decode_payment_header(headers: Mapping[str, str]) -> dict[str, Any]:
    """Return the decoded x402 payment payload carried by *headers*.

    Raises :class:`PaymentHeaderError` when no payment header is present, the
    Base64 is corrupt, or the payload is not a JSON object.
    """
    for name in _PAYMENT_HEADERS:
        raw = _lookup(headers, name)
        if not raw:
            continue
        try:
            decoded = base64.b64decode(raw, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise PaymentHeaderError("payment header is not valid base64", name) from exc
        try:
            payload = json.loads(decoded)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PaymentHeaderError("payment header is not valid JSON", name) from exc
        if not isinstance(payload, dict):
            raise PaymentHeaderError("payment payload must be a JSON object", name)
        return payload
    raise PaymentHeaderError("missing payment header")


def has_payment_header(headers: Mapping[str, str]) -> bool:
    """True when either accepted payment header name carries a value."""
    return any(_lookup(headers, name) for name in _PAYMENT_HEADERS)


def encode_payment_required(body: Mapping[str, Any]) -> str:
    """Compact, Base64 ``PAYMENT-REQUIRED`` value (stable key order for caches)."""
    raw = json.dumps(body, separators=(",", ":"), sort_keys=True).encode()
    return base64.b64encode(raw).decode()


def settlement_header_pairs(receipt: Mapping[str, Any]) -> dict[str, str]:
    """Return both receipt header names carrying one identical Base64 value."""
    raw = json.dumps(dict(receipt), separators=(",", ":"), sort_keys=True, default=str).encode()
    encoded = base64.b64encode(raw).decode()
    return {HEADER_PAYMENT_RESPONSE: encoded, HEADER_X_PAYMENT_RESPONSE: encoded}


def decode_settlement_header(headers: Mapping[str, str]) -> dict[str, Any] | None:
    """Inverse of :func:`settlement_header_pairs` (client-side receipt check)."""
    for name in (HEADER_PAYMENT_RESPONSE, HEADER_X_PAYMENT_RESPONSE):
        raw = _lookup(headers, name)
        if not raw:
            continue
        try:
            payload = json.loads(base64.b64decode(raw, validate=True))
        except (binascii.Error, ValueError, UnicodeDecodeError, json.JSONDecodeError):
            return None
        return payload if isinstance(payload, dict) else None
    return None
