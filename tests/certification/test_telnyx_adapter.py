"""Offline certification of the Telnyx Voice API and TeXML carrier path."""

from __future__ import annotations

import base64
import json
import time
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from voicey.errors import VoiceyError
from voicey.telephony import (
    LiveKitTarget,
    PipecatTarget,
    RollbackToken,
    TelephonyAdapter,
    TelephonyRequest,
)
from voicey.telephony.ledger import TelephonyLedger
from voicey.telephony.telnyx import TelnyxAdapter

NUMBER = "+14155550100"
NUMBER_ID = "number-1"
CALL_ID = "v3:certified-call-control-id"
TARGET = PipecatTarget(
    "https://voice.example.test",
    ws_path="/telnyx/media/reservation",
    answer_path="/telnyx/answer",
    event_path="/telnyx/events",
    recording_path="/telnyx/recordings",
    amd_path="/telnyx/amd",
)


class FakeTelnyx:
    def __init__(self) -> None:
        self.number: dict[str, object] = {
            "id": NUMBER_ID,
            "phone_number": NUMBER,
            "connection_id": "old-connection",
            "country_iso_alpha2": "US",
            "locality": "San Francisco",
            "administrative_area": "CA",
            "features": ["voice", "sms"],
        }
        self.calls: list[tuple[str, str, dict[str, object] | None]] = []
        self.failures: dict[tuple[str, str], int] = {}
        self.available = True
        self.purchased: dict[str, object] | None = None
        self.patch_response_connection: str | None = None

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/v2")
        body: dict[str, object] | None = None
        if request.content:
            loaded: object = json.loads(request.content)
            body = cast("dict[str, object]", loaded)
        self.calls.append((request.method, path, body))
        status = self.failures.get((request.method, path))
        if status is not None:
            return httpx.Response(status, json={"errors": [{"code": "private"}]})
        if request.method == "GET" and path == "/balance":
            return httpx.Response(200, json={"data": {"balance": "10.00", "currency": "USD"}})
        if request.method == "GET" and path == "/phone_numbers":
            numbers = [self.number]
            if self.purchased is not None:
                numbers.append(self.purchased)
            requested = request.url.params.get("filter[phone_number]")
            if requested:
                numbers = [item for item in numbers if item["phone_number"] == requested]
            return httpx.Response(200, json={"data": numbers})
        if request.method == "GET" and path == f"/phone_numbers/{NUMBER_ID}":
            return httpx.Response(200, json={"data": self.number})
        if request.method == "PATCH" and path == f"/phone_numbers/{NUMBER_ID}":
            assert body is not None
            self.number.update(body)
            response_number = dict(self.number)
            if self.patch_response_connection is not None:
                response_number["connection_id"] = self.patch_response_connection
            return httpx.Response(200, json={"data": response_number})
        if request.method == "DELETE" and path == f"/phone_numbers/{NUMBER_ID}":
            return httpx.Response(204)
        if request.method == "GET" and path == "/available_phone_numbers":
            data = [{"phone_number": "+14155550199"}] if self.available else []
            return httpx.Response(200, json={"data": data})
        if request.method == "POST" and path == "/number_orders":
            self.purchased = {
                "id": "number-purchased",
                "phone_number": "+14155550199",
                "country_iso_alpha2": "US",
                "features": ["voice"],
            }
            return httpx.Response(201, json={"data": {"id": "order-1"}})
        if request.method == "POST" and path == "/calls":
            return httpx.Response(201, json={"data": {"call_control_id": CALL_ID}})
        if request.method == "POST" and path.startswith(f"/calls/{CALL_ID}/actions/"):
            return httpx.Response(200, json={"data": {"result": "ok"}})
        return httpx.Response(404, json={"errors": [{"code": "not_found"}]})


@pytest.fixture
def adapter_bundle(
    tmp_path: Path,
) -> Iterator[tuple[TelnyxAdapter, FakeTelnyx, TelephonyLedger, Ed25519PrivateKey]]:
    fake = FakeTelnyx()
    private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    client = httpx.Client(
        base_url="https://api.telnyx.com/v2",
        transport=httpx.MockTransport(fake.handler),
    )
    ledger = TelephonyLedger(tmp_path / "telnyx.sqlite3")
    adapter = TelnyxAdapter(
        api_key="KEY-not-real",  # pragma: allowlist secret
        public_key=public_key.hex(),
        connection_id="connection-voicey",
        ledger=ledger,
        client=client,
        clock=lambda: 1_700_000_000,
    )
    try:
        yield adapter, fake, ledger, private_key
    finally:
        client.close()
        ledger.close()


