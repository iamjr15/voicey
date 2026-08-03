"""Strict wire values for results-relay protocol version one."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator

from voicey.obs.records import NewCall
from voicey.storage.models import CallLease, ResultDeliveryConfig

RelayOperation: TypeAlias = Literal[
    "renew_lease",
    "append_timeline",
    "append_transcript",
    "record_tool_call",
    "record_latency",
    "flush_results",
    "update_provider_state",
    "terminalize",
    "mark_recording_ready",
    "mark_recording_failed",
]


class RelayModel(BaseModel):
    """Strict immutable relay value."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class RelayBeginRequest(RelayModel):
    """Idempotent call creation performed before worker acceptance."""

    idempotency_key: str = Field(min_length=16, max_length=128)
    call: NewCall
    owner_id: str = Field(min_length=1, max_length=200)
    delivery: ResultDeliveryConfig
    lease_ttl_s: float = Field(gt=0, le=3600)
    requested_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @field_validator("requested_at")
    @classmethod
    def aware_requested_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            msg = "requested_at lacks a timezone. Fix: use a UTC-aware datetime."
            raise ValueError(msg)
        return value.astimezone(UTC)


class RelayClaimRequest(RelayModel):
    """Idempotent reservation-to-worker ownership handoff."""

    idempotency_key: str = Field(min_length=16, max_length=128)
    call_id: str = Field(min_length=1, max_length=200)
    expected_owner_id: str = Field(min_length=1, max_length=200)
    owner_id: str = Field(min_length=1, max_length=200)
    lease_ttl_s: float = Field(gt=0, le=3600)
    requested_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @field_validator("requested_at")
    @classmethod
    def aware_requested_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            msg = "requested_at lacks a timezone. Fix: use a UTC-aware datetime."
            raise ValueError(msg)
        return value.astimezone(UTC)


class RelayLeaseResponse(RelayModel):
    """Server-issued generation plus opaque fence and stream cursor."""

    lease: CallLease
    fence_token: str
    next_sequence: int = Field(ge=1)


class RelayUpdateRequest(RelayModel):
    """One ordered, idempotent worker mutation."""

    sequence: int = Field(ge=1)
    idempotency_key: str = Field(min_length=16, max_length=128)
    fence_token: str = Field(min_length=32)
    operation: RelayOperation
    payload: dict[str, JsonValue] = Field(default_factory=dict)
    requested_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @field_validator("requested_at")
    @classmethod
    def aware_requested_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            msg = "requested_at lacks a timezone. Fix: use a UTC-aware datetime."
            raise ValueError(msg)
        return value.astimezone(UTC)


class RelayUpdateResponse(RelayModel):
    """Acknowledgement returned only after durable application."""

    sequence: int = Field(ge=1)
    next_sequence: int = Field(ge=2)
    result: dict[str, JsonValue] = Field(default_factory=dict)
    fence_token: str | None = None


class RelayReadyResponse(RelayModel):
    """Fail-closed worker startup preflight response."""

    ready: Literal[True]
    protocol: Literal["voicey-results-relay/v1"]
    storage_ready: Literal[True]


class FenceClaims(RelayModel):
    """Signed opaque token claims; never accepted without HMAC verification."""

    call_id: str
    owner_id: str
    generation: int = Field(ge=1)
    lease_expires_at: datetime
    token_expires_at: datetime

    @field_validator("lease_expires_at", "token_expires_at")
    @classmethod
    def aware_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            msg = "fence timestamp lacks a timezone. Fix: use a server-issued token."
            raise ValueError(msg)
        return value.astimezone(UTC)
