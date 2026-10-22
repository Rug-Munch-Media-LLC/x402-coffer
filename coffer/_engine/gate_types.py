"""Coffer — shared gate types (decision + wiring policy).

Split out so :mod:`coffer._engine.gate` (decision order) and
:mod:`coffer._engine.gate_settle` (settle orchestration) can both depend on the
types without importing each other.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .budgets import KeyBudgetLedger
from .store import PaymentStore

__all__ = ["GateDecision", "GatePolicy", "Settler"]

Settler = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


@dataclass(frozen=True)
class GateDecision:
    """Outcome of one authorisation attempt (paid or refuse-with-reason)."""

    paid: bool
    reason: str = ""
    status: int = 402
    receipt: dict[str, Any] | None = None
    headers: Mapping[str, str] = field(default_factory=dict)
    retry_after: int = 0


@dataclass
class GatePolicy:
    """Wiring for the gate. Every field optional => legacy behaviour."""

    store: PaymentStore | None = None
    budgets: KeyBudgetLedger | None = None
    ledger_path: Path | None = None
    settler: Settler | None = None
    require_attestation: bool = False
    operation: str = "api_access"
    required_atomic: int = 0
