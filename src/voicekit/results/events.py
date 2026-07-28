"""Deterministic webhook payload construction and field-level redaction."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, cast

from pydantic import JsonValue

from voicekit.obs.latency import LatencySeries
from voicekit.obs.logging import REDACTED, scrub_secrets
from voicekit.obs.records import CallRecord
from voicekit.results.schema import WebhookEvent
from voicekit.storage.models import EventType, RecordingReady, ResultDeliveryConfig


def build_event_body(
    *,
    event_id: str,
    event_type: EventType,
    call: CallRecord,
    ended_at: datetime,
    ended_reason: str,
    outcome: str | None,
    data: Mapping[str, JsonValue],
    interruptions: int,
    delivery: ResultDeliveryConfig,
    recording: RecordingReady | None,
    recording_id: str | None,
) -> bytes:
    """Build canonical immutable bytes for push and pull parity."""
    duration_s = max(0, int((ended_at - call.started_at).total_seconds()))
    payload: dict[str, Any] = {
        "event": event_type,
        "id": event_id,
        "call": {
            "id": call.call_id,
            "direction": call.direction,
            "from": call.from_number,
            "to": call.to_number,
            "started_at": _iso(call.started_at),
            "ended_at": _iso(ended_at),
            "duration_s": duration_s,
            "ended_reason": ended_reason,
        },
        "agent": {
            "name": call.agent_name,
            "runtime": call.runtime,
            "config_hash": call.config_hash,
        },
        "outcome": outcome,
    }
    if "data" in delivery.include:
        payload["data"] = dict(data)
    if "transcript" in delivery.include:
        payload["transcript"] = [
            {"role": turn.role, "text": turn.text, "t_ms": turn.t_ms}
            for turn in call.transcript
            if turn.role in {"user", "assistant"}
        ]
    if "recording" in delivery.include:
        if recording is not None:
            payload["recording"] = {
                "id": recording.recording_id,
                "status": "ready",
                "url": recording.access_url,
            }
        elif recording_id is not None:
            payload["recording"] = {
                "id": recording_id,
                "status": "pending",
                "url": None,
            }
        else:
            payload["recording"] = None
    if "metrics" in delivery.include:
        series = LatencySeries()
        for sample in call.latency:
            series.record(**sample.model_dump())
        summaries = series.summaries()
        e2e = summaries.get("e2e")
        payload["metrics"] = {
            "turns": len(
                {turn.turn_id for turn in call.transcript if turn.role in {"user", "assistant"}}
            ),
            "interruptions": interruptions,
            "latency_ms": (None if e2e is None else {"p50": e2e.p50_ms, "p95": e2e.p95_ms}),
        }

    canonical = WebhookEvent.model_validate(payload).model_dump(
        mode="json",
        by_alias=True,
        exclude_unset=True,
    )
    safe_payload = cast("dict[str, Any]", scrub_secrets(canonical))
    for path in delivery.redact:
        _redact_path(safe_payload, path.split("."))
    return json.dumps(
        safe_payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode()


def _redact_path(value: object, path: Sequence[str]) -> None:
    if not path:
        return
    if isinstance(value, dict):
        mapping = cast("dict[str, object]", value)
        if len(path) == 1:
            target = path[0]
            for key, nested in mapping.items():
                if key == target:
                    mapping[key] = REDACTED
                else:
                    _redact_path(nested, path)
            return
        head, *tail = path
        if head in mapping:
            if not tail:
                mapping[head] = REDACTED
            else:
                _redact_path(mapping[head], tail)
        return
    if isinstance(value, list):
        for nested in cast("list[object]", value):
            _redact_path(nested, path)


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
