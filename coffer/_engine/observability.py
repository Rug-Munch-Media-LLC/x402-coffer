"""Settlement observability — bounded ledger + metrics for x402 Coffer.

Records every verification and settlement outcome so operators can answer
"what did we actually get paid?" without touching chain explorers.

Design rules:
* Observability must NEVER break a payment — all hooks are fail-safe.
* Bounded memory (eviction like every other Coffer store).
* Honest counters only reflect what actually happened on this instance.
"""

from __future__ import annotations

import threading
from collections import OrderedDict, defaultdict
from datetime import UTC, datetime
from typing import Any

DEFAULT_LEDGER_MAX = 50_000


# ── Prometheus metrics (dashboard parity with pryscraper) ───────────────────
# Mirrors pryscraper:src/pry/payments/x402/metrics.py so the same Grafana
# dashboards (grafana/dashboards/x402-payments.json) work for both services.
# All metrics are optional: if ``prometheus_client`` is unavailable the
# helpers become no-ops so the payment flow never breaks on observability.

try:
    from prometheus_client import Counter, Gauge, Histogram

    _has_prometheus = True
except ImportError:  # pragma: no cover — guard anyway, observability never breaks payments
    _has_prometheus = False

CHALLENGES_SENT: Any = None
VERIFY_SUCCESS: Any = None
VERIFY_FAILURE: Any = None
SETTLE_TOTAL: Any = None
SETTLE_SUCCESS: Any = None
REPLAY_DETECTED: Any = None
RPC_LATENCY: Any = None
SETTLEMENT_VOLUME_USD: Any = None
FACILITATOR_HEALTH: Any = None
CHAIN_ACTIVE: Any = None
OPERATION_CALLS: Any = None

if _has_prometheus:
    CHALLENGES_SENT = Counter(
        "x402_challenges_sent_total",
        "402 payment challenges (PAYMENT-REQUIRED responses) sent",
        ["operation", "network"],
    )
    VERIFY_SUCCESS = Counter(
        "x402_verify_success_total",
        "Successful payment verifications",
        ["network"],
    )
    VERIFY_FAILURE = Counter(
        "x402_verify_failure_total",
        "Failed payment verifications",
        ["network", "reason"],
    )
    SETTLE_TOTAL = Counter(
        "x402_settle_total",
        "Settlement attempts (all outcomes)",
        ["network"],
    )
    SETTLE_SUCCESS = Counter(
        "x402_settle_success_total",
        "Successful settlements",
        ["network"],
    )
    REPLAY_DETECTED = Counter(
        "x402_replay_detected_total",
        "Replayed tx_hash / payment attempts rejected",
        ["network"],
    )
    RPC_LATENCY = Histogram(
        "x402_rpc_latency_seconds",
        "Blockchain RPC call latency",
        ["network", "method"],
    )
    SETTLEMENT_VOLUME_USD = Counter(
        "x402_settlement_volume_usd_total",
        "Cumulative settled volume in USD",
        ["network", "asset"],
    )
    FACILITATOR_HEALTH = Gauge(
        "x402_facilitator_health",
        "Facilitator health (1 = healthy, 0 = failing)",
        ["facilitator"],
    )
    CHAIN_ACTIVE = Gauge(
        "x402_chain_active",
        "Chain activity (1 = verify/settle attempted, 0 = unsupported)",
        ["network"],
    )
    OPERATION_CALLS = Counter(
        "x402_operation_calls_total",
        "Gated operation calls",
        ["operation"],
    )


def _safe(metric: Any, **labels: str) -> Any | None:
    """Return a labeled child, or None when metrics are unavailable."""
    if metric is None:
        return None
    try:
        return metric.labels(**labels)
    except (ValueError, TypeError):  # unexpected label set — never break the flow
        return None


def record_challenge(operation: str, network: str) -> None:
    child = _safe(CHALLENGES_SENT, operation=operation or "unknown", network=network or "unknown")
    if child:
        child.inc()


def record_verify_success(network: str) -> None:
    child = _safe(VERIFY_SUCCESS, network=network or "unknown")
    if child:
        child.inc()


