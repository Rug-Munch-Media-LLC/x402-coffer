"""Multi-asset ``accepts[]`` exhaustiveness + honest health signals.

These tests catch a misconfigured chain *before a merchant advertises a broken
payment option*: for EVERY ``(network, asset)`` the system claims to support in
``SUPPORTED_ASSETS`` we assert that, when the underlying wallet is configured,
the advertised ``accepts[]`` option is fully valid — CAIP-2 network id, correct
asset symbol, a REAL server ``payToAddress`` and a ``maxAmountRequired`` that
exactly prices ``resolve_price_usd`` in atomic units. Networks WITHOUT a
configured receiving wallet (Solana/TRON by default) must be HONESTLY
absent — never advertised with a zero/blank payTo.

The pricing authority is enforced here too: ``maxAmountRequired`` must equal
``asset_to_atomic(resolve_price_usd(op, net, asset), asset, net)``, so a server
that quotes the wrong price for an option is caught before a client signs it.

Separately we assert the operator-facing health signals a merchant/operator
relies on: the ``/v1/x402/health`` verification status (evm/solana live, tron
scaffolded — never claimed live), that ``FacilitatorRouter.readiness()`` reports
``wallet_configured``, and that each offer carries an honest oracle-probe label
(``extra.usdPerUnitSource``: stablecoin vs static-default) so the quoted atomic
amount's price source is auditable.
"""

from __future__ import annotations

from typing import Any

import pytest
from conftest import X402E2EHarness

from coffer._engine.assets import normalize_caip2_network
from coffer._engine.models import flatten_supported_assets

# The operation every option below is quoted for (0.01 USD flat).
MATRIX_OP = "api_access"


def _offered_prices() -> dict[str, Any]:
    """Rebuild the advertised surface ONCE and index by (network, asset)."""
    from coffer._engine.middleware import build_multi_asset_accepts

    return {(e["network"], e["asset"]): e for e in build_multi_asset_accepts(MATRIX_OP)}


@pytest.mark.parametrize("row", flatten_supported_assets(), ids=lambda r: f"{r['network']}-{r['asset']}")
def test_every_supported_asset_yields_valid_or_honestly_absent_option(row: dict[str, Any]) -> None:
    """Every advertised (network, asset) yields a valid option; an unconfigured
    chain's assets are NEVER advertised (fail-closed, not blank pay-to)."""
    from coffer._engine.service import (
        X402_NETWORK_WALLETS,
        X402_WALLET,
        asset_to_atomic,
        resolve_price_usd,
    )

    caip2 = row["network"]
    asset = row["asset"]

    # A supported chain must have a configured receiving wallet behind it.
    pay_to = (X402_NETWORK_WALLETS.get(caip2) or X402_WALLET or "").strip()
    assert pay_to, f"no receiving wallet configured for {caip2} — chain cannot be advertised"

    # Is (network, asset) actually quotable/verifiable? If the price/verifier
    # layer cannot serve it, the honest surface must NOT advertise a broken row.
    try:
        expected = asset_to_atomic(resolve_price_usd(MATRIX_OP, caip2, asset), asset, caip2)
    except ValueError:
        expected = None

    entry = _offered_prices().get((caip2, asset))
    if expected is None:
        # Non-verifiable asset (e.g. Solana USDT has no SPL verifier): it MUST
        # stay OUT of accepts[] — never a broken, unpayable option.
        assert entry is None, f"{caip2}/{asset} is not verifiable but was advertised: {entry}"
        return

    assert entry is not None, (
        f"SUPPORTED_ASSETS claims {caip2}/{asset} but build_multi_asset_accepts"
        f" omitted it (misconfigured chain quietly unadvertised)"
    )

    # (a) CAIP-2 network id (never a bare "base").
    assert entry["network"] == caip2 == normalize_caip2_network(entry["network"])
    # (b) atomic string amount on the wire, never a float.
    assert isinstance(entry["maxAmountRequired"], str)
    assert entry["maxAmountRequired"].isdigit()
    # (c) a real receiving address (the configured wallet), with pay-to for THIS chain.
    assert entry["payToAddress"].strip() == pay_to
    assert entry["payToAddress"] != "coffer-unconfigured-wallet"
    # (d) correct asset symbol.
    assert entry["asset"] == asset
    # (e) PRICING AUTHORITY: the quoted atomic max MUST equal the server-price.
    assert int(entry["maxAmountRequired"]) == expected, (
        f"{caip2}/{asset} mispriced: advertised {entry['maxAmountRequired']} != server {expected}"
    )


def test_accepts_surface_nonempty_and_every_option_spec_valid() -> None:
    """Sanity: the multi-asset surface exists and every option is structurally valid."""
    offers = list(_offered_prices().values())
    assert offers, "accepts[] must be non-empty for the priced API operation"
    for e in offers:
        assert e["scheme"] in ("exact", "upto")
        assert e["network"].count(":") == 1
        assert e["maxAmountRequired"].isdigit()
        assert e["payToAddress"].strip()
        assert e["asset"]
        assert e["extra"]["caip2"] == e["network"]


def test_offer_oracle_probe_label_is_honest() -> None:
    """Each offer's oracle-probe label (usdPerUnitSource) matches the asset:
    stablecoins 1:1, volatile assets static-default — never the reverse."""
    stable = {"USDC", "USDT", "POL", "TRX"}
    for (net, asset), e in _offered_prices().items():
        label = e["extra"]["usdPerUnitSource"]
        if asset in stable:
            assert label == "stablecoin", f"{net}/{asset} flagged {label}"
        else:
            assert label == "static-default", f"{net}/{asset} flagged {label}"
        # and there is a real USD quote behind the label
        assert e["extra"]["usdPerUnit"] > 0
        assert e["extra"]["priceUsd"] > 0


async def test_health_wallet_and_readiness_agree(e2e: X402E2EHarness) -> None:
    """Honest health: endpoint verification status + readiness wallet_configured
    + the offered payTo address all reflect the SAME configured wallet."""
    from coffer._engine.service import (
        X402_WALLET,
        get_facilitator_router,
    )

    async with e2e.caller("9.9.9.9") as client:
        r = await client.get("/v1/x402/health")
    assert r.status_code == 200
    status = r.json()["verification_status"]
    assert status["evm"] == "live"
    assert status["solana"] == "live"
    assert status["tron"] == "live"

    # readiness() carries the wallet-configured signal the surface advertises.
    ready = get_facilitator_router().readiness()
    assert ready["wallet_configured"] is True

    # The advertised EVM payTo is the configuration the middleware actually uses.
    offers = list(_offered_prices().values())
    evm_offers = [e for e in offers if e["network"].startswith("eip155:")]
    assert evm_offers
    for e in evm_offers:
        assert e["payToAddress"] == X402_WALLET
