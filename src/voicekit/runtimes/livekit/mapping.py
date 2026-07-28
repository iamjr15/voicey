"""Auditable canonical-config mapping for LiveKit Agents 1.6.7."""

from __future__ import annotations

from dataclasses import dataclass
from typing import cast

from livekit.agents import TurnHandlingOptions
from livekit.agents.voice.turn import TurnDetectionMode

from voicekit.config.models import Agent, VoicemailBehavior


@dataclass(frozen=True, slots=True)
class ConfigMapping:
    field: str
    mechanism: str
    test: str


@dataclass(frozen=True, slots=True)
class LiveKitPolicy:
    """Every shared policy field copied without a silent runtime drop."""

    max_duration_s: int
    max_concurrent: int
    silence_hangup_s: int
    daily_spend_alert_usd: float | None
    allow_interruptions: bool
    voicemail: VoicemailBehavior
    dtmf: bool
    transfer_number: str | None
    end_call_phrases: tuple[str, ...]
    fallback_language: str | None
    record: bool
    prometheus_enabled: bool = False
    prometheus_bind: str = "127.0.0.1"
    prometheus_port: int = 9464
    prometheus_path: str = "/metrics"
    otlp_endpoint: str | None = None
    otlp_headers_env: str | None = None

    @classmethod
    def from_agent(cls, agent: Agent) -> LiveKitPolicy:
        return cls(
            max_duration_s=agent.limits.max_duration_s,
            max_concurrent=agent.limits.max_concurrent,
            silence_hangup_s=agent.limits.silence_hangup_s,
            daily_spend_alert_usd=agent.limits.daily_spend_alert_usd,
            allow_interruptions=agent.behavior.allow_interruptions,
            voicemail=agent.behavior.voicemail,
            dtmf=agent.behavior.dtmf,
            transfer_number=agent.behavior.transfer_number,
            end_call_phrases=tuple(agent.behavior.end_call_phrases),
            fallback_language=agent.voice.fallback_language,
            record=bool(agent.phone and agent.phone.record),
            prometheus_enabled=agent.observability.prometheus_enabled,
            prometheus_bind=agent.observability.prometheus_bind,
            prometheus_port=agent.observability.prometheus_port,
            prometheus_path=agent.observability.prometheus_path,
            otlp_endpoint=agent.observability.otlp_endpoint,
            otlp_headers_env=agent.observability.otlp_headers_env,
        )

    def turn_handling(self, detector: TurnDetectionMode) -> TurnHandlingOptions:
        """Use only the consolidated 1.6.7 turn-handling API."""
        return TurnHandlingOptions(
            turn_detection=detector,
            endpointing={
                "mode": "dynamic",
                "min_delay": 0.30,
                "max_delay": 3.0,
                "alpha": 0.5,
            },
            interruption={
                "enabled": self.allow_interruptions,
                "mode": "adaptive",
                "discard_audio_if_uninterruptible": True,
                "min_duration": 0.5,
                "min_words": 0,
                "resume_false_interruption": self.allow_interruptions,
                "false_interruption_timeout": 2.0,
                "backchannel_boundary": None,
            },
            preemptive_generation={
                "enabled": True,
                "preemptive_tts": False,
                "max_speech_duration": 30.0,
                "max_retries": 2,
            },
            user_turn_limit={"max_words": None, "max_duration": 120.0},
        )


