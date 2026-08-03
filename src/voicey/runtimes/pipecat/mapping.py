"""Auditable canonical-config mapping for the Pipecat runtime."""

from __future__ import annotations

from dataclasses import dataclass

from voicey.config.models import Agent, VoicemailBehavior


@dataclass(frozen=True, slots=True)
class ConfigMapping:
    field: str
    mechanism: str
    test: str


@dataclass(frozen=True, slots=True)
class PipecatPolicy:
    """Every cross-runtime policy value copied without silent drops."""

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
    def from_agent(cls, agent: Agent) -> PipecatPolicy:
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


PIPECAT_CONFIG_MAPPINGS: tuple[ConfigMapping, ...] = (
    ConfigMapping(
        "models.fallbacks.stt",
        "ServiceSwitcher(services=[primary, backup], ServiceSwitcherStrategyFailover)",
        "test_config_field_mapping[pipecat-models.fallbacks.stt]",
    ),
    ConfigMapping(
        "models.fallbacks.llm",
        "LLMSwitcher(llms=[primary, backup], ServiceSwitcherStrategyFailover)",
        "test_config_field_mapping[pipecat-models.fallbacks.llm]",
    ),
    ConfigMapping(
        "models.fallbacks.tts",
        "ServiceSwitcher(services=[primary, backup], ServiceSwitcherStrategyFailover)",
        "test_config_field_mapping[pipecat-models.fallbacks.tts]",
    ),
    ConfigMapping(
        "limits.max_duration_s",
        "asyncio duration task queues EndFrame(reason='duration_limit')",
        "test_config_field_mapping[pipecat-limits.max_duration_s]",
    ),
    ConfigMapping(
        "limits.max_concurrent",
        "AdmissionController reservation before carrier answer or WebRTC SDP answer",
        "test_config_field_mapping[pipecat-limits.max_concurrent]",
    ),
    ConfigMapping(
        "limits.silence_hangup_s",
        "LLMUserAggregatorParams.user_idle_timeout plus PipelineWorker idle backstop",
        "test_config_field_mapping[pipecat-limits.silence_hangup_s]",
    ),
    ConfigMapping(
        "limits.daily_spend_alert_usd",
        "PipecatPolicy threshold plus PipelineParams.enable_usage_metrics",
        "test_config_field_mapping[pipecat-limits.daily_spend_alert_usd]",
    ),
    ConfigMapping(
        "behavior.allow_interruptions",
        "AlwaysUserMuteStrategy while bot speaks when false; native VAD interruption when true",
        "test_config_field_mapping[pipecat-behavior.allow_interruptions]",
    ),
    ConfigMapping(
        "behavior.voicemail",
        "signed carrier AMD callback selects hangup or connect-machine flow",
        "test_config_field_mapping[pipecat-behavior.voicemail]",
    ),
    ConfigMapping(
        "behavior.dtmf",
        "carrier serializer InputDTMFFrame gated by DTMFPolicyProcessor",
        "test_config_field_mapping[pipecat-behavior.dtmf]",
    ),
    ConfigMapping(
        "behavior.transfer_number",
        "global native FlowsFunctionSchema invokes carrier transfer and queues EndFrame",
        "test_config_field_mapping[pipecat-behavior.transfer_number]",
    ),
    ConfigMapping(
        "behavior.end_call_phrases",
        "assistant aggregator turn event matches phrase and queues EndFrame",
        "test_config_field_mapping[pipecat-behavior.end_call_phrases]",
    ),
    ConfigMapping(
        "voice.fallback_language",
        "typed STTUpdateSettingsFrame and TTSUpdateSettingsFrame for every service member",
        "test_config_field_mapping[pipecat-voice.fallback_language]",
    ),
    ConfigMapping(
        "phone.record",
        "carrier live-call recording command plus signed callback parsing "
        "and authenticated download",
        "test_config_field_mapping[pipecat-phone.record]",
    ),
    ConfigMapping(
        "observability.prometheus_enabled",
        "process-local TelemetryServer enabled explicitly",
        "test_config_field_mapping[pipecat-observability.prometheus_enabled]",
    ),
    ConfigMapping(
        "observability.prometheus_bind",
        "uvicorn metrics listener host",
        "test_config_field_mapping[pipecat-observability.prometheus_bind]",
    ),
    ConfigMapping(
        "observability.prometheus_port",
        "uvicorn metrics listener port",
        "test_config_field_mapping[pipecat-observability.prometheus_port]",
    ),
    ConfigMapping(
        "observability.prometheus_path",
        "Prometheus ASGI exposition route",
        "test_config_field_mapping[pipecat-observability.prometheus_path]",
    ),
    ConfigMapping(
        "observability.otlp_endpoint",
        "fork-safe OTLPSpanExporter with BatchSpanProcessor",
        "test_config_field_mapping[pipecat-observability.otlp_endpoint]",
    ),
    ConfigMapping(
        "observability.otlp_headers_env",
        "secret OTLP headers loaded from the named environment variable",
        "test_config_field_mapping[pipecat-observability.otlp_headers_env]",
    ),
)
