"""MCP Tool Manifest (M2 in v3 unfuck plan).

Frozen on creation. Modified only via bounded-task delegation with Task ID.

Defines the schema for versioning, auth, and discovery of every tool in the
MCP mesh. See DESIGN.md §M2.

Schema:
    name: "{server}:{tool}" e.g. "rmi-netcup:docker_ps"
    version: "MAJOR.MINOR.PATCH" semantic
    server: server slug
    description: human-readable, max 200 chars
    input_schema/output_schema: JSON Schema
    auth_scope: gopass path e.g. "rmi/infra/netcup/ssh"
    deprecated: bool
    successor: optional replacement name
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, HttpUrl


class MCPToolManifest(BaseModel):
    """Versioned, authenticated, discoverable description of one MCP tool."""

    model_config = ConfigDict(strict=True, frozen=True)

    name: str = Field(
        ...,
        pattern=r"^[a-z][a-z0-9-]*:[a-z][a-z0-9_]*$",
        description='Format "{server}:{tool}" — lowercase, hyphens for server, underscores for tool.',
        examples=["rmi-netcup:docker_ps", "coingecko:simple_price"],
    )
    version: str = Field(
        ...,
        pattern=r"^\d+\.\d+\.\d+$",
        description="Semantic version MAJOR.MINOR.PATCH.",
        examples=["1.0.0", "2.1.3"],
    )
    server: str = Field(
        ...,
        pattern=r"^[a-z][a-z0-9-]*$",
        description="Server slug (matches the name prefix before ':').",
    )
    description: str = Field(..., max_length=200)
    input_schema: dict[str, Any] = Field(
        default_factory=dict,
        description="JSON Schema describing the tool's input.",
    )
    output_schema: dict[str, Any] = Field(
        default_factory=dict,
        description="JSON Schema describing the tool's output.",
    )
    auth_scope: str = Field(
        ...,
        pattern=r"^[a-z][a-z0-9/_-]*$",
        description="Gopass path. Resolved at tool-call time, never exposed to agents.",
        examples=["rmi/infra/netcup/ssh", "rmi/api/coingecko/key"],
    )
    deprecated: bool = False
    successor: str | None = Field(
        default=None,
        pattern=r"^[a-z][a-z0-9-]*:[a-z][a-z0-9_]*$",
        description="Replacement tool name if deprecated.",
    )
    documented_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    documentation_url: HttpUrl | None = None


class MCPServerManifest(BaseModel):
    """Bundle of tools from one MCP server."""

    model_config = ConfigDict(strict=True)

    name: str = Field(..., pattern=r"^[a-z][a-z0-9-]*$")
    transport: str = Field(..., pattern=r"^(stdio|http|sse)$")
    endpoint: str
    tools: list[MCPToolManifest] = Field(default_factory=list)
    auth_scopes: list[str] = Field(default_factory=list)


# ── Registry helpers (live in app/mcp/registry.py at M2 ship) ─────────
async def resolve_tool(
    name: str,
    major_version: int | None = None,
) -> MCPToolManifest | None:
    """Resolve a tool name + optional MAJOR version to its full manifest.

    Returns None if not found or if the registry is unavailable.

    Placeholder — real implementation lives in app/mcp/registry.py (M2 #2).
    """
    return None


async def list_tools(
    server: str | None = None,
    auth_scope: str | None = None,
    include_deprecated: bool = False,
) -> list[MCPToolManifest]:
    """List tools matching filters. Placeholder for registry impl."""
    return []
