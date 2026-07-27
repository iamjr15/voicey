from collections.abc import Callable
from typing import Any, cast

import pytest
from pipecat.processors.frame_processor import FrameProcessor
from pipecat.services.anthropic.llm import AnthropicLLMService
from pipecat.services.cartesia.tts import CartesiaTTSService
from pipecat.services.deepgram.stt import DeepgramSTTService
from pipecat.services.elevenlabs.tts import ElevenLabsTTSService
from pipecat.services.google.llm import GoogleLLMService
from pipecat.services.openai.llm import OpenAILLMService
from pipecat.services.openai.stt import OpenAISTTService
from pipecat.services.openai.tts import OpenAITTSService

from voicekit import Voice
from voicekit.errors import VoicekitError
from voicekit.runtimes.pipecat.providers import DefaultProviderFactory

_ENVIRONMENT = {
    "DEEPGRAM_API_KEY": "deepgram-test",
    "OPENAI_API_KEY": "openai-test",
    "ANTHROPIC_API_KEY": "anthropic-test",
    "GEMINI_API_KEY": "gemini-test",
    "CARTESIA_API_KEY": "cartesia-test",
    "ELEVENLABS_API_KEY": "elevenlabs-test",
}


@pytest.mark.parametrize(
    ("model_id", "expected_type", "provider_model"),
    [
        ("deepgram/nova-3", DeepgramSTTService, "nova-3-general"),
        ("openai/gpt-4o-transcribe", OpenAISTTService, "gpt-4o-transcribe"),
    ],
)
def test_stt_catalog_builds_current_settings(
    model_id: str,
    expected_type: type[FrameProcessor],
    provider_model: str,
) -> None:
    service = DefaultProviderFactory(_ENVIRONMENT).create_stt(
        model_id,
        Voice(language="en"),
        16000,
    )

    assert isinstance(service, expected_type)
    assert cast(Any, service)._settings.model == provider_model
    assert str(cast(Any, service)._settings.language).casefold() in {"en", "language.en"}


@pytest.mark.parametrize(
    ("model_id", "expected_type", "provider_model"),
    [
        ("anthropic/claude-sonnet-5", AnthropicLLMService, "claude-sonnet-5"),
        ("openai/gpt-5", OpenAILLMService, "gpt-5"),
        ("google/gemini-2.5-flash", GoogleLLMService, "gemini-2.5-flash"),
    ],
)
def test_llm_catalog_builds_current_settings(
    model_id: str,
    expected_type: type[FrameProcessor],
    provider_model: str,
) -> None:
    service = DefaultProviderFactory(_ENVIRONMENT).create_llm(model_id, "Test persona.")

    assert isinstance(service, expected_type)
    assert cast(Any, service)._settings.model == provider_model
    assert cast(Any, service)._settings.system_instruction == "Test persona."


@pytest.mark.parametrize(
    ("model_id", "expected_type", "provider_model"),
    [
        ("cartesia/sonic-3.5", CartesiaTTSService, "sonic-3.5"),
        ("elevenlabs/flash-2.5", ElevenLabsTTSService, "eleven_flash_v2_5"),
        ("openai/gpt-4o-mini-tts", OpenAITTSService, "gpt-4o-mini-tts"),
    ],
)
@pytest.mark.parametrize("voice_id", [None, "voice-test"])
def test_tts_catalog_builds_current_settings(
    model_id: str,
    expected_type: type[FrameProcessor],
    provider_model: str,
    voice_id: str | None,
) -> None:
    service = DefaultProviderFactory(_ENVIRONMENT).create_tts(
        model_id,
        Voice(language="en", id=voice_id, speed=1.1),
        16000,
    )

    assert isinstance(service, expected_type)
    assert cast(Any, service)._settings.model == provider_model
    if voice_id is not None:
        assert cast(Any, service)._settings.voice == voice_id


@pytest.mark.parametrize(
    ("method", "model_id"),
    [
        ("create_stt", "deepgram/nova-3"),
        ("create_llm", "anthropic/claude-sonnet-5"),
        ("create_tts", "cartesia/sonic-3.5"),
    ],
)
def test_missing_provider_credentials_are_cataloged(
    method: str,
    model_id: str,
) -> None:
    factory = DefaultProviderFactory({})
    calls: dict[str, Callable[[], object]] = {
        "create_llm": lambda: factory.create_llm(model_id, "Persona."),
        "create_stt": lambda: factory.create_stt(model_id, Voice(), 16000),
        "create_tts": lambda: factory.create_tts(model_id, Voice(), 16000),
    }

    with pytest.raises(VoicekitError, match="VK-RUN-002"):
        calls[method]()


def test_unsupported_provider_model_and_settings_are_cataloged() -> None:
    factory = DefaultProviderFactory(_ENVIRONMENT)

    with pytest.raises(VoicekitError, match="VK-RUN-002"):
        factory.create_stt("unknown/model", Voice(), 16000)
    with pytest.raises(VoicekitError, match="VK-RUN-002"):
        factory.language_delta(cast(FrameProcessor, object()), "es")
