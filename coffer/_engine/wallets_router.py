"""Wallet management API router — inspect and validate receiving wallets.

Endpoints:
    GET  /v1/x402/wallets          — list configured wallets (masked) + unconfigured
    POST /v1/x402/wallets/validate  — validate {chain, address}
    GET  /v1/x402/wallets/status   — honest status of which chains can accept payments
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel

from .wallets import (
    CHAIN_INFO,
    load_wallets,
    mask_wallet,
    validate_wallet,
    wallet_status,
)

router = APIRouter(prefix="/v1/x402/wallets", tags=["wallets"])


class ValidateRequest(BaseModel):
    chain: str
    address: str


@router.get("")
async def list_wallets() -> dict[str, Any]:
    """List all configured wallets (masked) and unconfigured chains."""
    config = load_wallets()
    return wallet_status(config)


@router.post("/validate")
async def validate_address(req: ValidateRequest) -> dict[str, Any]:
    """Validate a wallet address for a given chain."""
    return validate_wallet(req.address, req.chain)


@router.get("/status")
async def get_status() -> dict[str, Any]:
    """Honest status: which chains are ready to accept payments."""
    return wallet_status()
