"""Runtime-blind storage contract values for lifecycle and delivery."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal, TypeAlias
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator

from voicey.config.models import ResultField

TerminalEventType: TypeAlias = Literal["call.completed", "call.failed"]
EventType: TypeAlias = Literal[
    "call.started",
    "call.completed",
    "call.failed",
    "call.recording.ready",
]
DeliveryStatus: TypeAlias = Literal[
    "pending",
    "delivering",
    "delivered",
    "dead_lettered",
]
EndedReason: TypeAlias = Literal[
    "caller_hangup",
    "agent_hangup",
    "duration_limit",
    "silence_timeout",
    "transferred",
    "voicemail",
    "provider_hangup",
    "stt_unavailable",
    "llm_unavailable",
    "tts_unavailable",
    "carrier_error",
    "provider_error",
    "worker_crash",
    "setup_error",
    "recovery_unknown",
    "unknown",
]
ProviderCallState: TypeAlias = Literal["active", "completed", "failed", "unknown"]


class StorageValue(BaseModel):
    """Strict immutable storage value."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class ResultDeliveryConfig(StorageValue):
    """Non-secret result settings persisted with an active call."""

    endpoint: str
    include: tuple[ResultField, ...] = (
        "transcript",
        "data",
        "recording",
        "metrics",
    )
    redact: tuple[str, ...] = ()
    purge_after_days: int = 30
    recording_enabled: bool = False

    @field_validator("endpoint")
    @classmethod
    def https_endpoint(cls, value: str) -> str:
        parsed = urlsplit(value)
        if parsed.scheme != "https" or not parsed.hostname:
            msg = "result endpoint is not HTTPS. Fix: configure an https:// receiver."
            raise ValueError(msg)
        return value

    @field_validator("purge_after_days")
    @classmethod
    def valid_retention(cls, value: int) -> int:
        if not 1 <= value <= 3650:
            msg = "purge_after_days is outside 1-3650. Fix: choose a supported retention."
            raise ValueError(msg)
        return value


class CallLease(StorageValue):
    """Generation token fencing one active owner."""

    call_id: str
    owner_id: str
    generation: int
    expires_at: datetime


class TerminalRequest(StorageValue):
    """Terminal state proposed by the currently fenced owner."""

    event_type: TerminalEventType
    ended_reason: EndedReason
    ended_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    provider_state: str | None = None

    @field_validator("ended_at")
    @classmethod
    def aware_ended_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            msg = "ended_at lacks a timezone. Fix: use a UTC-aware datetime."
            raise ValueError(msg)
        return value.astimezone(UTC)


class PersistedEvent(StorageValue):
    """Immutable event bytes used by both push and pull surfaces."""

    event_id: str
    call_id: str
    event_type: EventType
    body: bytes
    created_at: datetime


class DeliveryClaim(StorageValue):
    """One exclusively leased delivery attempt."""

    event_id: str
    call_id: str
    endpoint: str
    body: bytes
    attempt_count: int
    lease_owner: str
    lease_expires_at: datetime


class DeliveryRecord(StorageValue):
    """Operator-visible delivery state."""

    event_id: str
    call_id: str
    endpoint: str
    status: DeliveryStatus
    attempt_count: int
    next_attempt_at: datetime
    last_error: str | None
    delivered_at: datetime | None


class RecordingReady(StorageValue):
    """Engine-owned artifact update; carrier URLs never enter this contract."""

    recording_id: str
    access_url: str
    storage_key: str
    ready_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @field_validator("access_url")
    @classmethod
    def https_access_url(cls, value: str) -> str:
        parsed = urlsplit(value)
        if parsed.scheme != "https" or not parsed.hostname:
            msg = (
                "recording access_url is not HTTPS. "
                "Fix: expose the engine-owned authenticated artifact URL."
            )
            raise ValueError(msg)
        return value


class RecordingSnapshot(StorageValue):
    """Protected recording metadata returned only by an admin/read surface."""

    recording_id: str
    call_id: str
    status: Literal["pending", "ready", "failed"]
    access_url: str | None
    storage_key: str | None
    created_at: datetime
    ready_at: datetime | None


class PurgeItem(StorageValue):
    """Durable artifact deletion still owed after database retention."""

    storage_key: str
    artifact_kind: Literal["recording", "backup"]


class ResultSnapshot(StorageValue):
    """Incremental result state flushed while a call is active."""

    outcome: str | None = None
    data: dict[str, JsonValue] = Field(default_factory=dict)
    interruptions: int = 0

    @field_validator("interruptions")
    @classmethod
    def valid_interruptions(cls, value: int) -> int:
        if value < 0:
            msg = "interruptions is negative. Fix: record a non-negative count."
            raise ValueError(msg)
        return value
