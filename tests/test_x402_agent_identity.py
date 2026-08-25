"""Tests for the agent identity extension (ERC-8004 / a2a-x402 shaped)."""

from __future__ import annotations

import os

import httpx
import pytest

from coffer._engine.extensions.agent_identity import (
    AgentIdentity,
    AgentRegistry,
    declare_agent_identity,
    extract_agent_identity,
)


def test_declare_shape_uses_camel_case_aliases() -> None:
    payload = declare_agent_identity(
        agent_id="agent-42", identity_registry="0xreg", agent_name="ScoutBot"
    )
    info = payload["agentIdentity"]["info"]
    assert info["agentId"] == "agent-42"
    assert info["identityRegistry"] == "0xreg"
    assert "chainId" not in info  # exclude_none


def test_extract_from_v2_style_extensions() -> None:
    declared = declare_agent_identity(agent_id="a1")
    payload = {"extensions": {"agentIdentity": declared["agentIdentity"]}}
    assert extract_agent_identity(payload)["agentId"] == "a1"


def test_extract_from_v1_style_extra() -> None:
    declared = declare_agent_identity(agent_id="a2")
    payload = {"extra": {"extensions": {"agentIdentity": declared["agentIdentity"]}}}
    assert extract_agent_identity(payload)["agentId"] == "a2"


def test_extract_absent_returns_none() -> None:
    assert extract_agent_identity({}) is None
    assert extract_agent_identity({"extensions": {}}) is None


def test_registry_records_and_aggregates() -> None:
    reg = AgentRegistry()
    ident = declare_agent_identity(agent_id="a9", agent_name="Bot")["agentIdentity"]["info"]
    reg.record_payment("0xAAA", ident, amount_atomic=500)
    reg.record_payment("0xaaa", None, amount_atomic=300)  # same wallet, case-insensitive
    rec = reg.get("0xAaa")
    assert rec is not None and rec["payments"] == 2
    assert rec["total_paid_atomic"] == 800
    assert rec["identity"]["agentId"] == "a9"  # first declared identity sticks


def test_registry_rejects_empty_wallet() -> None:
    reg = AgentRegistry()
    assert reg.record_payment("", None) is False


def test_model_validates_via_alias() -> None:
    ident = AgentIdentity.model_validate({"agentId": "x"})
    assert ident.agent_id == "x"


@pytest.mark.asyncio
async def test_agents_router_mounted_and_verify_attribution() -> None:
    os.environ.setdefault("X402_WALLET", "0x742d35Cc6634C0532925a3b844Bc9e7595f2bD18")
    os.environ.setdefault("X402_PAY_TO", "0x742d35Cc6634C0532925a3b844Bc9e7595f2bD18")
    from coffer._engine.config import create_app

    app = create_app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get("/v1/x402/agents")
        assert r.status_code == 200 and "agents" in r.json()
