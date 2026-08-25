"""Bazaar Discovery Extension — facilitator-side catalog of paid resources.

Mirrors the official ``@x402/extensions/bazaar`` semantics (foundation spec):
servers declare discovery metadata for their paid endpoints; the facilitator
extracts that metadata from payment payloads and catalogs the resources so
users/agents can browse what's available through ``GET /v1/x402/bazaar``.

Honest by construction: the catalog only lists resources actually declared by
servers via ``declare_*_discovery`` — nothing is invented.
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from typing import Any

from pydantic import BaseModel, Field

BAZAAR_EXTENSION_KEY = "bazaar"

_QUERY_METHODS = ("GET", "HEAD", "DELETE")
_BODY_METHODS = ("POST", "PUT", "PATCH")


class DiscoveryOutput(BaseModel):
    """Declared output shape/example for a paid resource."""

    type: str = "object"
    example: Any = None


class QueryDiscoveryInfo(BaseModel):
    """Discovery info for query-parameter methods (GET/HEAD/DELETE)."""

    input: dict[str, Any] = Field(
        description='{"type":"http","method":...,"queryParams":{...}}',
    )
    output: DiscoveryOutput | None = None


class BodyDiscoveryInfo(BaseModel):
    """Discovery info for body methods (POST/PUT/PATCH)."""

    input: dict[str, Any] = Field(
        description='{"type":"http","method":...,"bodyType":...,"body":{...}}',
    )
    output: DiscoveryOutput | None = None


def declare_query_discovery(
    method: str,
    *,
    query_params: dict[str, Any] | None = None,
    path_params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    output_example: Any = None,
) -> dict[str, Any]:
    """Server-side helper: build the ``bazaar`` extension payload for a
    query-parameter endpoint (GET/HEAD/DELETE)."""
    if method.upper() not in _QUERY_METHODS:
        raise ValueError(f"query discovery requires one of {_QUERY_METHODS}, got {method}")
    info: dict[str, Any] = {"type": "http", "method": method.upper()}
    if query_params:
        info["queryParams"] = query_params
    if path_params:
        info["pathParams"] = path_params
    if headers:
        info["headers"] = headers
    payload: dict[str, Any] = {"info": {"input": info}}
    if output_example is not None:
        payload["info"]["output"] = {"type": "object", "example": output_example}
    return payload


def declare_body_discovery(
    method: str,
    *,
    body_type: str = "json",
    body: dict[str, Any] | None = None,
    query_params: dict[str, Any] | None = None,
    output_example: Any = None,
) -> dict[str, Any]:
    """Server-side helper: build the ``bazaar`` extension payload for a
    body endpoint (POST/PUT/PATCH)."""
    if method.upper() not in _BODY_METHODS:
        raise ValueError(f"body discovery requires one of {_BODY_METHODS}, got {method}")
    if body_type not in ("json", "form-data", "text"):
        raise ValueError(f"bodyType must be json|form-data|text, got {body_type}")
    info: dict[str, Any] = {
        "type": "http",
        "method": method.upper(),
        "bodyType": body_type,
        "body": body or {},
    }
    if query_params:
        info["queryParams"] = query_params
    payload: dict[str, Any] = {"info": {"input": info}}
    if output_example is not None:
        payload["info"]["output"] = {"type": "object", "example": output_example}
    return payload


def extract_bazaar_info(accepts_entry: dict[str, Any]) -> dict[str, Any] | None:
    """Facilitator-side: pull the ``bazaar`` discovery payload out of one
    ``accepts[]`` entry (from ``extra.extensions``). Returns None when absent."""
    extra = accepts_entry.get("extra") or {}
    extensions = extra.get("extensions") or {}
    payload = extensions.get(BAZAAR_EXTENSION_KEY)
    if isinstance(payload, dict) and "info" in payload:
        return payload
    return None


class BazaarCatalog:
    """Thread-safe, bounded catalog of discovered paid resources."""

    def __init__(self, max_entries: int = 10_000) -> None:
        self._max = int(max_entries)
        self._lock = threading.Lock()
        self._resources: OrderedDict[str, dict[str, Any]] = OrderedDict()

    def record(
        self,
        resource_key: str,
        info: dict[str, Any],
        *,
        network: str = "",
        asset: str = "",
        max_amount_required: str = "",
    ) -> bool:
        """Record/refresh a discovered resource. Returns True when stored."""
        if not resource_key or not isinstance(info, dict) or "info" not in info:
            return False
        entry = {
            "resource": resource_key,
            "network": network,
            "asset": asset,
            "maxAmountRequired": max_amount_required,
            "discovery": info,
        }
        with self._lock:
            self._resources.pop(resource_key, None)  # refresh => move to end
            self._resources[resource_key] = entry
            while len(self._resources) > self._max:
                self._resources.popitem(last=False)
        return True

    def record_from_accepts(self, resource_key: str, accepts_entry: dict[str, Any]) -> bool:
        """Convenience: extract + record from one ``accepts[]`` entry."""
        info = extract_bazaar_info(accepts_entry)
        if info is None:
            return False
        return self.record(
            resource_key,
            info,
            network=str(accepts_entry.get("network", "")),
            asset=str(accepts_entry.get("asset", "")),
            max_amount_required=str(accepts_entry.get("maxAmountRequired", "")),
        )

    def list_resources(self, *, network: str | None = None) -> list[dict[str, Any]]:
        """Browse the catalog (newest first), optionally filtered by network."""
        with self._lock:
            items = reversed(list(self._resources.values()))
            if network:
                nid = network.lower()
                return [e for e in items if e["network"].lower() == nid]
            return list(items)

    def get(self, resource_key: str) -> dict[str, Any] | None:
        with self._lock:
            return self._resources.get(resource_key)

    def __len__(self) -> int:
        with self._lock:
            return len(self._resources)


_catalog: BazaarCatalog | None = None


def get_bazaar_catalog() -> BazaarCatalog:
    """Global catalog (lazily initialised)."""
    global _catalog
    if _catalog is None:
        _catalog = BazaarCatalog()
    return _catalog


def register_bazaar_router() -> Any:
    """Build the browse APIRouter: ``GET /v1/x402/bazaar`` (+ ``/{key}``).

    Returned router is mounted by ``create_app``; importing lazily so this
    module stays importable without FastAPI.
    """
    from fastapi import APIRouter, HTTPException

    router = APIRouter(prefix="/v1/x402/bazaar", tags=["x402-bazaar"])

    @router.get("")
    async def browse(network: str | None = None) -> dict[str, Any]:
        resources = get_bazaar_catalog().list_resources(network=network)
        return {"count": len(resources), "resources": resources}

    @router.get("/{resource_key:path}")
    async def detail(resource_key: str) -> dict[str, Any]:
        entry = get_bazaar_catalog().get(resource_key)
        if entry is None:
            raise HTTPException(status_code=404, detail="resource not found in bazaar")
        return entry

    return router