def _signed_request(
    private_key: Ed25519PrivateKey,
    document: dict[str, object],
    *,
    timestamp: int = 1_700_000_000,
) -> TelephonyRequest:
    body = json.dumps(document, separators=(",", ":"))
    signature = base64.b64encode(private_key.sign(f"{timestamp}|{body}".encode())).decode()
    return TelephonyRequest(
        scheme="https",
        host="voice.example.test",
        path="/telnyx/events",
        headers={
            "telnyx-timestamp": str(timestamp),
            "telnyx-signature-ed25519": signature,
        },
        raw_body=body,
    )


def _event(
    event_type: str,
    *,
    payload: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "data": {
            "record_type": "event",
            "event_type": event_type,
            "id": "event-1",
            "occurred_at": "2026-07-27T00:00:00Z",
            "payload": {
                "call_control_id": CALL_ID,
                "call_leg_id": "leg-1",
                **(payload or {}),
            },
        }
    }


def test_protocol_account_and_number_lifecycle(
    adapter_bundle: tuple[TelnyxAdapter, FakeTelnyx, TelephonyLedger, Ed25519PrivateKey],
) -> None:
    adapter, fake, _, _ = adapter_bundle
    assert isinstance(adapter, TelephonyAdapter)
    assert adapter.capabilities.livekit_sip
    assert adapter.capabilities.native_outbound_idempotency
    assert adapter.account_state().balance == "10.00"

    numbers = adapter.list_numbers()
    assert numbers[0].number == NUMBER
    assert numbers[0].provider_id == NUMBER_ID
    assert numbers[0].capabilities == frozenset({"voice", "sms"})

    bought = adapter.buy_number("us", "415")
    assert bought.number == "+14155550199"
    assert bought.provider_id == "number-purchased"
    order = next(item for item in fake.calls if item[:2] == ("POST", "/number_orders"))
    assert order[2] == {"phone_numbers": [{"phone_number": "+14155550199"}]}
    adapter.release_number(NUMBER)
    assert ("DELETE", f"/phone_numbers/{NUMBER_ID}", None) in fake.calls

    fake.available = False
    with pytest.raises(VoiceyError, match="VY-TEL-003"):
        adapter.buy_number("US")


def test_routing_is_snapshot_first_confirmed_and_conflict_safe(
    adapter_bundle: tuple[TelnyxAdapter, FakeTelnyx, TelephonyLedger, Ed25519PrivateKey],
) -> None:
    adapter, fake, ledger, _ = adapter_bundle
    token = adapter.point_inbound(NUMBER, TARGET)
    route = ledger.get_route(token.token)
    assert route.state == "applied"
    assert route.snapshot == {"connection_id": "old-connection"}
    assert fake.number["connection_id"] == "connection-voicey"

    adapter.restore(token)
    assert fake.number["connection_id"] == "old-connection"
    assert ledger.get_route(token.token).state == "restored"
    adapter.restore(token)

    second = adapter.point_inbound(NUMBER, TARGET)
    fake.number["connection_id"] = "human-change"
    with pytest.raises(VoiceyError, match="VY-TEL-006"):
        adapter.restore(second)
    assert ledger.get_route(second.token).state == "conflict"


def test_route_rejection_and_ambiguous_outcome_are_distinct(
    adapter_bundle: tuple[TelnyxAdapter, FakeTelnyx, TelephonyLedger, Ed25519PrivateKey],
) -> None:
    adapter, fake, ledger, _ = adapter_bundle
    fake.failures[("PATCH", f"/phone_numbers/{NUMBER_ID}")] = 422
    with pytest.raises(VoiceyError, match="VY-TEL-004"):
        adapter.point_inbound(NUMBER, TARGET)
    assert ledger.open_routes(provider="telnyx") == ()

    fake.failures[("PATCH", f"/phone_numbers/{NUMBER_ID}")] = 503
    with pytest.raises(VoiceyError, match="VY-TEL-006"):
        adapter.point_inbound(NUMBER, TARGET)
    assert ledger.open_routes(provider="telnyx")[0].state == "ambiguous"


