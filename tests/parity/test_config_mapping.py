from __future__ import annotations

import json
from importlib.metadata import version
from pathlib import Path
from typing import Any, cast

import pytest
from livekit.agents.inference import TurnDetector

from voicekit import Agent, Behavior, Limits, Models, Observability, Phone, Results, Voice, Web
from voicekit.config.models import RuntimeName
from voicekit.runtimes.livekit.mapping import (
    LIVEKIT_CONFIG_MAPPINGS,
    LiveKitPolicy,
)
from voicekit.runtimes.pipecat.mapping import (
    PIPECAT_CONFIG_MAPPINGS,
    PipecatPolicy,
)

ROOT = Path(__file__).parents[2]
RUNTIMES: tuple[RuntimeName, ...] = ("pipecat", "livekit")
MAPPINGS = {
    "pipecat": PIPECAT_CONFIG_MAPPINGS,
    "livekit": LIVEKIT_CONFIG_MAPPINGS,
}
FIELDS = tuple(mapping.field for mapping in PIPECAT_CONFIG_MAPPINGS)


def _agent(runtime: RuntimeName) -> Agent:
    return Agent(
        name="parity-agent",
        runtime=runtime,
        models=Models(
            stt="deepgram/nova-3",
            llm="anthropic/claude-sonnet-5",
            tts="cartesia/sonic-3.5",
            fallbacks={
                "stt": "openai/gpt-4o-transcribe",
                "llm": "openai/gpt-5",
                "tts": "elevenlabs/flash-2.5",
            },
        ),
        voice=Voice(language="en", fallback_language="es", speed=1.1),
        persona="Exercise every canonical runtime field.",
        flow="flow:entry",
        tools="tools",
        phone=Phone(
            provider="twilio",
            number="+14155550123",
            record=True,
        ),
        web=Web(enabled=True, allowed_origins=["https://app.example.test"]),
        results=Results(
            webhook="https://receiver.example.test/results",
            secret_env="VOICEKIT_WEBHOOK_SECRET",  # pragma: allowlist secret
        ),
        limits=Limits(
            max_duration_s=90,
            max_concurrent=7,
            silence_hangup_s=12,
            daily_spend_alert_usd=4.25,
        ),
        observability=Observability(
            prometheus_enabled=True,
            prometheus_bind="127.0.0.2",
            prometheus_port=9465,
            prometheus_path="/internal/metrics",
            otlp_endpoint="https://collector.example.test/v1/traces",
            otlp_headers_env="VOICEKIT_OTLP_HEADERS",
        ),
        behavior=Behavior(
            allow_interruptions=False,
            voicemail="leave_message",
            dtmf=False,
            transfer_number="+14155550124",
            end_call_phrases=["finish now"],
        ),
    )


@pytest.mark.parametrize("runtime", RUNTIMES)
@pytest.mark.parametrize("field", FIELDS)
def test_config_field_mapping(runtime: RuntimeName, field: str) -> None:
    """Each matrix cell copies its canonical value and names a native mechanism."""
    agent = _agent(runtime)
    mapping = {row.field: row for row in MAPPINGS[runtime]}[field]
    policy = (
        PipecatPolicy.from_agent(agent) if runtime == "pipecat" else LiveKitPolicy.from_agent(agent)
    )
    assert mapping.mechanism
    assert "pending" not in mapping.mechanism.casefold()
    assert mapping.test == f"test_config_field_mapping[{runtime}-{field}]"

    if field.startswith("models.fallbacks."):
        axis = field.rsplit(".", maxsplit=1)[-1]
        assert agent.models.fallbacks[cast(Any, axis)]
        assert "fallback" in mapping.mechanism.casefold() or "switcher" in (
            mapping.mechanism.casefold()
        )
        return

    attribute = {
        "limits.max_duration_s": "max_duration_s",
        "limits.max_concurrent": "max_concurrent",
        "limits.silence_hangup_s": "silence_hangup_s",
        "limits.daily_spend_alert_usd": "daily_spend_alert_usd",
        "behavior.allow_interruptions": "allow_interruptions",
        "behavior.voicemail": "voicemail",
        "behavior.dtmf": "dtmf",
        "behavior.transfer_number": "transfer_number",
        "behavior.end_call_phrases": "end_call_phrases",
        "voice.fallback_language": "fallback_language",
        "phone.record": "record",
        "observability.prometheus_enabled": "prometheus_enabled",
        "observability.prometheus_bind": "prometheus_bind",
        "observability.prometheus_port": "prometheus_port",
        "observability.prometheus_path": "prometheus_path",
        "observability.otlp_endpoint": "otlp_endpoint",
        "observability.otlp_headers_env": "otlp_headers_env",
    }[field]
    expected: object = {
        "limits.max_duration_s": 90,
        "limits.max_concurrent": 7,
        "limits.silence_hangup_s": 12,
        "limits.daily_spend_alert_usd": 4.25,
        "behavior.allow_interruptions": False,
        "behavior.voicemail": "leave_message",
        "behavior.dtmf": False,
        "behavior.transfer_number": "+14155550124",
        "behavior.end_call_phrases": ("finish now",),
        "voice.fallback_language": "es",
        "phone.record": True,
        "observability.prometheus_enabled": True,
        "observability.prometheus_bind": "127.0.0.2",
        "observability.prometheus_port": 9465,
        "observability.prometheus_path": "/internal/metrics",
        "observability.otlp_endpoint": "https://collector.example.test/v1/traces",
        "observability.otlp_headers_env": "VOICEKIT_OTLP_HEADERS",
    }[field]
    assert getattr(policy, attribute) == expected

    if runtime == "livekit" and field == "behavior.allow_interruptions":
        handling = cast(
            "dict[str, Any]",
            cast("LiveKitPolicy", policy).turn_handling(cast(Any, TurnDetector(version="v1-mini"))),
        )
        assert handling["interruption"]["enabled"] is False


def test_config_matrix_is_complete_versioned_and_matches_runtime_sources() -> None:
    document = json.loads((ROOT / "docs" / "runtime-config-matrix.json").read_text())
    rows = document["rows"]

    assert document["schema_version"] == 3
    assert document["pipecat_version"] == version("pipecat-ai")
    assert document["livekit_version"] == version("livekit-agents")
    assert [row["field"] for row in rows] == list(FIELDS)
    assert tuple(mapping.field for mapping in LIVEKIT_CONFIG_MAPPINGS) == FIELDS
    for runtime in RUNTIMES:
        source = MAPPINGS[runtime]
        assert [row[runtime] for row in rows] == [mapping.mechanism for mapping in source]
        assert [row[f"{runtime}_test"] for row in rows] == [mapping.test for mapping in source]
        assert all(row[runtime] and row[f"{runtime}_test"] for row in rows)
