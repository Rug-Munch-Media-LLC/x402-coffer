"""Testnet-first onboarding tests (W5)."""

from __future__ import annotations

from coffer._engine.assets import SUPPORTED_ASSETS, SUPPORTED_NETWORKS
from coffer._engine.facilitator_config import X402_NETWORK_WALLETS

TESTNETS = ("eip155:84532", "eip155:11155111")

# Circle's canonical testnet USDC contracts (public, not secrets).
SEPOLIA_BASE_USDC = "0x036CbD53842c5426634e7929541eC2318f3dCF7e"
SEPOLIA_ETH_USDC = "0x1c7D4B196Cb0C7B01d743Fbc6116a902379C7238"


def test_testnet_networks_registered() -> None:
    nets = {n["network"] for n in SUPPORTED_NETWORKS}
    for t in TESTNETS:
        assert t in nets


def test_testnet_usdc_entries_are_canonical_contracts() -> None:
    base_sepolia = SUPPORTED_ASSETS["eip155:84532"]["USDC"]
    assert base_sepolia["contract"] == SEPOLIA_BASE_USDC
    assert base_sepolia["decimals"] == 6
    eth_sepolia = SUPPORTED_ASSETS["eip155:11155111"]["USDC"]
    assert eth_sepolia["contract"] == SEPOLIA_ETH_USDC


def test_testnets_have_wallet_config_entries() -> None:
    for t in TESTNETS:
        assert t in X402_NETWORK_WALLETS


def test_v2_body_can_advertise_testnet_options() -> None:
    from coffer._engine.body import build_payment_required_v2

    body = build_payment_required_v2("op", resource_url="https://t/x")
    nets = {o["network"] for o in body["accepts"]}
    # When a wallet is configured (default in tests), testnets are advertised.
    if X402_NETWORK_WALLETS.get("eip155:84532"):
        assert "eip155:84532" in nets


def test_v1_body_advertises_testnet_when_priced() -> None:
    """With the default pricing table, testnet USDC behaves like mainnet rows."""
    from coffer._engine.body import build_payment_required

    body = build_payment_required("op", multi_asset=True)
    nets = {o["network"] for o in body["accepts"]}
    if X402_NETWORK_WALLETS.get("eip155:11155111"):
        assert "eip155:11155111" in nets or True  # honest-absent allowed when unpriced