def test_route_requires_confirmation_and_restore_surfaces_provider_conflicts(
    adapter_bundle: tuple[TelnyxAdapter, FakeTelnyx, TelephonyLedger, Ed25519PrivateKey],
) -> None:
    adapter, fake, ledger, _ = adapter_bundle
    fake.patch_response_connection = "unexpected"
    with pytest.raises(VoiceyError, match="VY-TEL-006"):
        adapter.point_inbound(NUMBER, TARGET)
    assert ledger.open_routes(provider="telnyx")[0].state == "ambiguous"

    fake.patch_response_connection = None
    fake.number["connection_id"] = "old-connection"
    token = adapter.point_inbound(NUMBER, TARGET)
    fake.failures[("PATCH", f"/phone_numbers/{NUMBER_ID}")] = 422
    with pytest.raises(VoiceyError, match="VY-TEL-006"):
        adapter.restore(token)
    assert ledger.get_route(token.token).state == "conflict"

    del fake.failures[("PATCH", f"/phone_numbers/{NUMBER_ID}")]
    fake.number["connection_id"] = "old-connection"
    second = adapter.point_inbound(NUMBER, TARGET)
    fake.patch_response_connection = "unexpected"
    with pytest.raises(VoiceyError, match="VY-TEL-006"):
        adapter.restore(second)
    assert ledger.get_route(second.token).state == "conflict"


def test_restore_open_routes_recovers_interrupted_applied_route(
    adapter_bundle: tuple[TelnyxAdapter, FakeTelnyx, TelephonyLedger, Ed25519PrivateKey],
) -> None:
    adapter, fake, ledger, _ = adapter_bundle
    token = adapter.point_inbound(NUMBER, TARGET)
    assert adapter.restore_open_routes() == 1
    assert fake.number["connection_id"] == "old-connection"
    assert ledger.get_route(token.token).state == "restored"


def test_outbound_uses_native_idempotency_and_durable_intent(
    adapter_bundle: tuple[TelnyxAdapter, FakeTelnyx, TelephonyLedger, Ed25519PrivateKey],
) -> None:
    adapter, fake, ledger, _ = adapter_bundle
    call_id = adapter.start_call(
        NUMBER,
        "+14155550101",
        TARGET,
        intent_id="intent_certified",
        amd=True,
        record=True,
    )
    assert call_id == CALL_ID
    intent = ledger.get_intent("intent_certified")
    assert intent.state == "submitted"
    assert intent.provider_call_id == CALL_ID
    create = next(item[2] for item in fake.calls if item[:2] == ("POST", "/calls"))
    assert create is not None
    assert create["command_id"] == "intent_certified"
    assert create["answering_machine_detection"] == "detect"
    assert create["record"] == "record-from-answer-dual"
    assert create["webhook_url"] == "https://voice.example.test/telnyx/events"
    decoded = json.loads(base64.b64decode(cast("str", create["client_state"])))
    assert decoded == {"intent_id": "intent_certified"}


def test_outbound_rejection_ambiguity_and_reconciliation(
    adapter_bundle: tuple[TelnyxAdapter, FakeTelnyx, TelephonyLedger, Ed25519PrivateKey],
) -> None:
    adapter, fake, ledger, private_key = adapter_bundle
    fake.failures[("POST", "/calls")] = 422
    with pytest.raises(VoiceyError, match="VY-TEL-004"):
        adapter.start_call(NUMBER, "+14155550101", TARGET, intent_id="intent_rejected")
    assert ledger.get_intent("intent_rejected").state == "rejected"

    fake.failures[("POST", "/calls")] = 503
    with pytest.raises(VoiceyError, match="VY-TEL-007"):
        adapter.start_call(NUMBER, "+14155550101", TARGET, intent_id="intent_ambiguous")
    assert ledger.get_intent("intent_ambiguous").state == "ambiguous"
    with pytest.raises(VoiceyError, match="VY-TEL-007"):
        adapter.reconcile_outbound("intent_ambiguous")

    del fake.failures[("POST", "/calls")]
    client_state = base64.b64encode(b'{"intent_id":"intent_ambiguous"}').decode()
    adapter.parse_event(
        _signed_request(
            private_key,
            _event("call.ringing", payload={"client_state": client_state}),
        )
    )
    reconciled = adapter.reconcile_outbound("intent_ambiguous")
    assert reconciled.state == "submitted"
    assert reconciled.provider_call_id == CALL_ID
    assert len([call for call in fake.calls if call[:2] == ("POST", "/calls")]) == 2


