"""Auditable canonical-config mapping for the Pipecat runtime."""

from __future__ import annotations

from dataclasses import dataclass

from voicekit.config.models import Agent, VoicemailBehavior


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
        )


PIPECAT_CONFIG_MAPPINGS: tuple[ConfigMapping, ...] = (
    ConfigMapping(
        "models.fallbacks.stt",
        "ServiceSwitcher(services=[primary, backup], ServiceSwitcherStrategyFailover)",
        "test_model_fallback_axis_uses_native_switcher[stt]",
    ),
    ConfigMapping(
        "models.fallbacks.llm",
        "LLMSwitcher(llms=[primary, backup], ServiceSwitcherStrategyFailover)",
        "test_model_fallback_axis_uses_native_switcher[llm]",
    ),
    ConfigMapping(
        "models.fallbacks.tts",
        "ServiceSwitcher(services=[primary, backup], ServiceSwitcherStrategyFailover)",
        "test_model_fallback_axis_uses_native_switcher[tts]",
    ),
    ConfigMapping(
        "limits.max_duration_s",
        "asyncio duration task queues EndFrame(reason='duration_limit')",
        "test_duration_limit_queues_end_frame",
    ),
    ConfigMapping(
        "limits.max_concurrent",
        "AdmissionController reservation before carrier answer or WebRTC SDP answer",
        "test_admission_is_atomic_and_busy_at_limit",
    ),
    ConfigMapping(
        "limits.silence_hangup_s",
        "LLMUserAggregatorParams.user_idle_timeout plus PipelineWorker idle backstop",
        "test_policy_fields_reach_native_pipecat_objects",
    ),
    ConfigMapping(
        "limits.daily_spend_alert_usd",
        "PipecatPolicy threshold plus PipelineParams.enable_usage_metrics",
        "test_policy_fields_reach_native_pipecat_objects",
    ),
    ConfigMapping(
        "behavior.allow_interruptions",
        "AlwaysUserMuteStrategy while bot speaks when false; native VAD interruption when true",
        "test_interruption_policy_uses_native_user_mute_strategy",
    ),
    ConfigMapping(
        "behavior.voicemail",
        "signed Twilio AMD callback selects hangup or connect-machine flow",
        "test_voicemail_policy_controls_amd_disposition",
    ),
    ConfigMapping(
        "behavior.dtmf",
        "TwilioFrameSerializer InputDTMFFrame gated by DTMFPolicyProcessor",
        "test_dtmf_policy_filters_native_frame",
    ),
    ConfigMapping(
        "behavior.transfer_number",
        "global native FlowsFunctionSchema invokes carrier transfer and queues EndFrame",
        "test_transfer_config_adds_native_flow_tool",
    ),
    ConfigMapping(
        "behavior.end_call_phrases",
        "assistant aggregator turn event matches phrase and queues EndFrame",
        "test_end_phrase_event_requests_agent_hangup",
    ),
    ConfigMapping(
        "voice.fallback_language",
        "typed STTUpdateSettingsFrame and TTSUpdateSettingsFrame for every service member",
        "test_language_fallback_uses_typed_service_settings",
    ),
)
