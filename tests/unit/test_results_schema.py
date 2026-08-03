from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from voicey.results.schema import WebhookEvent


def _payload() -> dict[str, object]:
    return {
        "event": "call.completed",
        "id": "evt_1",
        "call": {
            "id": "call_1",
            "direction": "inbound",
            "from": "+14155550100",
            "to": "+14155550101",
            "started_at": datetime(2026, 1, 1, tzinfo=UTC),
            "ended_at": datetime(2026, 1, 1, 0, 0, 3, tzinfo=UTC),
            "duration_s": 3,
            "ended_reason": "caller_hangup",
        },
        "agent": {
            "name": "front-desk",
            "runtime": "pipecat",
            "config_hash": "abc123",
        },
        "outcome": "booked",
        "data": {"reservation_id": "r_1"},
        "transcript": [{"role": "user", "text": "Hello", "t_ms": 20}],
        "recording": {"id": "rec_1", "status": "ready", "url": "https://example.test/r"},
        "metrics": {
            "turns": 1,
            "interruptions": 0,
            "latency_ms": {"p50": 123.4, "p95": 200.0},
        },
    }


def test_webhook_schema_uses_wire_aliases_and_strict_objects() -> None:
    event = WebhookEvent.model_validate(_payload())
    wire = event.model_dump(mode="json", by_alias=True, exclude_unset=True)

    assert wire["call"]["from"] == "+14155550100"
    assert "from_number" not in wire["call"]
    schema = WebhookEvent.model_json_schema(by_alias=True, mode="serialization")
    assert "from" in schema["$defs"]["WebhookCall"]["properties"]


def test_webhook_schema_rejects_contract_drift() -> None:
    payload = _payload()
    payload["unexpected"] = True
    with pytest.raises(ValidationError, match="Extra inputs"):
        WebhookEvent.model_validate(payload)

    payload = _payload()
    metrics = payload["metrics"]
    assert isinstance(metrics, dict)
    metrics["interruptions"] = -1
    with pytest.raises(ValidationError, match="greater than or equal to 0"):
        WebhookEvent.model_validate(payload)
