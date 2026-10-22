"""Coffer — durable, append-only per-key usage ledger (JSONL).

Parity reference (Pry): ``pryscraper/src/pry/payments/x402/ledger_store.py`` and
``src/pry/payments/x402/handler_state_ledger.py`` — every settled payment lands
one ledger row carrying the key, route, network, asset, amount and the
settlement key, so billing history and per-key budgets read from one durable
source rather than an in-process dict.

Coffer gap this closes: ``coffer/_engine/tracking.py`` keeps ``UsageTracker``
counters in memory only (lost on restart, invisible to a second worker), while
``settlements.jsonl`` records the *receipt* without the key/route/outcome axis
budgets need. This module is deliberately small: one append primitive with
``O_APPEND`` + fsync (corruption-resistant under concurrent writers), one
reader, one summariser.

Row schema (stable, additive only)::

    {"ts": ISO8601, "key_id": str, "payment_id": str, "route": str,
     "network": str, "asset": str, "amount_atomic": int, "tx_hash": str,
     "outcome": "settled"|"released"|"failed", "settle_ms": int,
     "reservation_id": str, "extra": {...}}
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

LEDGER_FILENAME = "usage_ledger.jsonl"


def record_usage(path: Path | str, *, key_id: str, outcome: str, **fields: Any) -> dict[str, Any]:
    """Append one usage row and return it (fsynced before returning)."""
    if outcome not in {"settled", "released", "failed"}:
        raise ValueError(f"unsupported usage outcome: {outcome!r}")
    row: dict[str, Any] = {
        "ts": datetime.now(UTC).isoformat(),
        "key_id": str(key_id),
        "payment_id": str(fields.pop("payment_id", "")),
        "route": str(fields.pop("route", "")),
        "network": str(fields.pop("network", "")),
        "asset": str(fields.pop("asset", "")),
        "amount_atomic": int(fields.pop("amount_atomic", 0) or 0),
        "tx_hash": str(fields.pop("tx_hash", "")),
        "outcome": outcome,
        "settle_ms": int(fields.pop("settle_ms", 0) or 0),
        "reservation_id": str(fields.pop("reservation_id", "")),
        "extra": fields or {},
    }
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    line = (json.dumps(row, separators=(",", ":"), default=str) + "\n").encode()
    # O_APPEND is atomic for writes <= PIPE_BUF on POSIX; fsync makes the row
    # survive a crash instead of vanishing with the page cache.
    fd = os.open(target, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o640)
    try:
        os.write(fd, line)
        os.fsync(fd)
    finally:
        os.close(fd)
    return row


def read_usage(path: Path | str, *, key_id: str = "") -> list[dict[str, Any]]:
    """Read ledger rows (optionally one key). Corrupt lines are skipped, not fatal."""
    target = Path(path)
    if not target.exists():
        return []
    rows: list[dict[str, Any]] = []
    with target.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(row, dict):
                continue
            if key_id and row.get("key_id") != key_id:
                continue
            rows.append(row)
    return rows


def summarize_usage(path: Path | str, *, key_id: str = "") -> dict[str, Any]:
    """Aggregate settled spend and volume per key (budget reconciliation input)."""
    rows: Iterable[dict[str, Any]] = read_usage(path, key_id=key_id)
    total_atomic = 0
    by_outcome: dict[str, int] = {}
    by_key: dict[str, int] = {}
    for row in rows:
        outcome = str(row.get("outcome", ""))
        by_outcome[outcome] = by_outcome.get(outcome, 0) + 1
        amount = int(row.get("amount_atomic", 0) or 0)
        if outcome == "settled":
            total_atomic += amount
            by_key[str(row.get("key_id", ""))] = by_key.get(str(row.get("key_id", "")), 0) + amount
    return {
        "rows": sum(by_outcome.values()),
        "settled_atomic": total_atomic,
        "by_outcome": by_outcome,
        "settled_by_key": by_key,
    }
