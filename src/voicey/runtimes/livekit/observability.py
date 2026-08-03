"""Incremental LiveKit session persistence from current native events."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Coroutine
from datetime import UTC, datetime
from typing import Any, cast

from livekit.agents import (
    AgentSession,
    AgentStateChangedEvent,
    ConversationItemAddedEvent,
    FunctionToolsExecutedEvent,
    UserStateChangedEvent,
)
from livekit.agents.llm import ChatMessage
from pydantic import JsonValue

from voicey.errors import VoiceyError
from voicey.obs.latency import LatencyMetric, LatencySample
from voicey.obs.records import TimelineEvent, TranscriptRole, TranscriptTurn
from voicey.runtimes.livekit.lifecycle import LiveKitRepository


class LiveKitObservationBridge:
    """Persist native events immediately rather than relying on the final report."""

    def __init__(
        self,
        *,
        call_id: str,
        store: LiveKitRepository,
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
        self._tasks: set[asyncio.Task[None]] = set()
        self._failure: BaseException | None = None
        self.interruptions = 0

    @property
    def turn_index(self) -> int:
        return max(1, self._turn_index)

    def attach(self, session: AgentSession[Any]) -> None:
        """Register synchronous emit callbacks that schedule protected writes."""

        def conversation_item_added(event: ConversationItemAddedEvent) -> None:
            self._spawn(self.on_conversation_item(event))

        def user_state_changed(event: UserStateChangedEvent) -> None:
            self._spawn(self.on_user_state_changed(event))

        def agent_state_changed(event: AgentStateChangedEvent) -> None:
            self._spawn(
                self.timeline(
                    "runtime.agent_state",
                    old_state=event.old_state,
                    new_state=event.new_state,
                )
            )

        def function_tools_executed(event: FunctionToolsExecutedEvent) -> None:
            self._spawn(
                self.timeline(
                    "runtime.tools_executed",
                    count=len(event.function_calls),
                    names=",".join(call.name for call in event.function_calls),
                )
            )

        session.on("conversation_item_added", conversation_item_added)
        session.on("user_state_changed", user_state_changed)
        session.on("agent_state_changed", agent_state_changed)
        session.on("function_tools_executed", function_tools_executed)

    async def on_conversation_item(self, event: ConversationItemAddedEvent) -> None:
        """Persist one final user/assistant message and its attached metrics."""
        item = event.item
        if not isinstance(item, ChatMessage) or item.role not in {"user", "assistant"}:
            return
        text = item.text_content
        if not text:
            return
        async with self._lock:
            role = cast("TranscriptRole", item.role)
            if role == "user":
                self._turn_index += 1
            elif self._turn_index == 0:
                self._turn_index = 1
            await self._append_transcript(role, text)
            await self._persist_message_metrics(item)
            if role == "assistant":
                if item.interrupted:
                    self.interruptions += 1
                    await self.timeline("runtime.interrupted")
                normalized = text.casefold()
                if any(phrase in normalized for phrase in self._end_call_phrases):
                    await self.timeline("runtime.end_phrase")
                    await self._on_end_phrase()

    async def on_user_state_changed(self, event: UserStateChangedEvent) -> None:
        await self.timeline(
            "runtime.user_state",
            old_state=event.old_state,
            new_state=event.new_state,
        )
        if event.new_state == "away":
            await self.timeline("runtime.user_idle")
            await self._on_user_idle()

    async def timeline(self, event_type: str, **details: str | int | float | bool) -> None:
        await self._store.append_timeline(
            self.call_id,
            TimelineEvent(
                event_type=event_type,
                details=cast("dict[str, JsonValue]", details),
            ),
        )

    def schedule_timeline(
        self,
        event_type: str,
        **details: str | int | float | bool,
    ) -> None:
        """Schedule a timeline write from LiveKit's synchronous emitter."""
        self._spawn(self.timeline(event_type, **details))

    async def drain(self) -> None:
        """Wait for all event writes and surface a persistence failure."""
        while self._tasks:
            await asyncio.gather(*tuple(self._tasks), return_exceptions=True)
        if self._failure is not None:
            raise VoiceyError(
                "VY-RUN-006",
                detail=f"incremental LiveKit persistence failed for {self.call_id}.",
            ) from self._failure

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

    async def _persist_message_metrics(self, message: ChatMessage) -> None:
        mappings: tuple[tuple[str, LatencyMetric], ...]
        if message.role == "user":
            mappings = (("transcription_delay", "stt_final"),)
        else:
            mappings = (
                ("llm_node_ttft", "llm_ttft"),
                ("tts_node_ttfb", "tts_ttfb"),
                ("e2e_latency", "e2e"),
            )
        for key, metric in mappings:
            value = message.metrics.get(key)
            if value is not None:
                await self._store.record_latency(
                    self.call_id,
                    LatencySample(
                        turn_id=f"turn_{self.turn_index:04d}",
                        turn_index=self.turn_index,
                        metric=metric,
                        duration_ms=max(0, value * 1000),
                        observed_at=datetime.now(UTC),
                    ),
                )

    def _spawn(self, coroutine: Coroutine[Any, Any, None]) -> None:
        task = asyncio.create_task(
            coroutine,
            name=f"voicey-livekit-observation-{self.call_id}",
        )
        self._tasks.add(task)

        def completed(done: asyncio.Task[None]) -> None:
            self._tasks.discard(done)
            if done.cancelled():
                return
            exception = done.exception()
            if exception is not None and self._failure is None:
                self._failure = exception

        task.add_done_callback(completed)
