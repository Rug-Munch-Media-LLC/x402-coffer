"""Facilitator fallback chain + server-authoritative pricing (per scheme).

Two behaviours that MATTER for a payment system and are pure logic — no
blockchain required:

1. **Facilitator fallback chain** — ``FacilitatorRouter`` MUST try external
   facilitators in priority order and fall through to the next healthy one when
   the first fails (coinbase HTTP 500 -> payai 200-verified). And when EVERY
   facilitator is down and there is no self-hosted verifier, ``verify`` MUST
   return ``verified:False`` with a reason — NOT crash and NOT fabricate a
   verification. All network interaction is stubbed with ``respx`` so the tests
   exercise the real routing/pricing/replay code over ``httpx``, never a live
   provider.

2. **Pricing authority** — the server's price is enforced in ATOMIC units
   regardless of the client-claimed amount: underpayment rejected, exact and
   overpayment accepted, non-finite/negative rejected. Asserted per scheme on
   the public ``validate_client_amount`` contract, and the ``upto`` scheme's
   own max-amount pricing is checked against the same server rules.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any
from unittest.mock import patch

import httpx
import pytest
import respx

from coffer._engine.service import (
    FacilitatorConfig,
    FacilitatorRouter,
    get_facilitator_router,
    validate_client_amount,
)

_OP = "api_access"
_BASE = "eip155:8453"
# Optimism has NO EIP-7702 RPC configured -> fallback is a fast, offline miss.
_POLYGON = "eip155:137"


def _ext_router(facilitators: list[FacilitatorConfig]) -> FacilitatorRouter:
    """A router with ONLY external facilitators (self-hosted disabled)."""
    return FacilitatorRouter(facilitators=facilitators, self_hosted=False)


def _cfg(name: str, verify: str, priority: int) -> FacilitatorConfig:
    """A small external-facilitator config (settle_url required by the dataclass)."""
    return FacilitatorConfig(
        name=name,
        verify_url=verify,
        settle_url=verify.replace("/verify", "/settle"),
        priority=priority,
    )


# ── §1 facilitator fallback chain ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_falls_through_from_500_coinbase_to_payai() -> None:
    """coinbase 500 -> payai 200-verified: router MUST fall through to the
    healthy facilitator and report verified:true + facilitator=payai."""
    router = _ext_router(
        [
            _cfg("coinbase", "https://coinbase.test/verify", 1),
            _cfg("payai", "https://payai.test/verify", 2),
        ]
    )
    with respx.mock:
        respx.post("https://coinbase.test/verify").mock(
            return_value=httpx.Response(500, json={"error": "boom"})
        )
        respx.post("https://payai.test/verify").mock(
            return_value=httpx.Response(200, json={"verified": True, "transactionHash": "0xp"})
        )
        res = await router.verify("pid_fb1", "0xtx_fb1", _BASE, "USDC", 0.001)
    assert res["verified"] is True, res
    assert res["facilitator"] == "payai", res


@pytest.mark.asyncio
async def test_every_facilitator_down_fails_closed_no_crash() -> None:
    """All facilitators unreachable + NO self-hosted verifier -> verified:false
    with a reason; the EVM fallback (no 7702 RPC on Optimism) stays offline; no
    fabricated settlement."""
    router = _ext_router(
        [
            _cfg("coinbase", "https://coinbase.test/v", 1),
            _cfg("payai", "https://payai.test/v", 2),
        ]
    )
    with respx.mock:
        respx.post("https://coinbase.test/v").mock(side_effect=httpx.ConnectError("down"))
        respx.post("https://payai.test/v").mock(side_effect=httpx.ConnectError("down"))
        res = await router.verify("pid_fb2", "0xtx_fb2", _POLYGON, "USDC", 0.001)
    assert res["verified"] is False, res
    assert res["settlement"] is None, res
    assert isinstance(res["reason"], str) and res["reason"]  # a real reason, not a crash


@pytest.mark.asyncio
async def test_first_facilitator_success_short_circuits() -> None:
    """A healthy high-priority facilitator is used and the second is never hit."""
    called: list[str] = []

    async def _cb(request: httpx.Request) -> httpx.Response:
        called.append(request.url.host)
        return httpx.Response(200, json={"isValid": True, "transactionHash": "0xok"})

    router = _ext_router(
        [
            _cfg("coinbase", "https://coinbase.test/verify", 1),
            _cfg("payai", "https://payai.test/verify", 2),
        ]
    )
    with (
        respx.mock,
        patch("coffer._engine.external_verify.random.choices", return_value=[router.facilitators[0]]),
    ):
        respx.post("https://coinbase.test/verify").mock(side_effect=_cb)
        respx.post("https://payai.test/verify").mock(
            side_effect=lambda _: (_ for _ in ()).throw(AssertionError("payai must not be called"))
        )
        res = await router.verify("pid_fb3", "0xtx_fb3", _BASE, "USDC", 0.001)
    assert res["verified"] is True
    assert res["facilitator"] == "coinbase"
    assert called == ["coinbase.test"]


# ── §2 server-authoritative pricing (per scheme) ─────────────────────────────
# resolve_price_usd(_OP, base, USDC) = 0.01 flat -> 10_000 atomic (6 decimals).

_REQUIRED = 10_000  # atomic USDC on base for 0.01 USD

# (label, client_usd, expected_ok, reason_fragment)
_PRICING_CASES: list[tuple[str, float, bool, str]] = [
    ("exact-underpayment", 0.0099, False, "underpayment"),
    ("exact-boundary", 0.01, True, ""),
    ("exact-overpayment", 0.02, True, ""),
    ("negative", -5.0, False, "non-finite/negative"),
]


@pytest.mark.parametrize(
    ("label", "client_usd", "ok", "frag"), _PRICING_CASES, ids=[c[0] for c in _PRICING_CASES]
)
def test_pricing_authority_exact_scheme(label: str, client_usd: float, ok: bool, frag: str) -> None:
    """Exact scheme: server enforces its 10_000-atomic price in atomic units —
    under/over-payment decided by the server, never the client's claim."""
    got, reason = validate_client_amount(_OP, client_usd, "USDC", _BASE)
    assert got is ok, f"[{label}] validate_client_amount -> ({got}, {reason!r})"
    if frag:
        assert frag in reason.lower()


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_pricing_authority_rejects_nonfinite(bad: float) -> None:
    """Non-finite client amounts are never accepted (would break atomic math)."""
    ok, reason = validate_client_amount(_OP, bad, "USDC", _BASE)
    assert ok is False
    assert "non-finite" in reason.lower()