def record_verify_failure(network: str, reason: str) -> None:
    child = _safe(VERIFY_FAILURE, network=network or "unknown", reason=(reason or "unknown")[:64])
    if child:
        child.inc()


def record_settle(network: str, success: bool, amount_usd: float = 0.0, asset: str = "") -> None:
    child = _safe(SETTLE_TOTAL, network=network or "unknown")
    if child:
        child.inc()
    if success:
        child = _safe(SETTLE_SUCCESS, network=network or "unknown")
        if child:
            child.inc()
        if amount_usd > 0:
            child = _safe(SETTLEMENT_VOLUME_USD, network=network or "unknown", asset=asset or "unknown")
            if child:
                child.inc(amount_usd)


def record_replay(network: str) -> None:
    child = _safe(REPLAY_DETECTED, network=network or "unknown")
    if child:
        child.inc()


def record_rpc_latency(network: str, method: str, seconds: float) -> None:
    child = _safe(RPC_LATENCY, network=network or "unknown", method=method or "unknown")
    if child:
        child.observe(max(0.0, seconds))


def set_facilitator_health(name: str, healthy: bool) -> None:
    child = _safe(FACILITATOR_HEALTH, facilitator=name or "unknown")
    if child:
        child.set(1 if healthy else 0)


def set_chain_active(network: str, active: bool) -> None:
    child = _safe(CHAIN_ACTIVE, network=network or "unknown")
    if child:
        child.set(1 if active else 0)


def record_operation_call(operation: str) -> None:
    child = _safe(OPERATION_CALLS, operation=operation or "unknown")
    if child:
        child.inc()