def test_number_order_waits_for_owned_resource_and_times_out(tmp_path: Path) -> None:
    class PendingTelnyx(FakeTelnyx):
        def handler(self, request: httpx.Request) -> httpx.Response:
            response = super().handler(request)
            if request.method == "POST" and request.url.path.endswith("/number_orders"):
                self.purchased = None
            return response

    ticks = iter((0.0, 0.0, 1.0))

    def no_sleep(seconds: float) -> None:
        del seconds

    fake = PendingTelnyx()
    client = httpx.Client(
        base_url="https://api.telnyx.com/v2",
        transport=httpx.MockTransport(fake.handler),
    )
    ledger = TelephonyLedger(tmp_path / "pending-order.sqlite3")
    adapter = TelnyxAdapter(
        api_key="KEY-not-real",  # pragma: allowlist secret
        connection_id="connection-voicey",
        client=client,
        ledger=ledger,
        purchase_timeout_s=0.5,
        purchase_poll_interval_s=0.1,
        monotonic=lambda: next(ticks),
        sleeper=no_sleep,
    )
    try:
        with pytest.raises(VoiceyError) as caught:
            adapter.buy_number("US", "415")
    finally:
        client.close()
        ledger.close()
    assert caught.value.code == "VY-TEL-011"
    assert "order-1" in str(caught.value)


def test_call_control_actions_cover_media_dtmf_transfer_and_hangup(
    adapter_bundle: tuple[TelnyxAdapter, FakeTelnyx, TelephonyLedger, Ed25519PrivateKey],
) -> None:
    adapter, fake, _, _ = adapter_bundle
    adapter.answer_call(CALL_ID)
    adapter.start_media(CALL_ID, TARGET)
    adapter.start_recording(CALL_ID)
    adapter.send_dtmf(CALL_ID, "12#")
    adapter.cold_transfer(CALL_ID, "+14155550102")
    adapter.hangup(CALL_ID)

    actions = {
        path.rsplit("/", maxsplit=1)[-1]: body
        for method, path, body in fake.calls
        if method == "POST" and "/actions/" in path
    }
    assert set(actions) == {
        "answer",
        "streaming_start",
        "record_start",
        "send_dtmf",
        "transfer",
        "hangup",
    }
    stream = actions["streaming_start"]
    assert stream is not None
    assert stream["stream_url"] == "wss://voice.example.test/telnyx/media/reservation"
    assert stream["stream_track"] == "both_tracks"
    assert stream["stream_bidirectional_mode"] == "rtp"
    assert stream["stream_bidirectional_codec"] == "PCMU"
    assert stream["stream_bidirectional_sampling_rate"] == 8000
    recording = actions["record_start"]
    assert recording is not None
    assert recording["format"] == "mp3"
    assert recording["channels"] == "dual"
    assert recording["command_id"]


def test_texml_response_is_bidirectional_parameterized_and_bounded(
    adapter_bundle: tuple[TelnyxAdapter, FakeTelnyx, TelephonyLedger, Ed25519PrivateKey],
) -> None:
    adapter, _, _, _ = adapter_bundle
    xml = adapter.answer_response(
        PipecatTarget(
            "https://voice.example.test",
            ws_path="/telnyx/media/token",
            event_path="/telnyx/events",
            custom_parameters={"voicey_token": "token-1"},
        )
    )
    assert "<Connect>" in xml
    assert 'url="wss://voice.example.test/telnyx/media/token"' in xml
    assert 'bidirectionalMode="rtp"' in xml
    assert 'bidirectionalCodec="PCMU"' in xml
    assert 'bidirectionalSamplingRate="8000"' in xml
    assert '<Parameter name="voicey_token" value="token-1"' in xml

    with pytest.raises(VoiceyError, match="VY-TEL-002"):
        adapter.answer_response(
            PipecatTarget(
                "https://voice.example.test",
                custom_parameters={"oversize": "x" * 4090},
            )
        )


def test_livekit_target_is_never_silently_downgraded(
    adapter_bundle: tuple[TelnyxAdapter, FakeTelnyx, TelephonyLedger, Ed25519PrivateKey],
) -> None:
    adapter, _, _, _ = adapter_bundle
    target = LiveKitTarget(
        project="project.livekit.cloud",
        sip_uri="sip:project.sip.livekit.cloud",
    )
    for operation in (
        lambda: adapter.point_inbound(NUMBER, target),
        lambda: adapter.start_call(NUMBER, "+14155550101", target),
        lambda: adapter.answer_response(target),
    ):
        with pytest.raises(VoiceyError, match="VY-TEL-002"):
            operation()


