"""ERC-20 helpers for the Coffer client (asset addresses + transfer encoding)."""

from __future__ import annotations

from typing import Any

# Canonical token contracts per network (CAIP-2). Public constants.
ERC20_ASSETS: dict[str, dict[str, str]] = {
    "eip155:8453": {"USDC": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"},
    "eip155:1": {"USDC": "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"},
    "eip155:42161": {"USDC": "0xaf88d065e77c8cC2239327C5EDb3A432268e5831"},
    "eip155:10": {"USDC": "0x0b2C639c533813f4Aa9D7837CAf62653d097Ff85"},
    "eip155:137": {"USDC": "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359"},
}


def erc20_asset_address(asset: str, network: str) -> str | None:
    """Canonical ERC-20 contract for ``(asset, network)`` — None if unknown."""
    return ERC20_ASSETS.get(network.lower(), {}).get(asset.upper())


def encode_erc20_transfer(recipient: str, amount_atomic: int) -> str:
    """ABI-encode ``transfer(address,uint256)`` arguments (selector added by caller)."""
    addr = recipient.lower().replace("0x", "").rjust(64, "0")
    amount = format(amount_atomic, "064x")
    return addr + amount


def decode_option_extra(option: dict[str, Any]) -> dict[str, Any]:
    """Read ``extra`` from an accepts[] option defensively."""
    extra = option.get("extra")
    return extra if isinstance(extra, dict) else {}