class SettlementLedger:
    """Bounded record of verify/settle outcomes + aggregate counters."""

    def __init__(self, max_entries: int = DEFAULT_LEDGER_MAX) -> None:
        self._max = int(max_entries)
        self._lock = threading.Lock()
        self._events: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._counters: defaultdict[str, int] = defaultdict(int)
        self._by_network: defaultdict[str, defaultdict[str, int]] = defaultdict(lambda: defaultdict(int))

    def _bump(self, key: str, network: str = "") -> None:
        with self._lock:
            self._counters[key] += 1
            if network:
                self._by_network[network][key] += 1

    def record_verify(
        self,
        *,
        payment_id: str,
        tx_hash: str,
        network: str,
        asset: str,
        amount_atomic: int,
        verified: bool,
        reason: str = "",
        facilitator: str = "",
    ) -> None:
        """Record a verification outcome (never raises)."""
        try:
            key = "verifications_verified" if verified else "verifications_rejected"
            self._bump(key, network)
            entry = {
                "kind": "verify",
                "payment_id": payment_id,
                "tx_hash": tx_hash,
                "network": network,
                "asset": asset,
                "amount_atomic": int(amount_atomic),
                "verified": bool(verified),
                "reason": reason,
                "facilitator": facilitator,
                "at": datetime.now(UTC).isoformat(),
            }
            with self._lock:
                self._events[f"v:{payment_id}:{entry['at']}"] = entry
                while len(self._events) > self._max:
                    self._events.popitem(last=False)
        except Exception:  # noqa: BLE001, S110 — observability never breaks payments
            pass

    def record_settle(
        self,
        *,
        payment_id: str,
        tx_hash: str,
        network: str,
        asset: str,
        amount_atomic: int,
        success: bool,
        reason: str = "",
        facilitator: str = "",
    ) -> None:
        """Record a settlement outcome (never raises)."""
        try:
            key = "settlements_success" if success else "settlements_failed"
            self._bump(key, network)
            entry = {
                "kind": "settle",
                "payment_id": payment_id,
                "tx_hash": tx_hash,
                "network": network,
                "asset": asset,
                "amount_atomic": int(amount_atomic),
                "success": bool(success),
                "reason": reason,
                "facilitator": facilitator,
                "at": datetime.now(UTC).isoformat(),
            }
            with self._lock:
                self._events[f"s:{payment_id}:{entry['at']}"] = entry
                while len(self._events) > self._max:
                    self._events.popitem(last=False)
        except Exception:  # noqa: BLE001, S110
            pass

    def snapshot(self) -> dict[str, Any]:
        """Aggregate metrics snapshot."""
        with self._lock:
            counters = dict(self._counters)
            by_network = {n: dict(c) for n, c in self._by_network.items()}
            total_events = len(self._events)
        paid_atomic = sum(
            e.get("amount_atomic", 0) for e in self.recent_events(kind="settle") if e.get("success")
        )
        return {
            "counters": counters,
            "by_network": by_network,
            "settled_amount_atomic_total": paid_atomic,
            "recent_settlements": self.recent_events(kind="settle", limit=12),
            "ledger_entries": total_events,
            "generated_at": datetime.now(UTC).isoformat(),
        }

    def recent_events(self, *, kind: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        """Newest-first event list, optionally filtered by kind."""
        with self._lock:
            items = reversed(list(self._events.values()))
        out = [e for e in items if kind is None or e.get("kind") == kind]
        return out[:limit]


_ledger: SettlementLedger | None = None


def get_ledger() -> SettlementLedger:
    """Global ledger singleton."""
    global _ledger
    if _ledger is None:
        _ledger = SettlementLedger()
    return _ledger


def observe_verify(result: dict[str, Any], *, payment_id: str, tx_hash: str) -> None:
    """Fail-safe hook: record a verify result dict from FacilitatorRouter."""
    try:
        get_ledger().record_verify(
            payment_id=payment_id,
            tx_hash=tx_hash,
            network=str(result.get("network", "")),
            asset=str(result.get("asset", "")),
            amount_atomic=int(result.get("amount_atomic", 0) or 0),
            verified=bool(result.get("verified")),
            reason=str(result.get("reason", "")),
            facilitator=str(result.get("facilitator", "")),
        )
    except Exception:  # noqa: BLE001, S110
        pass


def observe_settle(result: dict[str, Any], *, payment_id: str, tx_hash: str) -> None:
    """Fail-safe hook: record a settle result dict from FacilitatorRouter."""
    try:
        get_ledger().record_settle(
            payment_id=payment_id,
            tx_hash=str(result.get("transaction", tx_hash)),
            network=str(result.get("network", "")),
            asset=str(result.get("asset", "")),
            amount_atomic=int(result.get("amount_atomic", 0) or 0),
            success=bool(result.get("success")),
            reason=str(result.get("reason", "")),
            facilitator=str(result.get("facilitator", "")),
        )
    except Exception:  # noqa: BLE001, S110
        pass


def _prometheus_text(snapshot: dict[str, Any]) -> str:
    # When prometheus_client is available, emit the full registry (includes
    # the x402_* dashboard metrics above) plus the ledger counters.
    if _has_prometheus:
        try:
            from prometheus_client import generate_latest

            registry_text = generate_latest().decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001 — observability never breaks payments
            registry_text = ""
    else:
        registry_text = ""
    lines = ["# TYPE coffer_verifications_total counter", "# TYPE coffer_settlements_total counter"]
    c = snapshot["counters"]
    lines.append(f'coffer_verifications_total{{outcome="verified"}} {c.get("verifications_verified", 0)}')
    lines.append(f'coffer_verifications_total{{outcome="rejected"}} {c.get("verifications_rejected", 0)}')
    lines.append(f'coffer_settlements_total{{outcome="success"}} {c.get("settlements_success", 0)}')
    lines.append(f'coffer_settlements_total{{outcome="failed"}} {c.get("settlements_failed", 0)}')
    lines.append(f"coffer_settled_amount_atomic_total {snapshot['settled_amount_atomic_total']}")
    body = "\n".join(lines) + "\n"
    if registry_text:
        body = registry_text + body
    return body


def register_metrics_router() -> Any:
    """APIRouter: ``GET /v1/x402/metrics`` (json | prometheus)."""
    from fastapi import APIRouter
    from fastapi.responses import PlainTextResponse

    router = APIRouter(prefix="/v1/x402/metrics", tags=["x402-observability"])

    @router.get("")
    async def metrics(format: str = "json") -> Any:
        snap = get_ledger().snapshot()
        if format == "prometheus":
            return PlainTextResponse(_prometheus_text(snap), media_type="text/plain")
        return snap

    return router