def test_ed25519_verification_rejects_tamper_replay_future_and_websocket(
    adapter_bundle: tuple[TelnyxAdapter, FakeTelnyx, TelephonyLedger, Ed25519PrivateKey],
    tmp_path: Path,
) -> None:
    adapter, _, _, private_key = adapter_bundle
    request = _signed_request(private_key, _event("call.answered"))
    assert adapter.verify_request(request)

    assert not adapter.verify_request(replace(request, raw_body=f"{request.raw_body} "))
    assert not adapter.verify_request(
        _signed_request(
            private_key,
            _event("call.answered"),
            timestamp=1,
        )
    )
    assert not adapter.verify_request(replace(request, headers={}))
    assert not adapter.verify_request(
        replace(
            request,
            headers={
                "telnyx-timestamp": "not-an-integer",
                "telnyx-signature-ed25519": "not-base64",
            },
        )
    )

    no_key = TelnyxAdapter(
        api_key="KEY-not-real",  # pragma: allowlist secret
        ledger_path=tmp_path / "no-key-cert.sqlite3",
        client=adapter._client,  # pyright: ignore[reportPrivateUsage]
    )
    try:
        assert not no_key.verify_request(request)
    finally:
        no_key.ledger.close()
    assert not adapter.verify_request(
        _signed_request(private_key, _event("call.answered"), timestamp=1_700_000_301)
    )
    assert not adapter.verify_request(
        TelephonyRequest(
            scheme="wss",
            host="voice.example.test",
            path="/telnyx/media/token",
            headers=request.headers,
            raw_body=request.raw_body,
            is_websocket=True,
        )
    )


def test_signed_json_event_mapping_and_intent_binding(
    adapter_bundle: tuple[TelnyxAdapter, FakeTelnyx, TelephonyLedger, Ed25519PrivateKey],
) -> None:
    adapter, _, ledger, private_key = adapter_bundle
    ledger.prepare_intent(
        intent_id="intent_callback",
        provider="telnyx",
        from_number=NUMBER,
        to_number="+14155550101",
        target={},
    )
    ledger.transition_intent(
        "intent_callback",
        expected=("prepared",),
        state="ambiguous",
    )
    client_state = base64.b64encode(b'{"intent_id":"intent_callback"}').decode()
    request = _signed_request(
        private_key,
        _event(
            "call.hangup",
            payload={
                "client_state": client_state,
                "hangup_cause": "normal_clearing",
            },
        ),
    )
    assert adapter.verify_request(request)
    event = adapter.parse_event(request)
    assert event.type == "completed"
    assert event.ended_reason == "provider_hangup"
    assert event.intent_id == "intent_callback"
    assert ledger.get_intent("intent_callback").state == "terminal"

    failed = adapter.parse_event(
        _signed_request(
            private_key,
            _event("call.hangup", payload={"hangup_cause": "unallocated_number"}),
        )
    )
    assert failed.type == "failed"
    assert failed.ended_reason == "carrier_error"

    inbound = adapter.parse_event(
        _signed_request(
            private_key,
            _event("call.answered", payload={"direction": "incoming"}),
        )
    )
    outbound = adapter.parse_event(
        _signed_request(
            private_key,
            _event("call.answered", payload={"direction": "outgoing"}),
        )
    )
    assert inbound.direction == "inbound"
    assert outbound.direction == "outbound"


@pytest.mark.parametrize(
    ("event_type", "payload", "expected_type"),
    [
        ("call.initiated", {}, "initiated"),
        ("call.ringing", {}, "ringing"),
        ("call.answered", {}, "answered"),
        ("streaming.started", {}, "answered"),
        ("streaming.failed", {}, "failed"),
        ("call.dtmf.received", {"digit": "#"}, "dtmf"),
        ("call.machine.detection.ended", {"result": "human"}, "amd"),
        (
            "call.recording.saved",
            {
                "recording_id": "recording-1",
                "recording_urls": {"mp3": "https://api.telnyx.test/recording.mp3"},
            },
            "recording_ready",
        ),
        ("call.recording.failed", {}, "recording_failed"),
    ],
)
def test_all_supported_json_events(
    adapter_bundle: tuple[TelnyxAdapter, FakeTelnyx, TelephonyLedger, Ed25519PrivateKey],
    event_type: str,
    payload: dict[str, object],
    expected_type: str,
) -> None:
    adapter, _, _, private_key = adapter_bundle
    event = adapter.parse_event(_signed_request(private_key, _event(event_type, payload=payload)))
    assert event.type == expected_type
    if event_type == "call.dtmf.received":
        assert event.digits == "#"
    if event_type == "call.machine.detection.ended":
        assert event.answered_by == "human"
    if event_type == "call.recording.saved":
        assert event.recording_sid == "recording-1"
        assert event.recording_url == "https://api.telnyx.test/recording.mp3"


