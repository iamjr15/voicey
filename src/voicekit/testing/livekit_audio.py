"""Real PCM sim-caller bridge for LiveKit's non-room audio test tier."""

from __future__ import annotations

import asyncio
import time
from contextlib import suppress
from pathlib import Path
from typing import Any

from livekit import rtc
from livekit.agents import AgentSession
from livekit.agents.voice.io import (
    AudioInput,
    AudioOutput,
    AudioOutputCapabilities,
)
from pipecat.evals.speech import EvalSpeech
from pipecat.evals.transcribe import EvalTranscriber

from voicekit import results
from voicekit.runtimes.livekit.flow import load_native_agent
from voicekit.runtimes.livekit.mapping import LiveKitPolicy, detector_mode
from voicekit.runtimes.livekit.providers import (
    DefaultLiveKitProviderFactory,
    build_livekit_services,
)
from voicekit.runtimes.livekit.tools import shared_livekit_tools
from voicekit.testing.models import JudgeConfig, ScenarioDefinition, ScenarioTurn
from voicekit.testing.reporting import AttemptResult
from voicekit.testing.sim_caller import OpenAICompatibleClient, TranscriptJudge


class QueueAudioInput(AudioInput):
    """Attachable virtual microphone with real-time PCM pacing."""

    def __init__(self) -> None:
        super().__init__(label="voicekit-sim-caller")
        self._frames: asyncio.Queue[rtc.AudioFrame | None] = asyncio.Queue()

    async def __anext__(self) -> rtc.AudioFrame:
        frame = await self._frames.get()
        if frame is None:
            raise StopAsyncIteration
        return frame

    async def speak(
        self,
        pcm: bytes,
        *,
        sample_rate: int,
        trailing_silence_s: float = 0.8,
    ) -> None:
        samples = sample_rate // 50
        bytes_per_frame = samples * 2
        silence_frames = int(trailing_silence_s * 50)
        for offset in range(0, len(pcm), bytes_per_frame):
            chunk = pcm[offset : offset + bytes_per_frame]
            if len(chunk) < bytes_per_frame:
                chunk += b"\0" * (bytes_per_frame - len(chunk))
            await self._frames.put(
                rtc.AudioFrame(
                    data=chunk,
                    sample_rate=sample_rate,
                    num_channels=1,
                    samples_per_channel=samples,
                )
            )
            await asyncio.sleep(0.02)
        silence = b"\0" * bytes_per_frame
        for _ in range(silence_frames):
            await self._frames.put(
                rtc.AudioFrame(
                    data=silence,
                    sample_rate=sample_rate,
                    num_channels=1,
                    samples_per_channel=samples,
                )
            )
            await asyncio.sleep(0.02)

    async def aclose(self) -> None:
        await self._frames.put(None)


class CapturingAudioOutput(AudioOutput):
    """Virtual speaker that preserves complete TTS segments for transcription."""

    def __init__(self) -> None:
        super().__init__(
            label="voicekit-agent-speaker",
            capabilities=AudioOutputCapabilities(pause=False),
        )
        self._current = bytearray()
        self._sample_rate = 16000
        self._segments: asyncio.Queue[tuple[bytes, int]] = asyncio.Queue()
        self._duration = 0.0

    async def capture_frame(self, frame: rtc.AudioFrame) -> None:
        await super().capture_frame(frame)
        if not self._current:
            self.on_playback_started(created_at=time.time())
        self._sample_rate = frame.sample_rate
        self._current.extend(frame.data)
        self._duration += frame.duration

    def flush(self) -> None:
        super().flush()
        if self._current:
            self._segments.put_nowait((bytes(self._current), self._sample_rate))
            self._current.clear()
            self.on_playback_finished(
                playback_position=self._duration,
                interrupted=False,
            )
            self._duration = 0.0

    def clear_buffer(self) -> None:
        super().flush()
        if self._current:
            self._current.clear()
            self.on_playback_finished(
                playback_position=self._duration,
                interrupted=True,
            )
            self._duration = 0.0

    async def next_segment(self, timeout_s: float) -> tuple[bytes, int]:
        return await asyncio.wait_for(self._segments.get(), timeout=timeout_s)


