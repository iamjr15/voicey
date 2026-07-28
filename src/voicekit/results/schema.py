"""Canonical public webhook payload schema."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from voicekit.config.models import RuntimeName
from voicekit.obs.records import Direction
from voicekit.storage.models import EventType


class WebhookModel(BaseModel):
    """Strict base for every documented webhook object."""

    model_config = ConfigDict(extra="forbid")


class WebhookCall(WebhookModel):
    id: str
    direction: Direction
    from_number: str | None = Field(alias="from")
    to_number: str | None = Field(alias="to")
    started_at: datetime
    ended_at: datetime
    duration_s: int = Field(ge=0)
    ended_reason: str


class WebhookAgent(WebhookModel):
    name: str
    runtime: RuntimeName
    config_hash: str


class WebhookTranscriptTurn(WebhookModel):
    role: Literal["user", "assistant"]
    text: str
    t_ms: int = Field(ge=0)


class WebhookRecording(WebhookModel):
    id: str
    status: Literal["pending", "ready"]
    url: str | None


class WebhookLatency(WebhookModel):
    p50: float = Field(ge=0)
    p95: float = Field(ge=0)


class WebhookMetrics(WebhookModel):
    turns: int = Field(ge=0)
    interruptions: int = Field(ge=0)
    latency_ms: WebhookLatency | None


class WebhookEvent(WebhookModel):
    """The stable envelope delivered and exposed by pull APIs."""

    event: EventType
    id: str
    call: WebhookCall
    agent: WebhookAgent
    outcome: str | None
    data: dict[str, JsonValue] | None = None
    transcript: list[WebhookTranscriptTurn] | None = None
    recording: WebhookRecording | None = None
    metrics: WebhookMetrics | None = None