def test_pricing_authority_missing_amount_rejected() -> None:
    ok, reason = validate_client_amount(_OP, None, "USDC", _BASE)
    assert ok is False
    assert "missing" in reason.lower()


def test_upto_scheme_pricing_matches_server_rules() -> None:
    """The ``upto`` scheme's own max-amount pricing honours the same atomic /
    decimal discipline as the exact scheme, and its on-chain settle is an honest
    scaffold (raises, never fabricates a tx_hash)."""
    from coffer._engine.extensions.schemes import UpToScheme

    scheme = UpToScheme(max_price_usd=Decimal("0.10"), asset="usdc", network=_BASE)
    option = scheme.payment_option(resource="r", description="d", pay_to="0xCoinbase")
    assert option["scheme"] == "upto"
    assert option["network"] == _BASE
    # maxAmountRequired is the price in atomic USDC (0.10 * 10^6), a string.
    assert option["maxAmountRequired"] == str(100_000)
    assert option["maxAmountRequired"].isdigit()
    # The advertised max must be >= what the server would require (>= 10k atomic).
    assert int(option["maxAmountRequired"]) >= _REQUIRED
    assert option["extra"]["scaffolded"] == "needs_facilitator"

    with pytest.raises(NotImplementedError):
        asyncio_run_upto(scheme)

    # A forged/empty authorization is still structurally valid (no privilege).
    assert scheme.authorize(max_amount_atomic=100_000)["scheme"] == "upto"


def asyncio_run_upto(scheme: Any) -> Any:
    """Drive UpToScheme.settle (async) synchronously; assert it raises honestly."""
    import asyncio

    async def _go() -> None:
        await scheme.settle({}, override="50%")

    return asyncio.run(_go())


def test_router_singleton_resolves_and_readiness_mode() -> None:
    """The process-wide router resolves to a FacilitatorRouter in self-hosted mode
    (the default fail-closed posture) with a configured wallet."""
    r = get_facilitator_router()
    assert isinstance(r, FacilitatorRouter)
    readiness = r.readiness()
    assert readiness["mode"] == "self_hosted"
    assert readiness["wallet_configured"] is True