async def execute_audio_case(
    root: Path,
    definition: ScenarioDefinition,
    turns: tuple[ScenarioTurn, ...],
    *,
    judge: JudgeConfig,
    environment: dict[str, str],
) -> AttemptResult:
    """Drive the complete LiveKit STT→LLM→TTS path with synthesized caller PCM."""
    from voicekit.testing.runner import (
        MemorySink,
        hard_result_failures,
        load_project_agent,
        project_modules,
    )

    started = time.monotonic()
    failures: list[str] = []
    transcript: list[str] = []
    with project_modules(root, environment):
        agent = load_project_agent()
        factory = DefaultLiveKitProviderFactory(environment)
        services = build_livekit_services(agent, factory=factory)
        policy = LiveKitPolicy.from_agent(agent)
        buffer = results.CallResultBuffer(call_id="call_test_livekit_audio")
        sink = MemorySink()
        tools = shared_livekit_tools(
            agent.tools,
            call_id=buffer.call_id,
            buffer=buffer,
            sink=sink,
        )
        native = await load_native_agent(agent.flow, shared_tools=list(tools))
        session: AgentSession[Any] = AgentSession(
            stt=services.stt,
            vad=services.vad,
            llm=services.llm,
            tts=services.tts,
            turn_handling=policy.turn_handling(detector_mode(services.turn_detection)),
            max_tool_steps=3,
        )
        microphone = QueueAudioInput()
        speaker = CapturingAudioOutput()
        session.input.audio = microphone
        session.output.audio = speaker

        def on_transcription(event: Any) -> None:
            if event.is_final and event.transcript:
                transcript.append(f"caller: {event.transcript}")

        session.on("user_input_transcribed", on_transcription)
        speech = EvalSpeech.from_config(
            {"service": "kokoro", "voice": "af_heart", "sample_rate": 16000}
        )
        transcriber = EvalTranscriber.from_config(
            {"service": "moonshine", "model": "small-streaming", "padding_secs": 0}
        )
        await speech.start()
        await transcriber.start()
        try:
            await session.start(native, record=False)
            with suppress(TimeoutError):
                opening_pcm, opening_rate = await speaker.next_segment(20)
                opening = await transcriber.transcribe(opening_pcm, opening_rate)
                if opening:
                    transcript.append(f"agent: {opening}")
            for turn in turns:
                if turn.user is None:
                    continue
                before_tools = len(sink.observations)
                caller_pcm, caller_rate = await speech.generate(turn.user)
                await microphone.speak(caller_pcm, sample_rate=caller_rate)
                timeout_s = (
                    turn.expect.within_ms / 1000
                    if turn.expect is not None and turn.expect.within_ms
                    else 60
                )
                try:
                    agent_pcm, agent_rate = await speaker.next_segment(timeout_s)
                except TimeoutError:
                    failures.append(f"no agent audio within {timeout_s:.1f}s")
                    continue
                agent_text = await transcriber.transcribe(agent_pcm, agent_rate)
                if agent_text:
                    transcript.append(f"agent: {agent_text}")
                expectation = turn.expect
                if expectation is None:
                    continue
                observed_tools = {item.tool_name for item in sink.observations[before_tools:]}
                for tool in expectation.tools:
                    if "livekit" in tool.runtimes and tool.name not in observed_tools:
                        failures.append(f"expected function call {tool.name!r}")
                if (
                    expectation.text_contains
                    and expectation.text_contains.casefold() not in agent_text.casefold()
                ):
                    failures.append(f"agent audio does not contain {expectation.text_contains!r}")
                if expectation.judge:
                    decision = await TranscriptJudge(
                        OpenAICompatibleClient(judge, environment=environment)
                    ).evaluate(
                        expectation.judge,
                        tuple(transcript),
                        seed=definition.seed,
                    )
                    if not decision.passed:
                        failures.append(f"judge: {decision.reason}")
        finally:
            await session.aclose()
            await microphone.aclose()
            await speech.aclose()
            await transcriber.aclose()
    duration = int((time.monotonic() - started) * 1000)
    failures.extend(hard_result_failures(definition, buffer.snapshot()))
    if duration > definition.max_duration_ms:
        failures.append(f"duration {duration}ms exceeds {definition.max_duration_ms}ms budget")
    return AttemptResult(
        passed=not failures,
        failures=tuple(failures),
        duration_ms=duration,
        turn_count=len(turns),
        transcript=tuple(transcript),
    )
