"""Provider construction on the installed LiveKit Agents 1.6.7 plugins."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from livekit.agents import llm, stt, tts, vad
from livekit.agents.inference import TurnDetector
from livekit.plugins import anthropic, cartesia, deepgram, elevenlabs, google, openai, silero

from voicekit.config.models import Agent, Voice
from voicekit.errors import VoicekitError

_MODEL_NAMES: dict[str, str] = {
    "deepgram/nova-3": "nova-3",
    "openai/gpt-4o-transcribe": "gpt-4o-transcribe",
    "anthropic/claude-sonnet-5": "claude-sonnet-5",
    "openai/gpt-5": "gpt-5",
    "google/gemini-2.5-flash": "gemini-2.5-flash",
    "cartesia/sonic-3.5": "sonic-3.5",
    "elevenlabs/flash-2.5": "eleven_flash_v2_5",
    "openai/gpt-4o-mini-tts": "gpt-4o-mini-tts",
}
_PROVIDER_KEYS: dict[str, str] = {
    "deepgram": "DEEPGRAM_API_KEY",
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "google": "GEMINI_API_KEY",
    "cartesia": "CARTESIA_API_KEY",
    "elevenlabs": "ELEVENLABS_API_KEY",
}


class LiveKitProviderFactory(Protocol):
    """Injectable provider construction for runtime and matrix tests."""

    def create_stt(self, model_id: str, voice: Voice) -> stt.STT[Any]: ...

    def create_llm(self, model_id: str) -> llm.LLM[Any]: ...

    def create_tts(self, model_id: str, voice: Voice) -> tts.TTS[Any]: ...

    def create_vad(self) -> vad.VAD: ...

    def create_turn_detector(self) -> TurnDetector: ...


@dataclass(frozen=True, slots=True)
class LiveKitServices:
    """Native primary/fallback services and local turn handling models."""

    stt: stt.STT[Any]
    llm: llm.LLM[Any]
    tts: tts.TTS[Any]
    vad: vad.VAD
    turn_detection: TurnDetector
    stt_members: tuple[stt.STT[Any], ...]
    llm_members: tuple[llm.LLM[Any], ...]
    tts_members: tuple[tts.TTS[Any], ...]


class DefaultLiveKitProviderFactory:
    """Map every shared-catalog model to its verified 1.6.7 plugin."""

    def __init__(
        self,
        environment: Mapping[str, str] | None = None,
        *,
        vad_model: vad.VAD | None = None,
    ) -> None:
        self._environment = environment if environment is not None else os.environ
        self._vad_model = vad_model

    def create_stt(self, model_id: str, voice: Voice) -> stt.STT[Any]:
        model = _model_name(model_id)
        api_key = self._credential(model_id)
        if model_id == "deepgram/nova-3":
            return deepgram.STT(
                model=model,
                language=voice.language,
                api_key=api_key,
                interim_results=True,
                punctuate=True,
                smart_format=True,
            )
        if model_id == "openai/gpt-4o-transcribe":
            return openai.STT(
                model=model,
                language=voice.language,
                api_key=api_key,
            )
        raise VoicekitError(
            "VK-RUN-002",
            detail=f"{model_id!r} is not a supported LiveKit STT model.",
        )

    def create_llm(self, model_id: str) -> llm.LLM[Any]:
        model = _model_name(model_id)
        api_key = self._credential(model_id)
        if model_id == "anthropic/claude-sonnet-5":
            return anthropic.LLM(model=model, api_key=api_key)
        if model_id == "openai/gpt-5":
            return openai.LLM(model=model, api_key=api_key)
        if model_id == "google/gemini-2.5-flash":
            return google.LLM(model=model, api_key=api_key)
        raise VoicekitError(
            "VK-RUN-002",
            detail=f"{model_id!r} is not a supported LiveKit LLM model.",
        )

    def create_tts(self, model_id: str, voice: Voice) -> tts.TTS[Any]:
        model = _model_name(model_id)
        api_key = self._credential(model_id)
        if model_id == "cartesia/sonic-3.5":
            if voice.id is None:
                return cartesia.TTS(
                    model=model,
                    language=voice.language,
                    speed=voice.speed,
                    api_key=api_key,
                )
            return cartesia.TTS(
                model=model,
                language=voice.language,
                speed=voice.speed,
                api_key=api_key,
                voice=voice.id,
            )
        if model_id == "elevenlabs/flash-2.5":
            settings = elevenlabs.VoiceSettings(
                stability=0.5,
                similarity_boost=0.75,
                speed=voice.speed,
            )
            if voice.id is None:
                return elevenlabs.TTS(
                    model=model,
                    language=voice.language,
                    api_key=api_key,
                    voice_settings=settings,
                )
            return elevenlabs.TTS(
                model=model,
                language=voice.language,
                api_key=api_key,
                voice_settings=settings,
                voice_id=voice.id,
            )
        if model_id == "openai/gpt-4o-mini-tts":
            if voice.id is None:
                return openai.TTS(
                    model=model,
                    speed=voice.speed,
                    api_key=api_key,
                )
            return openai.TTS(
                model=model,
                speed=voice.speed,
                api_key=api_key,
                voice=voice.id,
            )
        raise VoicekitError(
            "VK-RUN-002",
            detail=f"{model_id!r} is not a supported LiveKit TTS model.",
        )

    def create_vad(self) -> vad.VAD:
        return self._vad_model or silero.VAD.load()

    def create_turn_detector(self) -> TurnDetector:
        return TurnDetector(version="v1-mini")

    def _credential(self, model_id: str) -> str:
        provider = model_id.split("/", maxsplit=1)[0]
        env_name = _PROVIDER_KEYS[provider]
        value = self._environment.get(env_name, "")
        if not value:
            raise VoicekitError(
                "VK-RUN-002",
                detail=f"{env_name} is required for {model_id}.",
            )
        return value


def build_livekit_services(
    agent: Agent,
    *,
    factory: LiveKitProviderFactory,
) -> LiveKitServices:
    """Build primary members and native failover adapters for every axis."""
    stt_members = _stt_members(agent, factory)
    llm_members = _llm_members(agent, factory)
    tts_members = _tts_members(agent, factory)
    vad_service = factory.create_vad()
    stt_service = (
        stt_members[0]
        if len(stt_members) == 1
        else stt.FallbackAdapter(list(stt_members), vad=vad_service)
    )
    llm_service = (
        llm_members[0] if len(llm_members) == 1 else llm.FallbackAdapter(list(llm_members))
    )
    tts_service = (
        tts_members[0] if len(tts_members) == 1 else tts.FallbackAdapter(list(tts_members))
    )
    return LiveKitServices(
        stt=stt_service,
        llm=llm_service,
        tts=tts_service,
        vad=vad_service,
        turn_detection=factory.create_turn_detector(),
        stt_members=stt_members,
        llm_members=llm_members,
        tts_members=tts_members,
    )


def _stt_members(
    agent: Agent,
    factory: LiveKitProviderFactory,
) -> tuple[stt.STT[Any], ...]:
    identifiers = [agent.models.stt]
    fallback = agent.models.fallbacks.get("stt")
    if fallback is not None:
        identifiers.append(fallback)
    return tuple(factory.create_stt(identifier, agent.voice) for identifier in identifiers)


def _tts_members(
    agent: Agent,
    factory: LiveKitProviderFactory,
) -> tuple[tts.TTS[Any], ...]:
    identifiers = [agent.models.tts]
    fallback = agent.models.fallbacks.get("tts")
    if fallback is not None:
        identifiers.append(fallback)
    return tuple(factory.create_tts(identifier, agent.voice) for identifier in identifiers)


def _llm_members(
    agent: Agent,
    factory: LiveKitProviderFactory,
) -> tuple[llm.LLM[Any], ...]:
    identifiers = [agent.models.llm]
    fallback = agent.models.fallbacks.get("llm")
    if fallback is not None:
        identifiers.append(fallback)
    return tuple(factory.create_llm(identifier) for identifier in identifiers)


def _model_name(model_id: str) -> str:
    try:
        return _MODEL_NAMES[model_id]
    except KeyError as exc:
        raise VoicekitError(
            "VK-RUN-002",
            detail=f"{model_id!r} has no LiveKit provider mapping.",
        ) from exc