LIVEKIT_CONFIG_MAPPINGS: tuple[ConfigMapping, ...] = (
    ConfigMapping(
        "models.fallbacks.stt",
        "livekit.agents.stt.FallbackAdapter([primary, fallback])",
        "test_config_field_mapping[livekit-models.fallbacks.stt]",
    ),
    ConfigMapping(
        "models.fallbacks.llm",
        "livekit.agents.llm.FallbackAdapter([primary, fallback])",
        "test_config_field_mapping[livekit-models.fallbacks.llm]",
    ),
    ConfigMapping(
        "models.fallbacks.tts",
        "livekit.agents.tts.FallbackAdapter([primary, fallback])",
        "test_config_field_mapping[livekit-models.fallbacks.tts]",
    ),
    ConfigMapping(
        "limits.max_duration_s",
        "call-local duration task invokes AgentSession.aclose()",
        "test_config_field_mapping[livekit-limits.max_duration_s]",
    ),
    ConfigMapping(
        "limits.max_concurrent",
        "AdmissionController reservation before dispatch token or SIP job session",
        "test_config_field_mapping[livekit-limits.max_concurrent]",
    ),
    ConfigMapping(
        "limits.silence_hangup_s",
        "AgentSession.user_away_timeout and native user_state_changed=away",
        "test_config_field_mapping[livekit-limits.silence_hangup_s]",
    ),
    ConfigMapping(
        "limits.daily_spend_alert_usd",
        "LiveKitPolicy threshold plus native session usage events",
        "test_config_field_mapping[livekit-limits.daily_spend_alert_usd]",
    ),
    ConfigMapping(
        "behavior.allow_interruptions",
        "TurnHandlingOptions.interruption.enabled plus mutating-tool interruption boundary",
        "test_config_field_mapping[livekit-behavior.allow_interruptions]",
    ),
    ConfigMapping(
        "behavior.voicemail",
        "SIP participant disposition maps to configured hangup or message workflow",
        "test_config_field_mapping[livekit-behavior.voicemail]",
    ),
    ConfigMapping(
        "behavior.dtmf",
        "room sip_dtmf_received listener and beta send_dtmf_events tool are capability-gated",
        "test_config_field_mapping[livekit-behavior.dtmf]",
    ),
    ConfigMapping(
        "behavior.transfer_number",
        "native cold transfer tool plus WarmTransferTask workflow",
        "test_config_field_mapping[livekit-behavior.transfer_number]",
    ),
    ConfigMapping(
        "behavior.end_call_phrases",
        "conversation_item_added assistant message invokes AgentSession.aclose()",
        "test_config_field_mapping[livekit-behavior.end_call_phrases]",
    ),
    ConfigMapping(
        "voice.fallback_language",
        "native provider update_options(language=...) on every compatible STT/TTS member",
        "test_config_field_mapping[livekit-voice.fallback_language]",
    ),
    ConfigMapping(
        "phone.record",
        "AgentSession.start(record=...) plus provider recording reconciliation",
        "test_config_field_mapping[livekit-phone.record]",
    ),
    ConfigMapping(
        "observability.prometheus_enabled",
        "parent AgentServer process-local TelemetryServer enabled explicitly",
        "test_config_field_mapping[livekit-observability.prometheus_enabled]",
    ),
    ConfigMapping(
        "observability.prometheus_bind",
        "uvicorn metrics listener host",
        "test_config_field_mapping[livekit-observability.prometheus_bind]",
    ),
    ConfigMapping(
        "observability.prometheus_port",
        "uvicorn metrics listener port",
        "test_config_field_mapping[livekit-observability.prometheus_port]",
    ),
    ConfigMapping(
        "observability.prometheus_path",
        "Prometheus ASGI exposition route",
        "test_config_field_mapping[livekit-observability.prometheus_path]",
    ),
    ConfigMapping(
        "observability.otlp_endpoint",
        "job-process fork-safe OTLPSpanExporter with BatchSpanProcessor",
        "test_config_field_mapping[livekit-observability.otlp_endpoint]",
    ),
    ConfigMapping(
        "observability.otlp_headers_env",
        "secret OTLP headers loaded from the named environment variable",
        "test_config_field_mapping[livekit-observability.otlp_headers_env]",
    ),
)


def detector_mode(value: object) -> TurnDetectionMode:
    """Keep the runtime cast isolated at the installed inference boundary."""
    return cast(TurnDetectionMode, value)
