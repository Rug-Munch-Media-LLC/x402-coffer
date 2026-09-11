"""Tests for the Bazaar Discovery extension."""

from __future__ import annotations

import pytest

from coffer._engine.extensions.bazaar import (
    BazaarCatalog,
    declare_body_discovery,
    declare_query_discovery,
    extract_bazaar_info,
    get_bazaar_catalog,
)


def test_declare_query_discovery_shape() -> None:
    payload = declare_query_discovery(
        "GET",
        query_params={"lat": "number", "lon": "number"},
        output_example={"temp_c": 21},
    )
    assert payload["info"]["input"]["method"] == "GET"
    assert payload["info"]["input"]["queryParams"] == {"lat": "number", "lon": "number"}
    assert payload["info"]["output"]["example"] == {"temp_c": 21}


def test_declare_query_discovery_rejects_body_method() -> None:
    with pytest.raises(ValueError, match="query discovery requires"):
        declare_query_discovery("POST")


def test_declare_body_discovery_shape() -> None:
    payload = declare_body_discovery("POST", body={"prompt": "string"}, body_type="json")
    assert payload["info"]["input"]["bodyType"] == "json"
    assert payload["info"]["input"]["body"] == {"prompt": "string"}


def test_declare_body_discovery_rejects_get() -> None:
    with pytest.raises(ValueError, match="body discovery requires"):
        declare_body_discovery("GET")


def test_extract_bazaar_info_from_accepts_entry() -> None:
    entry = {
        "scheme": "exact",
        "network": "eip155:8453",
        "asset": "USDC",
        "maxAmountRequired": "1000",
        "extra": {"extensions": {"bazaar": declare_query_discovery("GET")}},
    }
    info = extract_bazaar_info(entry)
    assert info is not None
    assert info["info"]["input"]["method"] == "GET"


def test_extract_bazaar_info_absent_returns_none() -> None:
    assert extract_bazaar_info({"extra": {"extensions": {}}}) is None
    assert extract_bazaar_info({}) is None


def test_catalog_record_and_browse() -> None:
    cat = BazaarCatalog()
    entry = {
        "network": "eip155:8453",
        "asset": "USDC",
        "maxAmountRequired": "1000",
        "extra": {"extensions": {"bazaar": declare_query_discovery("GET", query_params={"q": "str"})}},
    }
    assert cat.record_from_accepts("GET /weather", entry) is True
    assert len(cat) == 1
    resources = cat.list_resources()
    assert resources[0]["resource"] == "GET /weather"
    assert resources[0]["network"] == "eip155:8453"


def test_catalog_network_filter_and_missing_key() -> None:
    cat = BazaarCatalog()
    base = {"asset": "USDC", "maxAmountRequired": "1"}
    cat.record_from_accepts(
        "GET /a",
        {
            **base,
            "network": "eip155:8453",
            "extra": {"extensions": {"bazaar": declare_query_discovery("GET")}},
        },
    )
    cat.record_from_accepts(
        "GET /b",
        {
            **base,
            "network": "solana:mainnet",
            "extra": {"extensions": {"bazaar": declare_query_discovery("GET")}},
        },
    )
    assert len(cat.list_resources(network="eip155:8453")) == 1
    assert cat.get("nope") is None


def test_catalog_rejects_invalid_payloads() -> None:
    cat = BazaarCatalog()
    assert cat.record("", {"info": {}}) is False
    assert cat.record("k", {}) is False
    assert cat.record_from_accepts("k", {"extra": {}}) is False
    assert len(cat) == 0


def test_global_catalog_singleton() -> None:
    assert get_bazaar_catalog() is get_bazaar_catalog()


def test_bounded_catalog_evicts_oldest() -> None:
    cat = BazaarCatalog(max_entries=2)
    for i in range(3):
        cat.record(f"k{i}", declare_query_discovery("GET"))
    assert len(cat) == 2
    assert cat.get("k0") is None  # oldest evicted
