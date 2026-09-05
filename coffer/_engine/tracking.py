"""Coffer — per-token usage analytics, subscriptions, and paywalled products.

Extracted from the former ``handler.py`` god file. These are standalone
stateful trackers owned by :class:`coffer._engine.handler.X402Handler`:

- :class:`UsageTracker`      — settlement volume / gas / response time per API key
- :class:`SubscriptionManager` — recurring subscriptions with grace-period state machine
- :class:`ProductManager`    — paywalled URL products: pay → access token → redirect
"""
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Rug Munch Media LLC
# Part of Coffer — https://git.rugmunch.io/RugMunchMedia/x402-coffer

from __future__ import annotations

import time


class UsageTracker:
    """Per-token usage analytics: settlement volume, gas cost, response time per API key."""

    def __init__(self) -> None:
        self._records: dict[str, dict] = {}

    def record(
        self,
        token: str,
        *,
        amount_usd: float = 0,
        gas_used: int = 0,
        response_ms: int = 0,
        endpoint: str = "",
        network: str = "",
    ) -> None:
        stats = self._records.setdefault(
            token,
            {
                "total_settlement_volume": 0.0,
                "total_gas_cost": 0,
                "total_requests": 0,
                "avg_response_ms": 0,
                "by_endpoint": {},
                "by_network": {},
            },
        )
        stats["total_settlement_volume"] += amount_usd
        stats["total_gas_cost"] += gas_used
        stats["total_requests"] += 1
        stats["avg_response_ms"] = int(
            (stats["avg_response_ms"] * (stats["total_requests"] - 1) + response_ms) / stats["total_requests"]
        )
        if endpoint:
            stats["by_endpoint"][endpoint] = stats["by_endpoint"].get(endpoint, 0) + 1
        if network:
            stats["by_network"][network] = stats["by_network"].get(network, 0) + 1

    def get_stats(self, token: str = "") -> dict:
        if token:
            return self._records.get(token, {})
        return dict(self._records)


class SubscriptionManager:
    """Recurring subscription management with grace period state machine."""

    def __init__(self) -> None:
        self._subs: dict[str, dict] = {}

    def create(self, sub_id: str, tier: str, monthly_cost: float, payee: str = "") -> dict:
        self._subs[sub_id] = {
            "tier": tier,
            "monthly_cost": monthly_cost,
            "state": "pending",
            "grace_days": 0,
            "paid_until": 0,
            "payee": payee,
        }
        return self._subs[sub_id]

    def activate(self, sub_id: str, days: int = 30) -> dict:
        sub = self._subs.get(sub_id, {})
        sub["state"] = "active"
        sub["paid_until"] = int(time.time()) + days * 86400
        sub["grace_days"] = 0
        return sub

    def check_grace(self, sub_id: str, grace_period_days: int = 7) -> dict:
        sub = self._subs.get(sub_id, {})
        if sub.get("state") != "active":
            return sub
        now = int(time.time())
        if now > sub["paid_until"]:
            expired_days = (now - sub["paid_until"]) // 86400
            if expired_days > grace_period_days:
                sub["state"] = "inactive"
            else:
                sub["state"] = "pending"
                sub["grace_days"] = grace_period_days - expired_days
        return sub

    def get(self, sub_id: str) -> dict:
        return self._subs.get(sub_id, {})


class ProductManager:
    """Paywalled URL products: pay → get access token → redirect."""

    def __init__(self) -> None:
        self._products: dict[str, dict] = {}
        self._access_tokens: dict[str, dict] = {}
        import secrets

        self._secrets = secrets

    def create(
        self,
        slug: str,
        amount: float,
        asset: str = "USDC",
        network: str = "eip155:8453",
        redirect_url: str = "",
        access_ttl: int = 3600,
        payee: str = "",
    ) -> dict:
        self._products[slug] = {
            "slug": slug,
            "amount": amount,
            "asset": asset,
            "network": network,
            "redirect_url": redirect_url,
            "access_ttl": access_ttl,
            "payee": payee,
        }
        return self._products[slug]

    def grant_access(self, slug: str, payment_id: str = "") -> dict:
        product = self._products.get(slug)
        if not product:
            return {"error": "product not found"}
        token = self._secrets.token_urlsafe(32)
        self._access_tokens[token] = {
            "slug": slug,
            "expires_at": int(time.time()) + product["access_ttl"],
            "payment_id": payment_id,
        }
        return {
            "access_token": token,
            "redirect_url": product["redirect_url"],
            "expires_in": product["access_ttl"],
        }

    def verify_access(self, token: str) -> dict:
        access = self._access_tokens.get(token)
        if not access:
            return {"valid": False, "error": "invalid token"}
        if int(time.time()) > access["expires_at"]:
            del self._access_tokens[token]
            return {"valid": False, "error": "token expired"}
        return {"valid": True, "slug": access["slug"], "payment_id": access.get("payment_id", "")}

    def list(self) -> list[dict]:
        return list(self._products.values())
