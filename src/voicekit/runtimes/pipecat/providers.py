"""Provider construction and failover on the installed Pipecat 1.6 APIs."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol, TypeAlias, cast

from pipecat.pipeline.llm_switcher import LLMSwitcher
from pipecat.pipeline.service_switcher import (
    ServiceSwitcher,
    ServiceSwitcherStrategyFailover,
)
from pipecat.processors.frame_processor import FrameProcessor
from pipecat.services.anthropic.llm import AnthropicLLMService
from pipecat.services.cartesia.tts import CartesiaTTSService, GenerationConfig
from pipecat.services.deepgram.stt import DeepgramSTTService
from pipecat.services.elevenlabs.tts import ElevenLabsTTSService
from pipecat.services.google.llm import GoogleLLMService
from pipecat.services.llm_service import LLMService
from pipecat.services.openai.llm import OpenAILLMService
from pipecat.services.openai.stt import OpenAISTTService
from pipecat.services.openai.tts import OpenAITTSService
from pipecat.services.settings import ServiceSettings

from voicekit.config.models import Agent, ModelAxis, Voice
from voicekit.errors import VoicekitError

_PROVIDER_MODEL_NAMES: dict[str, str] = {
    "deepgram/nova-3": "nova-3-general",
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

LLMProcessor: TypeAlias = LLMService[Any] | LLMSwitcher[Any]


class ProviderFactory(Protocol):
    """Injectable service construction used by runtime tests and extensions."""

    def create_stt(self, model_id: str, voice: Voice, sample_rate: int) -> FrameProcessor: ...

    def create_llm(self, model_id: str, persona: str) -> LLMService[Any]: ...

    def create_tts(self, model_id: str, voice: Voice, sample_rate: int) -> FrameProcessor: ...

    def language_delta(self, service: FrameProcessor, language: str) -> ServiceSettings: ...


@dataclass(frozen=True, slots=True)
class PipecatServices:
    """Pipeline processors plus raw members needed for typed runtime updates."""

    stt: FrameProcessor
    llm: LLMProcessor
    tts: FrameProcessor
    stt_members: tuple[FrameProcessor, ...]
    llm_members: tuple[LLMService[Any], ...]
    tts_members: tuple[FrameProcessor, ...]


class DefaultProviderFactory:
    """Create every model in the checked-in provider catalog using Settings."""

    def __init__(self, environment: Mapping[str, str] | None = None) -> None:
        self._environment = environment if environment is not None else os.environ

    def create_stt(self, model_id: str, voice: Voice, sample_rate: int) -> FrameProcessor:
        model = _model_name(model_id)
        api_key = self._credential(model_id)
        if model_id == "deepgram/nova-3":
            return DeepgramSTTService(
                api_key=api_key,
                sample_rate=sample_rate,
                settings=DeepgramSTTService.Settings(
                    model=model,
                    language=voice.language,
                    interim_results=True,
                    punctuate=True,
                    smart_format=True,
                ),
            )
        if model_id == "openai/gpt-4o-transcribe":
            return OpenAISTTService(
                api_key=api_key,
                settings=OpenAISTTService.Settings(
                    model=model,
                    language=voice.language,
                ),
            )
        raise VoicekitError(
            "VK-RUN-002",
            detail=f"{model_id!r} is not a supported Pipecat STT model.",
        )

    def create_llm(self, model_id: str, persona: str) -> LLMService[Any]:
        model = _model_name(model_id)
        api_key = self._credential(model_id)
        if model_id == "anthropic/claude-sonnet-5":
            return cast(
                "LLMService[Any]",
                AnthropicLLMService(
                    api_key=api_key,
                    settings=AnthropicLLMService.Settings(
                        model=model,
                        system_instruction=persona,
                    ),
                ),
            )
        if model_id == "openai/gpt-5":
            return cast(
                "LLMService[Any]",
                OpenAILLMService(
                    api_key=api_key,
                    settings=OpenAILLMService.Settings(
                        model=model,
                        system_instruction=persona,
                    ),
                ),
            )
        if model_id == "google/gemini-2.5-flash":
            return cast(
                "LLMService[Any]",
                GoogleLLMService(
                    api_key=api_key,
                    settings=GoogleLLMService.Settings(
                        model=model,
                        system_instruction=persona,
                    ),
                ),
            )
        raise VoicekitError(
            "VK-RUN-002",
            detail=f"{model_id!r} is not a supported Pipecat LLM model.",
        )

    def create_tts(self, model_id: str, voice: Voice, sample_rate: int) -> FrameProcessor:
        model = _model_name(model_id)
        api_key = self._credential(model_id)
        if model_id == "cartesia/sonic-3.5":
            settings = CartesiaTTSService.Settings(
                model=model,
                language=voice.language,
                generation_config=GenerationConfig(speed=voice.speed),
            )
            if voice.id is not None:
                settings = CartesiaTTSService.Settings(
                    model=model,
                    language=voice.language,
                    voice=voice.id,
                    generation_config=GenerationConfig(speed=voice.speed),
                )
            return CartesiaTTSService(
                api_key=api_key,
                sample_rate=sample_rate,
                settings=settings,
            )
        if model_id == "elevenlabs/flash-2.5":
            settings = ElevenLabsTTSService.Settings(
                model=model,
                language=voice.language,
                speed=voice.speed,
            )
            if voice.id is not None:
                settings = ElevenLabsTTSService.Settings(
                    model=model,
                    language=voice.language,
                    voice=voice.id,
                    speed=voice.speed,
                )
            return ElevenLabsTTSService(
                api_key=api_key,
                sample_rate=sample_rate,
                settings=settings,
            )
        if model_id == "openai/gpt-4o-mini-tts":
            settings = OpenAITTSService.Settings(
                model=model,
                language=voice.language,
                speed=voice.speed,
            )
            if voice.id is not None:
                settings = OpenAITTSService.Settings(
                    model=model,
                    language=voice.language,
                    voice=voice.id,
                    speed=voice.speed,
                )
            return OpenAITTSService(
                api_key=api_key,
                settings=settings,
            )
        raise VoicekitError(
            "VK-RUN-002",
            detail=f"{model_id!r} is not a supported Pipecat TTS model.",
        )

    def language_delta(self, service: FrameProcessor, language: str) -> ServiceSettings:
        settings_type = getattr(type(service), "Settings", None)
        if settings_type is None:
            raise VoicekitError(
                "VK-RUN-002",
                detail=f"{type(service).__name__} has no current Settings surface.",
            )
        try:
            return cast(ServiceSettings, settings_type(language=language))
        except (TypeError, ValueError) as exc:
            raise VoicekitError(
                "VK-RUN-002",
                detail=f"{type(service).__name__} cannot switch to {language!r}.",
            ) from exc

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


def build_services(
    agent: Agent,
    *,
    sample_rate: int,
    factory: ProviderFactory,
) -> PipecatServices:
    """Build primary and configured backup services with failover processors."""
    stt_members = _members(agent, "stt", factory, sample_rate)
    llm_members = _llm_members(agent, factory)
    tts_members = _members(agent, "tts", factory, sample_rate)

    stt: FrameProcessor = stt_members[0]
    if len(stt_members) > 1:
        stt = ServiceSwitcher(
            services=list(stt_members),
            strategy_type=ServiceSwitcherStrategyFailover,
        )
    llm: LLMProcessor = llm_members[0]
    if len(llm_members) > 1:
        llm = LLMSwitcher(
            llms=list(llm_members),
            strategy_type=ServiceSwitcherStrategyFailover,
        )
    tts: FrameProcessor = tts_members[0]
    if len(tts_members) > 1:
        tts = ServiceSwitcher(
            services=list(tts_members),
            strategy_type=ServiceSwitcherStrategyFailover,
        )
    return PipecatServices(
        stt=stt,
        llm=llm,
        tts=tts,
        stt_members=stt_members,
        llm_members=llm_members,
        tts_members=tts_members,
    )


def _members(
    agent: Agent,
    axis: ModelAxis,
    factory: ProviderFactory,
    sample_rate: int,
) -> tuple[FrameProcessor, ...]:
    identifiers = [getattr(agent.models, axis)]
    fallback = agent.models.fallbacks.get(axis)
    if fallback is not None:
        identifiers.append(fallback)
    if axis == "stt":
        return tuple(
            factory.create_stt(identifier, agent.voice, sample_rate) for identifier in identifiers
        )
    if axis == "tts":
        return tuple(
            factory.create_tts(identifier, agent.voice, sample_rate) for identifier in identifiers
        )
    raise AssertionError(f"unsupported processor axis {axis}")


def _llm_members(
    agent: Agent,
    factory: ProviderFactory,
) -> tuple[LLMService[Any], ...]:
    identifiers = [agent.models.llm]
    fallback = agent.models.fallbacks.get("llm")
    if fallback is not None:
        identifiers.append(fallback)
    return tuple(factory.create_llm(identifier, agent.persona) for identifier in identifiers)


def _model_name(model_id: str) -> str:
    try:
        return _PROVIDER_MODEL_NAMES[model_id]
    except KeyError as exc:
        raise VoicekitError(
            "VK-RUN-002",
            detail=f"{model_id!r} has no Pipecat provider mapping.",
        ) from exc
