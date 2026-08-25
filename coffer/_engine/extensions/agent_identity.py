"""Agent Identity extension — ERC-8004-style identity + A2A interop hooks.

Attaches an optional, standard-shaped ``agentIdentity`` block to x402
payments so a facilitator can know *which agent* paid — not just which
wallet. Shapes follow:

* ERC-8004 (agent registration/identity) field conventions: an identity
  registry contract + agent identifier.
* Google ``a2a-x402``: agent card metadata (name, endpoint, capabilities).

HONEST SCOPE: this module records and serves declared identities. It does NOT
verify on-chain ERC-8004 registrations — that requires registry-contract
reads and is marked ``verification: "declared"`` until implemented. A declared
identity is attribution bookkeeping, never an authorization decision.
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field

AGENT_IDENTITY_KEY = "agentIdentity"


class AgentIdentity(BaseModel):
    """Declared identity of the paying agent (ERC-8004 / a2a-x402 shaped)."""

    agent_id: str = Field(alias="agentId", description="ERC-8004 agentId or a2a agent name")
    identity_registry: str | None = Field(
        default=None,
        alias="identityRegistry",
        description="ERC-8004 identity registry contract (0x…) when applicable",
    )
    agent_endpoint: str | None = Field(default=None, alias="agentEndpoint")
    agent_name: str | None = Field(default=None, alias="agentName")
    chain_id: str | None = Field(default=None, alias="chainId")

    model_config = {"populate_by_name": True}


def declare_agent_identity(
    *,
    agent_id: str,
    identity_registry: str | None = None,
    agent_endpoint: str | None = None,
    agent_name: str | None = None,
    chain_id: str | None = None,
) -> dict[str, Any]:
    """Build the ``agentIdentity`` extension payload for a payment."""
    ident = AgentIdentity(
        agentId=agent_id,
        identityRegistry=identity_registry,
        agentEndpoint=agent_endpoint,
        agentName=agent_name,
        chainId=chain_id,
    )
    return {AGENT_IDENTITY_KEY: {"info": ident.model_dump(by_alias=True, exclude_none=True)}}


def extract_agent_identity(payment_payload: dict[str, Any]) -> dict[str, Any] | None:
    """Pull the declared identity out of a payment payload's extensions.

    Looks in ``payload.extensions.agentIdentity`` (v2 echo semantics) then
    ``payload.extra.extensions.agentIdentity`` (fleet v1 convention).
    Returns the info dict or None.
    """
    for holder_key, inner in (("extensions", None), ("extra", "extensions")):
        holder = payment_payload.get(holder_key)
        if not isinstance(holder, dict):
            continue
        block = holder.get(inner) if inner else holder
        if isinstance(block, dict):
            info = block.get(AGENT_IDENTITY_KEY)
            if isinstance(info, dict) and "info" in info:
                return info["info"]
    return None


class AgentRegistry:
    """Bounded registry of agents seen paying, with lifetime spend totals."""

    def __init__(self, max_agents: int = 10_000) -> None:
        self._max = int(max_agents)
        self._lock = threading.Lock()
        self._agents: OrderedDict[str, dict[str, Any]] = OrderedDict()

    def record_payment(
        self,
        wallet: str,
        identity: dict[str, Any] | None,
        amount_atomic: int = 0,
    ) -> bool:
        """Attribute a payment to an agent. Returns True when recorded."""
        if not wallet:
            return False
        now = datetime.now(UTC).isoformat()
        with self._lock:
            rec = self._agents.get(wallet.lower())
            if rec is None:
                if len(self._agents) >= self._max:
                    self._agents.popitem(last=False)
                rec = {
                    "wallet": wallet.lower(),
                    "identity": None,
                    "first_seen": now,
                    "last_seen": now,
                    "payments": 0,
                    "total_paid_atomic": 0,
                }
                self._agents[wallet.lower()] = rec
            if identity is not None:
                rec["identity"] = identity
            rec["last_seen"] = now
            rec["payments"] += 1
            rec["total_paid_atomic"] += int(amount_atomic or 0)
        return True

    def list_agents(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(v) for v in reversed(list(self._agents.values()))]

    def get(self, wallet: str) -> dict[str, Any] | None:
        with self._lock:
            return self._agents.get(wallet.lower())

    def __len__(self) -> int:
        with self._lock:
            return len(self._agents)


_registry: AgentRegistry | None = None


def get_agent_registry() -> AgentRegistry:
    global _registry
    if _registry is None:
        _registry = AgentRegistry()
    return _registry


def register_agents_router() -> Any:
    """APIRouter: ``GET /v1/x402/agents`` (+ ``/{wallet}``)."""
    from fastapi import APIRouter, HTTPException

    router = APIRouter(prefix="/v1/x402/agents", tags=["x402-agents"])

    @router.get("")
    async def browse() -> dict[str, Any]:
        agents = get_agent_registry().list_agents()
        return {"count": len(agents), "agents": agents}

    @router.get("/{wallet}")
    async def detail(wallet: str) -> dict[str, Any]:
        rec = get_agent_registry().get(wallet)
        if rec is None:
            raise HTTPException(status_code=404, detail="unknown agent wallet")
        return rec

    return router
