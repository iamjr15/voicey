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
        "test_livekit_provider_fallbacks_use_native_adapters",
    ),
    ConfigMapping(
        "models.fallbacks.llm",
        "livekit.agents.llm.FallbackAdapter([primary, fallback])",
        "test_livekit_provider_fallbacks_use_native_adapters",
    ),
    ConfigMapping(
        "models.fallbacks.tts",
        "livekit.agents.tts.FallbackAdapter([primary, fallback])",
        "test_livekit_provider_fallbacks_use_native_adapters",
    ),
    ConfigMapping(
        "limits.max_duration_s",
        "call-local duration task invokes AgentSession.aclose()",
        "test_livekit_duration_limit_closes_session",
    ),
    ConfigMapping(
        "limits.max_concurrent",
        "AdmissionController reservation before dispatch token or SIP job session",
        "test_livekit_token_reserves_before_dispatch",
    ),
    ConfigMapping(
        "limits.silence_hangup_s",
        "AgentSession.user_away_timeout and native user_state_changed=away",
        "test_livekit_policy_reaches_native_session",
    ),
    ConfigMapping(
        "limits.daily_spend_alert_usd",
        "LiveKitPolicy threshold plus native session usage events",
        "test_livekit_policy_reaches_native_session",
    ),
    ConfigMapping(
        "behavior.allow_interruptions",
        "TurnHandlingOptions.interruption.enabled plus mutating-tool interruption boundary",
        "test_livekit_policy_reaches_native_session",
    ),
    ConfigMapping(
        "behavior.voicemail",
        "SIP participant disposition maps to configured hangup or message workflow",
        "test_livekit_sip_voicemail_disposition",
    ),
    ConfigMapping(
        "behavior.dtmf",
        "room sip_dtmf_received listener and beta send_dtmf_events tool are capability-gated",
        "test_livekit_dtmf_policy_gates_native_events",
    ),
    ConfigMapping(
        "behavior.transfer_number",
        "native cold transfer tool plus WarmTransferTask workflow",
        "test_livekit_transfer_tools_are_native",
    ),
    ConfigMapping(
        "behavior.end_call_phrases",
        "conversation_item_added assistant message invokes AgentSession.aclose()",
        "test_livekit_observations_flush_incrementally_with_native_metrics",
    ),
    ConfigMapping(
        "voice.fallback_language",
        "native provider update_options(language=...) on every compatible STT/TTS member",
        "test_livekit_language_fallback_updates_all_compatible_members",
    ),
    ConfigMapping(
        "phone.record",
        "AgentSession.start(record=...) plus CA-SID-keyed Twilio trunk recording reconciliation",
        "test_livekit_policy_reaches_native_session",
    ),
)


def detector_mode(value: object) -> TurnDetectionMode:
    """Keep the runtime cast isolated at the installed inference boundary."""
    return cast(TurnDetectionMode, value)
