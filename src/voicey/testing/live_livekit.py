"""Native LiveKit AgentSession caller over a real outbound SIP participant."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Mapping
from contextlib import suppress
from typing import Any, cast

from google.protobuf.duration_pb2 import Duration
from livekit import api, rtc
from livekit.agents import Agent as NativeAgent
from livekit.agents import AgentSession, ConversationItemAddedEvent, ErrorEvent
from livekit.agents.llm import ChatMessage
from livekit.agents.voice.room_io import RoomOptions

from voicey.config.models import Voice
from voicey.errors import VoiceyError
from voicey.runtimes.livekit.mapping import LiveKitPolicy, detector_mode
from voicey.runtimes.livekit.providers import (
    DefaultLiveKitProviderFactory,
    LiveKitProviderFactory,
)
from voicey.testing.live import (
    LiveCallEvidence,
    LiveCallPlan,
    LiveEnvironment,
)
from voicey.testing.models import LiveTestingConfig


class LiveKitSipPstnBackend:
    """Run the simulated caller as a native room agent and dial the target DID."""

    def __init__(
        self,
        *,
        config: LiveTestingConfig,
        live: LiveEnvironment,
        environment: Mapping[str, str],
        api_client: Any | None = None,
        room_factory: Callable[[], Any] = rtc.Room,
        session_factory: Callable[..., Any] = AgentSession,
        agent_factory: Callable[..., Any] = NativeAgent,
        provider_factory: LiveKitProviderFactory | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if live.runtime != "livekit" or live.livekit_outbound_trunk_id is None:
            raise VoiceyError("VY-TST-003", detail="invalid LiveKit live-test configuration.")
        self._config = config
        self._live = live
        self._environment = dict(environment)
        self._api = api_client
        self._owns_api = api_client is None
        self._room_factory = room_factory
        self._session_factory = session_factory
        self._agent_factory = agent_factory
        self._providers = provider_factory or DefaultLiveKitProviderFactory(self._environment)
        self._sleep = sleep
        self._closed = False

    async def run_call(self, plan: LiveCallPlan) -> LiveCallEvidence:
        if self._closed:
            raise VoiceyError("VY-TST-003", detail="LiveKit live caller is already closed.")
        lkapi = self._api_client()
        room_name = f"voicey-live-{plan.run_id}"[:128]
        participant_identity = f"voicey-target-{plan.run_id}"[:128]
        room = self._room_factory()
        session: Any | None = None
        room_created = False
        connected = False
        transcript: list[str] = []
        finished = asyncio.Event()
        heard_target = asyncio.Event()
        terminal = {"status": "completed"}
        started = time.monotonic()

        try:
            await lkapi.room.create_room(
                api.CreateRoomRequest(
                    name=room_name,
                    empty_timeout=max(60, plan.max_duration_s + 30),
                    max_participants=3,
                )
            )
            room_created = True
            token = (
                api.AccessToken(
                    self._environment["LIVEKIT_API_KEY"],
                    self._environment["LIVEKIT_API_SECRET"],
                )
                .with_identity(f"voicey-sim-{plan.run_id}"[:128])
                .with_name("voicey PSTN test caller")
                .with_grants(api.VideoGrants(room_join=True, room=room_name))
                .to_jwt()
            )
            await room.connect(
                self._environment["LIVEKIT_URL"],
                token,
                options=rtc.RoomOptions(auto_subscribe=True),
            )
            connected = True

            voice = Voice(language="en")
            stt = self._providers.create_stt("deepgram/nova-3", voice)
            llm = self._providers.create_llm("anthropic/claude-sonnet-5")
            tts = self._providers.create_tts("cartesia/sonic-3.5", voice)
            vad = self._providers.create_vad()
            turn_detector = self._providers.create_turn_detector()
            policy = _caller_policy(plan)
            native_session = self._session_factory(
                stt=stt,
                vad=vad,
                llm=llm,
                tts=tts,
                turn_handling=policy.turn_handling(detector_mode(turn_detector)),
                max_tool_steps=1,
                user_away_timeout=min(30.0, float(plan.max_duration_s)),
            )
            session = native_session

            def conversation_item_added(event: ConversationItemAddedEvent) -> None:
                item = event.item
                if not isinstance(item, ChatMessage) or not item.text_content:
                    return
                if item.role == "user":
                    transcript.append(f"agent: {item.text_content}")
                    heard_target.set()
                elif item.role == "assistant":
                    transcript.append(f"caller: {item.text_content}")
                    if "thank you, goodbye" in item.text_content.casefold():
                        finished.set()

            def session_error(_event: ErrorEvent) -> None:
                terminal["status"] = "session-error"
                finished.set()

            def participant_disconnected(participant: Any) -> None:
                if getattr(participant, "identity", None) == participant_identity:
                    finished.set()

            native_session.on("conversation_item_added", conversation_item_added)
            native_session.on("error", session_error)
            room.on("participant_disconnected", participant_disconnected)
            native_agent = self._agent_factory(instructions=plan.prompt)
            await native_session.start(
                native_agent,
                room=room,
                room_options=RoomOptions(
                    text_input=False,
                    audio_input=True,
                    video_input=False,
                    audio_output=True,
                    text_output=False,
                    participant_identity=participant_identity,
                    close_on_disconnect=False,
                    delete_room_on_close=False,
                ),
                record=False,
            )
            sip_info = await lkapi.sip.create_sip_participant(
                api.CreateSIPParticipantRequest(
                    sip_trunk_id=cast(str, self._live.livekit_outbound_trunk_id),
                    sip_call_to=self._live.target_number,
                    room_name=room_name,
                    participant_identity=participant_identity,
                    participant_name="voicey target agent",
                    wait_until_answered=True,
                    ringing_timeout=Duration(seconds=self._config.answer_timeout_s),
                    max_call_duration=Duration(seconds=plan.max_duration_s),
                    hide_phone_number=True,
                ),
                timeout=float(self._config.answer_timeout_s + 5),
            )
            try:
                await asyncio.wait_for(heard_target.wait(), timeout=3)
            except TimeoutError:
                opening = native_session.generate_reply(
                    instructions=(
                        "Begin the phone call now with the first short caller utterance "
                        "needed by your instructions."
                    )
                )
                await opening.wait_for_playout()
            try:
                await asyncio.wait_for(finished.wait(), timeout=float(plan.max_duration_s))
            except TimeoutError:
                terminal["status"] = "timeout"
            if finished.is_set() and terminal["status"] == "completed":
                await self._sleep(1)
            duration_ms = int((time.monotonic() - started) * 1000)
            return LiveCallEvidence(
                transcript=tuple(transcript),
                duration_ms=duration_ms,
                terminal_status=terminal["status"],
                provider="livekit-sip",
                path="livekit-native-agent-sip-pstn",
                provider_call_id=str(getattr(sip_info, "sip_call_id", "")),
                runtime_call_id=room_name,
            )
        except VoiceyError:
            raise
        except Exception as exc:
            raise VoiceyError(
                "VY-TST-003",
                detail=(
                    "LiveKit could not establish or execute the acknowledged PSTN call; "
                    f"provider error type {type(exc).__name__}."
                ),
            ) from exc
        finally:
            if session is not None:
                with suppress(Exception):
                    session.shutdown(drain=True)
                with suppress(Exception):
                    await session.aclose()
            if connected:
                with suppress(Exception):
                    await room.disconnect()
            if room_created:
                with suppress(Exception):
                    await lkapi.room.delete_room(api.DeleteRoomRequest(room=room_name))

    async def aclose(self) -> None:
        if self._closed:
            return
        if self._api is not None and self._owns_api:
            await self._api.aclose()
        self._closed = True

    def _api_client(self) -> Any:
        if self._api is None:
            self._api = api.LiveKitAPI(
                self._environment["LIVEKIT_URL"],
                self._environment["LIVEKIT_API_KEY"],
                self._environment["LIVEKIT_API_SECRET"],
            )
        return self._api


def _caller_policy(plan: LiveCallPlan) -> LiveKitPolicy:
    return LiveKitPolicy(
        max_duration_s=plan.max_duration_s,
        max_concurrent=1,
        silence_hangup_s=min(30, max(5, plan.max_duration_s - 1)),
        daily_spend_alert_usd=None,
        allow_interruptions=True,
        voicemail="hangup",
        dtmf=False,
        transfer_number=None,
        end_call_phrases=("thank you, goodbye",),
        fallback_language=None,
        record=False,
    )