def test_texml_callback_and_malformed_events_are_strict(
    adapter_bundle: tuple[TelnyxAdapter, FakeTelnyx, TelephonyLedger, Ed25519PrivateKey],
) -> None:
    adapter, _, _, private_key = adapter_bundle
    completed = adapter.parse_event(
        TelephonyRequest(
            scheme="https",
            host="voice.example.test",
            path="/telnyx/answer",
            headers={},
            form={"CallControlId": CALL_ID, "CallStatus": "completed"},
        )
    )
    assert completed.type == "completed"
    assert completed.ended_reason == "provider_hangup"
    with pytest.raises(VoiceyError, match="VY-TEL-008"):
        adapter.parse_event(_signed_request(private_key, _event("future.event")))
    with pytest.raises(VoiceyError, match="VY-TEL-008"):
        adapter.parse_event(
            TelephonyRequest(
                scheme="https",
                host="voice.example.test",
                path="/telnyx/events",
                headers={},
                form={"CallSid": CALL_ID, "CallStatus": "future-status"},
            )
        )
    with pytest.raises(VoiceyError, match="VY-TEL-008"):
        adapter.parse_event(
            TelephonyRequest(
                scheme="https",
                host="voice.example.test",
                path="/telnyx/events",
                headers={},
                raw_body="{",
            )
        )


@pytest.mark.parametrize(
    "document",
    [
        [],
        {"data": []},
        {"data": {"event_type": "call.answered"}},
        {
            "data": {
                "event_type": "call.answered",
                "payload": {"call_control_id": CALL_ID, "direction": "sideways"},
            }
        },
        {
            "data": {
                "event_type": "call.answered",
                "payload": {"call_control_id": CALL_ID, "client_state": "not-base64"},
            }
        },
        {
            "data": {
                "event_type": "call.recording.saved",
                "payload": {"call_control_id": CALL_ID},
            }
        },
    ],
)
def test_malformed_json_event_envelopes_fail_closed(
    adapter_bundle: tuple[TelnyxAdapter, FakeTelnyx, TelephonyLedger, Ed25519PrivateKey],
    document: object,
) -> None:
    adapter, _, _, _ = adapter_bundle
    request = TelephonyRequest(
        scheme="https",
        host="voice.example.test",
        path="/telnyx/events",
        headers={"content-type": "application/json"},
        raw_body=json.dumps(document),
    )
    with pytest.raises(VoiceyError, match="VY-TEL-008"):
        adapter.parse_event(request)


@pytest.mark.parametrize(
    ("status", "expected_type"),
    [
        ("queued", "initiated"),
        ("ringing", "ringing"),
        ("in-progress", "answered"),
        ("completed", "completed"),
        ("busy", "failed"),
    ],
)
def test_texml_status_matrix_and_dtmf(
    adapter_bundle: tuple[TelnyxAdapter, FakeTelnyx, TelephonyLedger, Ed25519PrivateKey],
    status: str,
    expected_type: str,
) -> None:
    adapter, _, _, _ = adapter_bundle
    event = adapter.parse_event(
        TelephonyRequest(
            scheme="https",
            host="voice.example.test",
            path="/telnyx/events",
            headers={},
            form={"CallSid": CALL_ID, "CallStatus": status},
        )
    )
    assert event.type == expected_type

    dtmf = adapter.parse_event(
        TelephonyRequest(
            scheme="https",
            host="voice.example.test",
            path="/telnyx/events",
            headers={},
            form={"CallSid": CALL_ID, "Digits": "9#"},
        )
    )
    assert dtmf.digits == "9#"


