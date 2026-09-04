"""MCP-native tool manifest for Coffer's paid tool surface.

Coffer is MCP-native: every paid intelligence tool is exposed to AI agents
as a *typed, described* tool entry (``app/mcp/manifest.py`` schema —
``MCPToolManifest``: semantic ``{server}:{tool}`` name, semver version,
JSON Schema input, gopass/payment auth scope). This module derives the
Coffer server manifest straight from the FastAPI route definitions of the
paid tools router, so tool metadata (names, parameter types, descriptions,
defaults, required-ness) can never drift from the HTTP surface agents
actually call.

Payment gating: tools are paid through the x402 rail (``X-PAYMENT`` header
/ 402 ``PAYMENT-REQUIRED`` challenge) rather than a gopass secret, so the
manifest carries the ``coffer/x402/paid`` auth scope marker and each tool
description advertises its USD price from the server-authoritative
``X402_PRICING`` table.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from coffer._engine import X402_PRICING
from coffer._engine.manifest import MCPServerManifest, MCPToolManifest
from coffer._engine.tools_router import router as _paid_tools_router

__all__ = [
    "COFFER_AUTH_SCOPE",
    "COFFER_SERVER",
    "SUPPORTED_KINDS",
    "MCPServerManifest",
    "MCPToolManifest",
    "coffer_tool_manifest",
    "ops_router",
    "tool_price_usd",
]

#: x402 V2 spec ``kinds`` advertised by ``GET /supported`` — the
#: (scheme, network) pairs this facilitator can settle.
SUPPORTED_KINDS: list[dict[str, str]] = [
    {"scheme": "exact", "network": "eip155:8453"},
    {"scheme": "exact", "network": "solana:mainnet"},
    {"scheme": "exact", "network": "tron:mainnet"},
]

#: Ops/probe surface (x402 V2 spec): discovery + liveness/readiness.
ops_router = APIRouter(tags=["Ops"])


@ops_router.get("/supported")
async def supported() -> dict[str, Any]:
    """x402 V2 spec: list supported (scheme, network) kinds."""
    return {"kinds": SUPPORTED_KINDS}


@ops_router.get("/healthz")
async def healthz() -> dict[str, str]:
    """Liveness probe: process is up."""
    return {"status": "ok"}


@ops_router.get("/readyz")
async def readyz() -> dict[str, Any]:
    """Readiness probe: 200 when at least one scheme kind is registered."""
    count = len(SUPPORTED_KINDS)
    if count < 1:
        from fastapi import HTTPException

        raise HTTPException(status_code=503, detail={"status": "not ready", "kinds": 0})
    return {"status": "ready", "kinds": count}


@ops_router.get("/metrics", tags=["Ops"])
async def get_metrics() -> str:
    """Prometheus text format metrics."""
    from coffer._engine.handler import X402Handler

    lines = [
        "# TYPE x402_verify_total counter",
        f"x402_verify_total 0",
        "# TYPE x402_settle_total counter",
        f"x402_settle_total 0",
        "# TYPE x402_verify_failures_total counter",
        f"x402_verify_failures_total 0",
        "# TYPE x402_facilitator_healthy gauge",
        f"x402_facilitator_healthy 1",
    ]
    return "\n".join(lines) + "\n"


#: MCP server slug for Coffer (manifest ``name`` prefix before ':').
COFFER_SERVER = "coffer"

#: Auth scope marker for x402-paid tools. Unlike infra tools (gopass paths),
#: Coffer tools are gated by the payment rail itself — the scope documents
#: the gating mechanism, never a secret path.
COFFER_AUTH_SCOPE = "coffer/x402/paid"

_JSON_TYPE_BY_ANNOTATION: dict[Any, str] = {
    str: "string",
    int: "integer",
    float: "number",
    bool: "boolean",
    list: "array",
    dict: "object",
}


def tool_price_usd(tool_name: str) -> float:
    """Server-authoritative USD price for a tool (falls back to the ``*`` default)."""
    entry = X402_PRICING.get(tool_name) or X402_PRICING.get("*") or {}
    if isinstance(entry, dict):
        return float(entry.get("price_usd") or 0.0)
    return 0.0


def _param_schema(param: Any) -> dict[str, Any]:
    """JSON Schema for one FastAPI query/body parameter (typed + described)."""
    field_info = getattr(param, "field_info", None)
    annotation = getattr(field_info, "annotation", None)
    schema: dict[str, Any] = {
        "type": _JSON_TYPE_BY_ANNOTATION.get(annotation, "string"),
    }
    description = getattr(field_info, "description", None)
    if description:
        schema["description"] = description
    if field_info is not None and not field_info.is_required():
        default = getattr(param, "default", None)
        if default is not None and type(default).__name__ != "PydanticUndefined":
            schema["default"] = default
    return schema


def _input_schema(route: Any) -> dict[str, Any]:
    """JSON Schema object describing the route's parameters."""
    properties: dict[str, Any] = {}
    required: list[str] = []
    dependant = getattr(route, "dependant", None)
    if dependant is None:
        return {"type": "object", "properties": {}}
    params = [*getattr(dependant, "query_params", []), *getattr(dependant, "body_params", [])]
    for param in params:
        name = getattr(param, "name", "") or ""
        if not name or name == "request":
            continue
        properties[name] = _param_schema(param)
        field_info = getattr(param, "field_info", None)
        if field_info is not None and field_info.is_required():
            required.append(name)
    schema: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required
    return schema


def _tool_description(route: Any, tool_name: str) -> str:
    """Human-readable description: summary + x402 price (<=200 chars)."""
    endpoint = getattr(route, "endpoint", None)
    doc = (getattr(endpoint, "__doc__", "") or "").strip()
    summary = getattr(route, "summary", "") or ""
    base = doc.splitlines()[0] if doc else (summary or tool_name.replace("_", " "))
    price = tool_price_usd(tool_name)
    text = f"{base} Paid via x402: ${price:.2f} USD per call (402 PAYMENT-REQUIRED challenge)."
    return text[:200]


def coffer_tool_manifest(
    endpoint: str = "https://mcp.rugmunch.io",
    transport: str = "http",
) -> MCPServerManifest:
    """Build Coffer's MCP server manifest from the live paid-tools router.

    Every paid tool route becomes a typed, described ``MCPToolManifest``
    entry (``coffer:{tool}``); input schemas are derived from the route
    parameters so agents can call tools without guessing types.
    """
    tools: list[MCPToolManifest] = []
    for route in getattr(_paid_tools_router, "routes", []):
        path = getattr(route, "path", "")
        tool_name = path.rsplit("/", 1)[-1] if path else getattr(route, "name", "")
        if not tool_name or "{" in tool_name:
            continue  # skip non-tool routes / path-parameter routes
        tools.append(
            MCPToolManifest(
                name=f"{COFFER_SERVER}:{tool_name}",
                version="1.0.0",
                server=COFFER_SERVER,
                description=_tool_description(route, tool_name),
                input_schema=_input_schema(route),
                output_schema={},
                auth_scope=COFFER_AUTH_SCOPE,
            )
        )
    return MCPServerManifest(
        name=COFFER_SERVER,
        transport=transport,
        endpoint=endpoint,
        tools=tools,
        auth_scopes=[COFFER_AUTH_SCOPE],
    )
