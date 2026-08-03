"""Validated per-turn latency samples and deterministic summaries."""

from __future__ import annotations

import math
from collections import defaultdict
from datetime import UTC, datetime
from threading import Lock
from typing import Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, field_validator

LatencyMetric: TypeAlias = Literal[
    "stt_partial",
    "stt_final",
    "llm_ttft",
    "tts_ttfb",
    "e2e",
]


class LatencySample(BaseModel):
    """One subsystem measurement for one conversation turn."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    turn_id: str
    turn_index: int
    metric: LatencyMetric
    duration_ms: float
    observed_at: datetime

    @field_validator("turn_id")
    @classmethod
    def valid_turn_id(cls, value: str) -> str:
        if not value:
            msg = "turn_id is empty. Fix: use the runtime's stable turn identifier."
            raise ValueError(msg)
        return value

    @field_validator("turn_index")
    @classmethod
    def valid_turn_index(cls, value: int) -> int:
        if value < 1:
            msg = "turn_index must start at 1. Fix: number conversation turns from 1."
            raise ValueError(msg)
        return value

    @field_validator("duration_ms")
    @classmethod
    def valid_duration(cls, value: float) -> float:
        if not math.isfinite(value) or value < 0:
            msg = "duration_ms is invalid. Fix: record a finite, non-negative duration."
            raise ValueError(msg)
        return value

    @field_validator("observed_at")
    @classmethod
    def timezone_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            msg = "observed_at lacks a timezone. Fix: record a UTC-aware datetime."
            raise ValueError(msg)
        return value.astimezone(UTC)


class LatencySummary(BaseModel):
    """Nearest-rank latency aggregate used by UI badges and gates."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    count: int
    p50_ms: float
    p95_ms: float
    max_ms: float


class LatencySeries:
    """Thread-safe append-only in-memory latency series for an active call."""

    def __init__(self) -> None:
        self._samples: list[LatencySample] = []
        self._lock = Lock()

    def record(
        self,
        *,
        turn_id: str,
        turn_index: int,
        metric: LatencyMetric,
        duration_ms: float,
        observed_at: datetime | None = None,
    ) -> LatencySample:
        """Validate and append one measurement."""
        sample = LatencySample(
            turn_id=turn_id,
            turn_index=turn_index,
            metric=metric,
            duration_ms=duration_ms,
            observed_at=observed_at or datetime.now(UTC),
        )
        with self._lock:
            self._samples.append(sample)
        return sample

    def snapshot(self) -> tuple[LatencySample, ...]:
        """Return an immutable insertion-order snapshot."""
        with self._lock:
            return tuple(self._samples)

    def for_turn(self, turn_id: str) -> tuple[LatencySample, ...]:
        """Return every sample for a stable runtime turn id."""
        return tuple(sample for sample in self.snapshot() if sample.turn_id == turn_id)

    def summaries(self) -> dict[LatencyMetric, LatencySummary]:
        """Aggregate each populated metric with deterministic nearest ranks."""
        grouped: defaultdict[LatencyMetric, list[float]] = defaultdict(list)
        for sample in self.snapshot():
            grouped[sample.metric].append(sample.duration_ms)
        return {
            metric: LatencySummary(
                count=len(values),
                p50_ms=_nearest_rank(values, 0.50),
                p95_ms=_nearest_rank(values, 0.95),
                max_ms=max(values),
            )
            for metric, values in grouped.items()
        }


def _nearest_rank(values: list[float], percentile: float) -> float:
    ordered = sorted(values)
    index = max(0, math.ceil(percentile * len(ordered)) - 1)
    return ordered[index]