def test_validation_and_safe_http_errors(
    tmp_path: Path,
    adapter_bundle: tuple[TelnyxAdapter, FakeTelnyx, TelephonyLedger, Ed25519PrivateKey],
) -> None:
    adapter, fake, _, _ = adapter_bundle
    for operation in (
        lambda: adapter.buy_number("USA"),
        lambda: adapter.buy_number("US", "bad"),
        lambda: adapter.start_call("bad", "+14155550101", TARGET),
        lambda: adapter.start_call(NUMBER, "+14155550101", TARGET, timeout_s=1),
        lambda: adapter.send_dtmf(CALL_ID, "A"),
    ):
        with pytest.raises(VoiceyError, match="VY-TEL-002"):
            operation()
    fake.failures[("GET", "/balance")] = 401
    with pytest.raises(VoiceyError) as rejected:
        adapter.account_state()
    assert rejected.value.code == "VY-TEL-004"
    assert "private" not in str(rejected.value)

    with pytest.raises(VoiceyError, match="VY-TEL-002"):
        TelnyxAdapter(
            api_key="KEY-not-real",  # pragma: allowlist secret
            public_key="invalid",
            ledger_path=tmp_path / "invalid.sqlite3",
        )
    no_connection = TelnyxAdapter(
        api_key="KEY-not-real",  # pragma: allowlist secret
        ledger_path=tmp_path / "no-connection.sqlite3",
        client=httpx.Client(
            base_url="https://api.telnyx.com/v2",
            transport=httpx.MockTransport(fake.handler),
        ),
    )
    try:
        with pytest.raises(VoiceyError, match="VY-TEL-002"):
            no_connection.point_inbound(NUMBER, TARGET)
    finally:
        no_connection._client.close()  # pyright: ignore[reportPrivateUsage]
        no_connection.ledger.close()


@pytest.mark.parametrize(
    "values",
    [
        {"api_key": ""},  # pragma: allowlist secret
        {"api_key": "key", "base_url": "http://api.telnyx.test"},  # pragma: allowlist secret
        {"api_key": "key", "replay_tolerance_s": 1},  # pragma: allowlist secret
        {"api_key": "key", "replay_tolerance_s": 901},  # pragma: allowlist secret
        {"api_key": "key", "purchase_timeout_s": 0},  # pragma: allowlist secret
        {"api_key": "key", "purchase_poll_interval_s": 0},  # pragma: allowlist secret
    ],
)
def test_constructor_rejects_unsafe_transport_and_timing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    values: dict[str, object],
) -> None:
    monkeypatch.delenv("TELNYX_API_KEY", raising=False)
    with pytest.raises(VoiceyError, match="VY-TEL-002"):
        TelnyxAdapter(
            ledger_path=tmp_path / "invalid-constructor.sqlite3",
            **cast("dict[str, Any]", values),
        )


