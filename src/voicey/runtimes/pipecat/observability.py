"""Incremental transcript and latency persistence from native Pipecat events."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Protocol, cast

from pipecat.observers.user_bot_latency_observer import (
    LatencyBreakdown,
    UserBotLatencyObserver,
)
from pipecat.processors.aggregators.llm_response_universal import (
    AssistantTurnStoppedMessage,
    LLMContextAggregatorPair,
    UserTurnMessageAddedMessage,
)
from pydantic import JsonValue

from voicey.obs.latency import LatencyMetric, LatencySample
from voicey.obs.records import TimelineEvent, TranscriptRole, TranscriptTurn


class ObservationStore(Protocol):
    """Protected store operations consumed by the Pipecat bridge."""

    async def append_timeline(self, call_id: str, event: TimelineEvent) -> None: ...

    async def append_transcript(self, call_id: str, turn: TranscriptTurn) -> None: ...

    async def record_latency(self, call_id: str, sample: LatencySample) -> None: ...


class PipecatObservationBridge:
    """Persist native aggregator/observer events as runtime-neutral records."""

    def __init__(
        self,
        *,
        call_id: str,
        store: ObservationStore,
        end_call_phrases: tuple[str, ...],
        on_user_idle: Callable[[], Awaitable[None]],
        on_end_phrase: Callable[[], Awaitable[None]],
    ) -> None:
        self.call_id = call_id
        self._store = store
        self._end_call_phrases = end_call_phrases
        self._on_user_idle = on_user_idle
        self._on_end_phrase = on_end_phrase
        self._started = time.monotonic()
        self._turn_index = 0
        self._lock = asyncio.Lock()
        self.interruptions = 0
        self.latency_observer = UserBotLatencyObserver()

    @property
    def turn_index(self) -> int:
        return max(1, self._turn_index)

    def attach(self, aggregators: LLMContextAggregatorPair) -> None:
        """Register handlers on the installed universal aggregators and observer."""

        @aggregators.user().event_handler("on_user_turn_message_added")
        async def on_user_turn_message_added(  # pyright: ignore[reportUnusedFunction]
            _aggregator: object,
            message: UserTurnMessageAddedMessage,
        ) -> None:
            async with self._lock:
                self._turn_index += 1
                await self._append_transcript("user", message.content)

        @aggregators.user().event_handler("on_user_turn_idle")
        async def on_user_turn_idle(  # pyright: ignore[reportUnusedFunction]
            _aggregator: object,
        ) -> None:
            await self.timeline("runtime.user_idle")
            await self._on_user_idle()

        @aggregators.assistant().event_handler("on_assistant_turn_stopped")
        async def on_assistant_turn_stopped(  # pyright: ignore[reportUnusedFunction]
            _aggregator: object,
            message: AssistantTurnStoppedMessage,
        ) -> None:
            async with self._lock:
                if self._turn_index == 0:
                    self._turn_index = 1
                if message.content:
                    await self._append_transcript("assistant", message.content)
                if message.interrupted:
                    self.interruptions += 1
                    await self.timeline("runtime.interrupted")
                normalized = message.content.casefold()
                if any(phrase in normalized for phrase in self._end_call_phrases):
                    await self.timeline("runtime.end_phrase")
                    await self._on_end_phrase()

        @self.latency_observer.event_handler("on_latency_measured")
        async def on_latency_measured(  # pyright: ignore[reportUnusedFunction]
            _observer: UserBotLatencyObserver,
            latency_seconds: float,
        ) -> None:
            await self._record_latency("e2e", latency_seconds)

        @self.latency_observer.event_handler("on_latency_breakdown")
        async def on_latency_breakdown(  # pyright: ignore[reportUnusedFunction]
            _observer: UserBotLatencyObserver,
            breakdown: LatencyBreakdown,
        ) -> None:
            for measurement in breakdown.ttfb:
                metric = _processor_metric(measurement.processor)
                if metric is not None:
                    await self._record_latency(metric, measurement.duration_secs)

    async def timeline(self, event_type: str, **details: str | int | float | bool) -> None:
        await self._store.append_timeline(
            self.call_id,
            TimelineEvent(
                event_type=event_type,
                details=cast("dict[str, JsonValue]", details),
            ),
        )

    async def _append_transcript(self, role: TranscriptRole, text: str) -> None:
        await self._store.append_transcript(
            self.call_id,
            TranscriptTurn(
                turn_id=f"turn_{self.turn_index:04d}",
                role=role,
                text=text,
                t_ms=max(0, round((time.monotonic() - self._started) * 1000)),
            ),
        )

    async def _record_latency(
        self,
        metric: LatencyMetric,
        duration_seconds: float,
    ) -> None:
        await self._store.record_latency(
            self.call_id,
            LatencySample(
                turn_id=f"turn_{self.turn_index:04d}",
                turn_index=self.turn_index,
                metric=metric,
                duration_ms=max(0, duration_seconds * 1000),
                observed_at=datetime.now(UTC),
            ),
        )


def _processor_metric(processor: str) -> LatencyMetric | None:
    normalized = processor.casefold()
    if "stt" in normalized:
        return "stt_final"
    if "llm" in normalized:
        return "llm_ttft"
    if "tts" in normalized:
        return "tts_ttfb"
    return None
