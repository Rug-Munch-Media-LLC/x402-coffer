# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Rug Munch Media LLC
# --- facilitator_health.py: facilitator health tracking + circuit breaker ---
from __future__ import annotations

import logging
import time

from .facilitator_config import FacilitatorConfig

log = logging.getLogger(__name__)

FAILURE_THRESHOLD = 3
COOLDOWN_SECONDS = 60
LATENCY_WINDOW = 100


class FacilitatorHealth:
    """Health tracker for external facilitators.

    Circuit breaker: after ``FAILURE_THRESHOLD`` consecutive failures a
    facilitator is considered unhealthy until ``COOLDOWN_SECONDS`` elapse
    (half-open probe). Health score feeds weighted-random selection.
    """

    def __init__(self) -> None:
        self._failures: dict[str, tuple[int, float]] = {}
        self._successes: dict[str, int] = {}
        self._latencies: dict[str, list[float]] = {}

    def is_healthy(self, config: FacilitatorConfig) -> bool:
        entry = self._failures.get(config.name)
        if entry is None:
            return True
        count, last_failure = entry
        if count >= FAILURE_THRESHOLD and time.time() - last_failure > COOLDOWN_SECONDS:
            return True  # half-open: allow a probe after the cooldown window
        return count < FAILURE_THRESHOLD

    def health_score(self, config: FacilitatorConfig) -> float:
        successes = self._successes.get(config.name, 0)
        failures = self._failures.get(config.name, (0, 0))[0]
        latencies = self._latencies.get(config.name, [])
        avg_latency = sum(latencies) / len(latencies) if latencies else 0.0
        return successes - failures * 2 - avg_latency

    def mark_failure(self, config: FacilitatorConfig) -> None:
        count, _ = self._failures.get(config.name, (0, 0.0))
        self._failures[config.name] = (count + 1, time.time())
        log.warning("x402_facilitator_failure", extra={"facilitator": config.name})

    def mark_success(self, config: FacilitatorConfig, start_time: float | None = None) -> None:
        self._successes[config.name] = self._successes.get(config.name, 0) + 1
        if start_time is not None:
            latencies = self._latencies.setdefault(config.name, [])
            latencies.append(time.time() - start_time)
            if len(latencies) > LATENCY_WINDOW:
                del latencies[: len(latencies) - LATENCY_WINDOW]
        entry = self._failures.get(config.name)
        if entry is None:
            return
        count, _ = entry
        if count <= 1:
            self._failures.pop(config.name, None)
        else:
            self._failures[config.name] = (count - 1, time.time())