def test_response_envelope_and_feature_shapes_are_strict(tmp_path: Path) -> None:
    outcomes: list[httpx.Response | Exception] = [
        httpx.Response(200, json=[]),
        httpx.Response(200, content=b"not-json"),
        httpx.Response(200, json={"data": []}),
        httpx.Response(200, json={"data": {}}),
        httpx.Response(200, json={"data": ["bad-item"]}),
        httpx.ConnectError("offline"),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return httpx.Response(
            outcome.status_code,
            request=request,
            content=outcome.content,
            headers=outcome.headers,
        )

    client = httpx.Client(
        base_url="https://api.telnyx.com/v2",
        transport=httpx.MockTransport(handler),
    )
    ledger = TelephonyLedger(tmp_path / "envelopes.sqlite3")
    adapter = TelnyxAdapter(
        api_key="key",  # pragma: allowlist secret
        client=client,
        ledger=ledger,
    )
    try:
        for operation in (
            adapter.account_state,
            adapter.account_state,
            adapter.account_state,
            adapter.list_numbers,
            adapter.list_numbers,
            adapter.account_state,
        ):
            with pytest.raises(VoiceyError, match="VY-TEL-011"):
                operation()
    finally:
        client.close()
        ledger.close()


def test_number_feature_object_and_empty_values_are_normalized(tmp_path: Path) -> None:
    items = [
        {
            "id": "number-object",
            "phone_number": NUMBER,
            "features": {"voice": True, "sms": False},
        },
        {
            "id": "number-empty",
            "phone_number": "+14155550101",
            "features": None,
        },
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, request=request, json={"data": items})

    client = httpx.Client(
        base_url="https://api.telnyx.com/v2",
        transport=httpx.MockTransport(handler),
    )
    ledger = TelephonyLedger(tmp_path / "features.sqlite3")
    adapter = TelnyxAdapter(
        api_key="key",  # pragma: allowlist secret
        client=client,
        ledger=ledger,
    )
    try:
        numbers = adapter.list_numbers()
    finally:
        client.close()
        ledger.close()
    assert numbers[0].capabilities == frozenset({"voice"})
    assert numbers[1].capabilities == frozenset()


def test_restore_rejects_foreign_and_unknown_tokens(
    adapter_bundle: tuple[TelnyxAdapter, FakeTelnyx, TelephonyLedger, Ed25519PrivateKey],
) -> None:
    adapter, _, _, _ = adapter_bundle
    with pytest.raises(VoiceyError, match="VY-TEL-006"):
        adapter.restore(RollbackToken(provider="twilio", token="route_foreign"))
    with pytest.raises(VoiceyError, match="VY-TEL-006"):
        adapter.restore(RollbackToken(provider="telnyx", token="route_missing"))


def test_signature_timestamp_uses_integer_seconds(
    adapter_bundle: tuple[TelnyxAdapter, FakeTelnyx, TelephonyLedger, Ed25519PrivateKey],
) -> None:
    adapter, _, _, private_key = adapter_bundle
    now = int(time.time())
    request = _signed_request(private_key, _event("call.answered"), timestamp=now)
    adapter._clock = lambda: now  # pyright: ignore[reportPrivateUsage]
    assert adapter.verify_request(request)


@pytest.mark.asyncio
async def test_recording_download_uses_signed_callback_url_and_protected_store(
    tmp_path: Path,
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            request=request,
            content=b"recording-bytes",
            headers={"content-type": "audio/mpeg"},
        )

    class MemoryArtifacts:
        def __init__(self) -> None:
            self.values: dict[str, bytes] = {}

        async def put(self, storage_key: str, content: bytes) -> None:
            self.values[storage_key] = content

        async def read(self, storage_key: str) -> bytes:
            return self.values[storage_key]

        async def delete(self, storage_key: str) -> None:
            self.values.pop(storage_key, None)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as recording_http:
        ledger = TelephonyLedger(tmp_path / "recording.sqlite3")
        adapter = TelnyxAdapter(
            api_key="KEY-not-real",  # pragma: allowlist secret
            ledger=ledger,
            recording_client=recording_http,
        )
        artifacts = MemoryArtifacts()
        try:
            key = await adapter.download_recording(
                "https://storage.example.test/signed/recording.mp3?signature=opaque",
                artifact_store=artifacts,
                storage_key="recordings/rec_1.mp3",
            )
        finally:
            ledger.close()

    assert requests[0].url.scheme == "https"
    assert "authorization" not in requests[0].headers
    assert key == "recordings/rec_1.mp3"
    assert artifacts.values[key] == b"recording-bytes"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("url", "status", "headers", "max_bytes"),
    [
        ("http://storage.example.test/recording.mp3", 200, {"content-type": "audio/mpeg"}, 100),
        ("https://storage.example.test/recording.mp3", 200, {"content-type": "audio/mpeg"}, 0),
        (
            "https://storage.example.test/recording.mp3",
            200,
            {"content-type": "text/html"},
            100,
        ),
        (
            "https://storage.example.test/recording.mp3",
            200,
            {"content-type": "audio/mpeg", "content-length": "1000"},
            10,
        ),
        (
            "https://storage.example.test/recording.mp3",
            200,
            {"content-type": "audio/mpeg"},
            10,
        ),
        (
            "https://storage.example.test/recording.mp3",
            503,
            {"content-type": "audio/mpeg"},
            100,
        ),
    ],
)
async def test_recording_download_failures_are_cataloged(
    tmp_path: Path,
    url: str,
    status: int,
    headers: dict[str, str],
    max_bytes: int,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status,
            request=request,
            content=b"too-many-bytes",
            headers=headers,
        )

    class NullArtifacts:
        async def put(self, storage_key: str, content: bytes) -> None:
            del storage_key, content

        async def read(self, storage_key: str) -> bytes:
            del storage_key
            return b""

        async def delete(self, storage_key: str) -> None:
            del storage_key

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as recording_http:
        ledger = TelephonyLedger(tmp_path / f"recording-{status}-{max_bytes}.sqlite3")
        adapter = TelnyxAdapter(
            api_key="KEY-not-real",  # pragma: allowlist secret
            ledger=ledger,
            recording_client=recording_http,
        )
        try:
            with pytest.raises(VoiceyError) as caught:
                await adapter.download_recording(
                    url,
                    artifact_store=NullArtifacts(),
                    storage_key="recordings/rec.mp3",
                    max_bytes=max_bytes,
                )
        finally:
            ledger.close()
    assert caught.value.code == "VY-TEL-009"
