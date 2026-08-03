from __future__ import annotations

import base64
from collections.abc import Generator
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from xml.etree import ElementTree

import httpx
import pytest
from twilio.base.exceptions import TwilioRestException
from twilio.request_validator import RequestValidator

from voicey.errors import VoiceyError
from voicey.telephony import (
    LiveKitTarget,
    PipecatTarget,
    RollbackToken,
    TelephonyRequest,
)
from voicey.telephony.ledger import TelephonyLedger
from voicey.telephony.twilio import TwilioAdapter

ACCOUNT_SID = "AC" + "1" * 32
AUTH_TOKEN = "test-auth-token"
NUMBER_SID = "PN" + "2" * 32
CALL_SID = "CA" + "3" * 32
RECORDING_SID = "RE" + "4" * 32
TARGET = PipecatTarget(
    https_base="https://voice.example.test",
    custom_parameters={"agent": "clinic"},
)


def _number(**overrides: object) -> SimpleNamespace:
    values: dict[str, object] = {
        "sid": NUMBER_SID,
        "phone_number": "+14155550100",
        "friendly_name": "Clinic",
        "iso_country": "US",
        "locality": "San Francisco",
        "region": "CA",
        "capabilities": {"voice": True, "sms": True},
        "voice_url": "https://old.example.test/answer",
        "voice_method": "POST",
        "voice_fallback_url": None,
        "voice_fallback_method": "POST",
        "status_callback": "https://old.example.test/status",
        "status_callback_method": "POST",
        "voice_application_sid": None,
        "trunk_sid": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class FakeIncomingContext:
    def __init__(self, owner: FakeIncomingNumbers, sid: str) -> None:
        self.owner = owner
        self.sid = sid

    def fetch(self) -> SimpleNamespace:
        if self.owner.fetch_error is not None:
            raise self.owner.fetch_error
        if self.sid not in self.owner.resources:
            raise TwilioRestException(404, "/numbers", code=20404)
        return self.owner.resources[self.sid]

    def update(self, **arguments: object) -> SimpleNamespace:
        self.owner.update_calls.append(dict(arguments))
        if self.owner.update_error is not None:
            raise self.owner.update_error
        resource = self.fetch()
        for key, value in arguments.items():
            setattr(resource, key, None if value == "" else value)
        if resource.voice_application_sid:
            resource.trunk_sid = None
        if resource.trunk_sid:
            resource.voice_application_sid = None
        if self.owner.after_update is not None:
            self.owner.after_update()
        return resource

    def delete(self) -> bool:
        self.owner.deleted.append(self.sid)
        if not self.owner.delete_result:
            return False
        return self.owner.resources.pop(self.sid, None) is not None


class FakeIncomingNumbers:
    def __init__(self, resource: SimpleNamespace | None = None) -> None:
        self.resources: dict[str, SimpleNamespace] = {}
        if resource is not None:
            self.resources[str(resource.sid)] = resource
        self.update_calls: list[dict[str, object]] = []
        self.deleted: list[str] = []
        self.update_error: Exception | None = None
        self.fetch_error: Exception | None = None
        self.list_error: Exception | None = None
        self.delete_result = True
        self.after_update: Any | None = None

    def __call__(self, sid: str) -> FakeIncomingContext:
        return FakeIncomingContext(self, sid)

    def list(
        self,
        *,
        phone_number: str | None = None,
        limit: int | None = None,
    ) -> list[SimpleNamespace]:
        del limit
        if self.list_error is not None:
            raise self.list_error
        values = list(self.resources.values())
        if phone_number is not None:
            values = [value for value in values if value.phone_number == phone_number]
        return values

    def create(self, *, phone_number: str) -> SimpleNamespace:
        resource = _number(phone_number=phone_number)
        self.resources[str(resource.sid)] = resource
        return resource


class FakeAvailableLocal:
    def __init__(self) -> None:
        self.arguments: dict[str, object] = {}
        self.values = [SimpleNamespace(phone_number="+14155550199")]
        self.error: Exception | None = None

    def list(self, **arguments: object) -> list[SimpleNamespace]:
        self.arguments = dict(arguments)
        if self.error is not None:
            raise self.error
        return self.values


class FakeAvailableNumbers:
    def __init__(self) -> None:
        self.local = FakeAvailableLocal()
        self.country: str | None = None

    def __call__(self, country: str) -> FakeAvailableNumbers:
        self.country = country
        return self


class FakeCallContext:
    def __init__(self, owner: FakeCalls, sid: str) -> None:
        self.owner = owner
        self.sid = sid

    def update(self, **arguments: object) -> SimpleNamespace:
        if self.owner.update_error is not None:
            raise self.owner.update_error
        self.owner.updates.append((self.sid, dict(arguments)))
        return SimpleNamespace(sid=self.sid, status=arguments.get("status", "in-progress"))

    @property
    def recordings(self) -> FakeRecordings:
        return self.owner.recordings


class FakeRecordings:
    def __init__(self) -> None:
        self.values: list[SimpleNamespace] = []
        self.creates: list[dict[str, object]] = []
        self.list_error: Exception | None = None
        self.create_error: Exception | None = None

    def list(self, *, limit: int) -> list[SimpleNamespace]:
        assert limit == 2
        if self.list_error is not None:
            raise self.list_error
        return self.values[:limit]

    def create(self, **arguments: object) -> SimpleNamespace:
        self.creates.append(dict(arguments))
        if self.create_error is not None:
            raise self.create_error
        value = SimpleNamespace(sid=RECORDING_SID, status="in-progress")
        self.values.append(value)
        return value


class FakeCalls:
    def __init__(self) -> None:
        self.creates: list[dict[str, object]] = []
        self.updates: list[tuple[str, dict[str, object]]] = []
        self.create_error: Exception | None = None
        self.update_error: Exception | None = None
        self.list_error: Exception | None = None
        self.created_sid = CALL_SID
        self.listed: list[SimpleNamespace] = []
        self.recordings = FakeRecordings()

    def __call__(self, sid: str) -> FakeCallContext:
        return FakeCallContext(self, sid)

    def create(self, **arguments: object) -> SimpleNamespace:
        self.creates.append(dict(arguments))
        if self.create_error is not None:
            raise self.create_error
        return SimpleNamespace(sid=self.created_sid, status="queued")

    def list(self, **arguments: object) -> list[SimpleNamespace]:
        if self.list_error is not None:
            raise self.list_error
        self.list_arguments = dict(arguments)
        return self.listed


class FakeClient:
    def __init__(self, resource: SimpleNamespace | None = None) -> None:
        self.incoming_phone_numbers = FakeIncomingNumbers(resource)
        self.available_phone_numbers = FakeAvailableNumbers()
        self.calls = FakeCalls()


@pytest.fixture
def adapter_bundle(
    tmp_path: Path,
) -> Generator[tuple[TwilioAdapter, FakeClient, TelephonyLedger]]:
    client = FakeClient(_number())
    ledger = TelephonyLedger(tmp_path / "telephony.sqlite3")
    adapter = TwilioAdapter(
        account_sid=ACCOUNT_SID,
        auth_token=AUTH_TOKEN,
        client=client,
        ledger=ledger,
        expected_public_base="https://voice.example.test",
    )
    yield adapter, client, ledger
    ledger.close()


def test_http_and_websocket_signatures_use_exact_trusted_public_url(
    adapter_bundle: tuple[TwilioAdapter, FakeClient, TelephonyLedger],
) -> None:
    adapter, _, _ = adapter_bundle
    validator = RequestValidator(AUTH_TOKEN)
    form = {"CallSid": CALL_SID, "CallStatus": "ringing"}
    http_url = "https://voice.example.test/twilio/events"
    request = TelephonyRequest(
        scheme="http",
        host="127.0.0.1:8000",
        path="/twilio/events",
        headers={
            "X-Forwarded-Proto": "https",
            "X-Forwarded-Host": "voice.example.test",
            "X-Twilio-Signature": validator.compute_signature(http_url, form),
        },
        form=form,
        peer_host="127.0.0.1",
    )

    assert adapter.verify_request(request)
    tampered = replace(
        request,
        form={"CallSid": CALL_SID, "CallStatus": "completed"},
    )
    assert not adapter.verify_request(tampered)

    ws_url = "wss://voice.example.test/twilio/media"
    websocket = TelephonyRequest(
        scheme="http",
        host="127.0.0.1:8000",
        path="/twilio/media",
        headers={
            "x-forwarded-proto": "https",
            "x-forwarded-host": "voice.example.test",
            "x-twilio-signature": validator.compute_signature(ws_url, {}),
        },
        form={"ignored": "for-websocket"},
        peer_host="127.0.0.1",
        is_websocket=True,
    )
    assert adapter.verify_request(websocket)

    untrusted = replace(request, peer_host="203.0.113.10")
    assert not adapter.verify_request(untrusted)


def test_json_body_hash_signature_is_verified(
    adapter_bundle: tuple[TwilioAdapter, FakeClient, TelephonyLedger],
) -> None:
    adapter, _, _ = adapter_bundle
    validator = RequestValidator(AUTH_TOKEN)
    body = '{"event":"test"}'
    body_hash = validator.compute_hash(body)
    query = f"bodySHA256={body_hash}"
    url = f"https://voice.example.test/twilio/events?{query}"
    request = TelephonyRequest(
        scheme="https",
        host="voice.example.test",
        path="/twilio/events",
        query_string=query,
        headers={"x-twilio-signature": validator.compute_signature(url, {})},
        raw_body=body,
    )

    assert adapter.verify_request(request)


def test_answer_twiml_is_under_limit_uses_nested_parameters_and_no_query(
    adapter_bundle: tuple[TwilioAdapter, FakeClient, TelephonyLedger],
) -> None:
    adapter, _, _ = adapter_bundle

    xml = adapter.answer_response(TARGET)
    root = ElementTree.fromstring(xml)
    stream = root.find("./Connect/Stream")

    assert stream is not None
    assert stream.attrib["url"] == "wss://voice.example.test/twilio/media"
    assert "?" not in stream.attrib["url"]
    parameter = stream.find("Parameter")
    assert parameter is not None
    assert parameter.attrib == {"name": "agent", "value": "clinic"}
    assert len(xml.encode()) <= 4096


def test_live_call_recording_is_dual_channel_callback_bound_and_idempotent(
    adapter_bundle: tuple[TwilioAdapter, FakeClient, TelephonyLedger],
) -> None:
    adapter, client, _ = adapter_bundle

    first = adapter.start_recording(CALL_SID, TARGET)
    second = adapter.start_recording(CALL_SID, TARGET)

    assert first == RECORDING_SID
    assert second == RECORDING_SID
    assert client.calls.recordings.creates == [
        {
            "recording_channels": "dual",
            "recording_status_callback": TARGET.recording_url,
            "recording_status_callback_method": "POST",
            "recording_status_callback_event": ["completed", "absent"],
            "trim": "do-not-trim",
        }
    ]


def test_live_call_recording_fails_closed_on_ambiguity_and_provider_error(
    adapter_bundle: tuple[TwilioAdapter, FakeClient, TelephonyLedger],
) -> None:
    adapter, client, _ = adapter_bundle
    client.calls.recordings.values = [
        SimpleNamespace(sid=RECORDING_SID),
        SimpleNamespace(sid="RE" + "5" * 32),
    ]
    with pytest.raises(VoiceyError) as ambiguous:
        adapter.start_recording(CALL_SID, TARGET)
    assert ambiguous.value.code == "VY-TEL-009"

    client.calls.recordings.values = []
    client.calls.recordings.list_error = ConnectionError("private")
    with pytest.raises(VoiceyError) as unavailable:
        adapter.start_recording(CALL_SID, TARGET)
    assert unavailable.value.code == "VY-TEL-011"


def test_point_and_restore_snapshot_all_precedence_fields_before_mutation(
    adapter_bundle: tuple[TwilioAdapter, FakeClient, TelephonyLedger],
) -> None:
    adapter, client, ledger = adapter_bundle
    saw_prepared = False

    def after_update() -> None:
        nonlocal saw_prepared
        saw_prepared = ledger.open_routes(provider="twilio")[0].state == "prepared"

    client.incoming_phone_numbers.after_update = after_update
    token = adapter.point_inbound("+14155550100", TARGET)

    assert saw_prepared
    record = ledger.get_route(token.token)
    assert record.state == "applied"
    assert set(record.snapshot) == {
        "voice_url",
        "voice_method",
        "voice_fallback_url",
        "voice_fallback_method",
        "status_callback",
        "status_callback_method",
        "voice_application_sid",
        "trunk_sid",
    }
    assert record.applied["voice_application_sid"] is None
    assert record.applied["trunk_sid"] is None

    adapter.restore(token)

    assert ledger.get_route(token.token).state == "restored"
    assert client.incoming_phone_numbers.resources[NUMBER_SID].voice_url == (
        "https://old.example.test/answer"
    )


def test_crash_recovery_restores_prepared_route_and_cas_rejects_manual_change(
    adapter_bundle: tuple[TwilioAdapter, FakeClient, TelephonyLedger],
) -> None:
    adapter, client, ledger = adapter_bundle
    snapshot = {
        key: getattr(client.incoming_phone_numbers.resources[NUMBER_SID], key)
        for key in (
            "voice_url",
            "voice_method",
            "voice_fallback_url",
            "voice_fallback_method",
            "status_callback",
            "status_callback_method",
            "voice_application_sid",
            "trunk_sid",
        )
    }
    applied = {
        **snapshot,
        "voice_url": TARGET.answer_url,
        "status_callback": TARGET.event_url(),
    }
    record = ledger.prepare_route(
        provider="twilio",
        number="+14155550100",
        number_sid=NUMBER_SID,
        snapshot=snapshot,
        applied=applied,
    )
    resource = client.incoming_phone_numbers.resources[NUMBER_SID]
    for field, value in applied.items():
        setattr(resource, field, value)

    assert adapter.recover_routes() == 1
    assert ledger.get_route(record.token).state == "restored"

    conflict = ledger.prepare_route(
        provider="twilio",
        number="+14155550100",
        number_sid=NUMBER_SID,
        snapshot=snapshot,
        applied=applied,
    )
    resource.voice_url = "https://manual.example.test"
    with pytest.raises(VoiceyError) as caught:
        adapter.restore(RollbackToken(provider="twilio", token=conflict.token))
    assert caught.value.code == "VY-TEL-006"
    assert resource.voice_url == "https://manual.example.test"
    assert ledger.get_route(conflict.token).state == "conflict"


def test_number_list_buy_and_release(
    adapter_bundle: tuple[TwilioAdapter, FakeClient, TelephonyLedger],
) -> None:
    adapter, client, _ = adapter_bundle

    listed = adapter.list_numbers()
    purchased = adapter.buy_number("us", "415")
    adapter.release_number(purchased.number)

    assert listed[0].number == "+14155550100"
    assert listed[0].capabilities == frozenset({"voice", "sms"})
    assert client.available_phone_numbers.country == "US"
    assert client.available_phone_numbers.local.arguments["area_code"] == 415
    assert NUMBER_SID in client.incoming_phone_numbers.deleted


def test_number_and_initialization_failures_are_cataloged(tmp_path: Path) -> None:
    with pytest.raises(VoiceyError) as credentials:
        TwilioAdapter(
            account_sid="not-an-account",
            auth_token="",
            client=FakeClient(),
            ledger_path=tmp_path / "invalid.sqlite3",
        )
    with pytest.raises(VoiceyError) as public_base:
        TwilioAdapter(
            account_sid=ACCOUNT_SID,
            auth_token=AUTH_TOKEN,
            client=FakeClient(),
            ledger_path=tmp_path / "base.sqlite3",
            expected_public_base="http://unsafe.example.test",
        )
    assert credentials.value.code == "VY-TEL-002"
    assert public_base.value.code == "VY-TEL-002"

    client = FakeClient(_number())
    ledger = TelephonyLedger(tmp_path / "errors.sqlite3")
    adapter = TwilioAdapter(
        account_sid=ACCOUNT_SID,
        auth_token=AUTH_TOKEN,
        client=client,
        ledger=ledger,
    )
    with pytest.raises(VoiceyError) as country:
        adapter.buy_number("USA")
    with pytest.raises(VoiceyError) as area:
        adapter.buy_number("US", "four")
    client.available_phone_numbers.local.values = []
    with pytest.raises(VoiceyError) as unavailable:
        adapter.buy_number("US")
    client.incoming_phone_numbers.delete_result = False
    with pytest.raises(VoiceyError) as release:
        adapter.release_number(NUMBER_SID)
    client.incoming_phone_numbers.list_error = ConnectionError("private")
    with pytest.raises(VoiceyError) as listing:
        adapter.list_numbers()
    assert country.value.code == "VY-TEL-002"
    assert area.value.code == "VY-TEL-002"
    assert unavailable.value.code == "VY-TEL-003"
    assert release.value.code == "VY-TEL-004"
    assert listing.value.code == "VY-TEL-011"
    ledger.close()


def test_point_failure_mismatch_and_livekit_path_are_fail_closed(
    adapter_bundle: tuple[TwilioAdapter, FakeClient, TelephonyLedger],
) -> None:
    adapter, client, ledger = adapter_bundle
    with pytest.raises(VoiceyError) as livekit:
        adapter.point_inbound(
            "+14155550100",
            LiveKitTarget(project="project", sip_uri="sip:example.test"),
        )
    assert livekit.value.code == "VY-TEL-002"

    client.incoming_phone_numbers.update_error = TwilioRestException(
        400,
        "/IncomingPhoneNumbers",
        code=21401,
    )
    with pytest.raises(VoiceyError) as rejected:
        adapter.point_inbound("+14155550100", TARGET)
    assert rejected.value.code == "VY-TEL-004"
    assert ledger.open_routes(provider="twilio") == ()

    client.incoming_phone_numbers.update_error = ConnectionError("unknown")
    with pytest.raises(VoiceyError) as ambiguous:
        adapter.point_inbound("+14155550100", TARGET)
    assert ambiguous.value.code == "VY-TEL-006"
    assert ledger.open_routes(provider="twilio")[0].state == "ambiguous"

    client.incoming_phone_numbers.update_error = None
    client.incoming_phone_numbers.after_update = lambda: setattr(
        client.incoming_phone_numbers.resources[NUMBER_SID],
        "voice_url",
        "https://mismatch.example.test",
    )
    with pytest.raises(VoiceyError) as mismatch:
        adapter.point_inbound("+14155550100", TARGET)
    assert mismatch.value.code == "VY-TEL-006"


def test_restore_wrong_provider_fetch_failure_mismatch_and_idempotence(
    adapter_bundle: tuple[TwilioAdapter, FakeClient, TelephonyLedger],
) -> None:
    adapter, client, ledger = adapter_bundle
    with pytest.raises(VoiceyError) as provider:
        adapter.restore(RollbackToken(provider="other", token="route_1"))
    assert provider.value.code == "VY-TEL-006"

    token = adapter.point_inbound("+14155550100", TARGET)
    client.incoming_phone_numbers.fetch_error = ConnectionError("down")
    with pytest.raises(VoiceyError) as fetch:
        adapter.restore(token)
    assert fetch.value.code == "VY-TEL-006"
    client.incoming_phone_numbers.fetch_error = None

    client.incoming_phone_numbers.after_update = lambda: setattr(
        client.incoming_phone_numbers.resources[NUMBER_SID],
        "voice_url",
        "https://restore-mismatch.example.test",
    )
    with pytest.raises(VoiceyError) as mismatch:
        adapter.restore(token)
    assert mismatch.value.code == "VY-TEL-006"

    client.incoming_phone_numbers.after_update = None
    resource = client.incoming_phone_numbers.resources[NUMBER_SID]
    for field, value in ledger.get_route(token.token).snapshot.items():
        setattr(resource, field, value)
    adapter.restore(token)
    adapter.restore(token)
    assert ledger.get_route(token.token).state == "restored"


def test_outbound_intent_precedes_create_and_configures_status_recording_and_dtmf(
    adapter_bundle: tuple[TwilioAdapter, FakeClient, TelephonyLedger],
) -> None:
    adapter, client, ledger = adapter_bundle
    intent_id = "intent_certified"
    original_create = client.calls.create
    was_prepared = False

    def create(**arguments: object) -> SimpleNamespace:
        nonlocal was_prepared
        was_prepared = ledger.get_intent(intent_id).state == "prepared"
        return original_create(**arguments)

    client.calls.create = create  # type: ignore[method-assign]

    call_sid = adapter.start_call(
        "+14155550100",
        "+14155550101",
        TARGET,
        intent_id=intent_id,
        send_digits="12w3",
        record=True,
    )

    assert call_sid == CALL_SID
    assert was_prepared
    intent = ledger.get_intent(intent_id)
    assert intent.state == "submitted"
    assert intent.provider_call_id == CALL_SID
    sent = client.calls.creates[0]
    assert "idempotency_key" not in sent
    assert sent["status_callback_event"] == [
        "initiated",
        "ringing",
        "answered",
        "completed",
    ]
    assert sent["recording_channels"] == "dual"
    assert sent["send_digits"] == "12w3"
    assert intent_id in cast("str", sent["status_callback"])
    assert intent_id in cast("str", sent["twiml"])


def test_ambiguous_outbound_is_recorded_once_and_never_blind_retried(
    adapter_bundle: tuple[TwilioAdapter, FakeClient, TelephonyLedger],
) -> None:
    adapter, client, ledger = adapter_bundle
    client.calls.create_error = ConnectionError("uncertain wire result")

    with pytest.raises(VoiceyError) as caught:
        adapter.start_call(
            "+14155550100",
            "+14155550101",
            TARGET,
            intent_id="intent_ambiguous",
        )

    assert caught.value.code == "VY-TEL-007"
    assert len(client.calls.creates) == 1
    assert ledger.get_intent("intent_ambiguous").state == "ambiguous"


def test_definitive_outbound_rejection_is_not_ambiguous(
    adapter_bundle: tuple[TwilioAdapter, FakeClient, TelephonyLedger],
) -> None:
    adapter, client, ledger = adapter_bundle
    client.calls.create_error = TwilioRestException(
        400,
        "/Calls",
        "private detail",
        code=21211,
    )

    with pytest.raises(VoiceyError) as caught:
        adapter.start_call(
            "+14155550100",
            "+14155550101",
            TARGET,
            intent_id="intent_rejected",
        )

    assert caught.value.code == "VY-TEL-004"
    assert "private detail" not in str(caught.value)
    assert ledger.get_intent("intent_rejected").state == "rejected"


def test_outbound_validation_invalid_provider_sid_and_reconcile_failures(
    adapter_bundle: tuple[TwilioAdapter, FakeClient, TelephonyLedger],
) -> None:
    adapter, client, ledger = adapter_bundle
    with pytest.raises(VoiceyError) as number:
        adapter.start_call("invalid", "+14155550101", TARGET)
    with pytest.raises(VoiceyError) as timeout:
        adapter.start_call(
            "+14155550100",
            "+14155550101",
            TARGET,
            timeout_s=1,
        )
    with pytest.raises(VoiceyError) as digits:
        adapter.start_call(
            "+14155550100",
            "+14155550101",
            TARGET,
            send_digits="invalid",
        )
    assert {number.value.code, timeout.value.code, digits.value.code} == {"VY-TEL-002"}

    client.calls.created_sid = "invalid"
    with pytest.raises(VoiceyError) as invalid_sid:
        adapter.start_call(
            "+14155550100",
            "+14155550101",
            TARGET,
            intent_id="intent_invalid_sid",
        )
    assert invalid_sid.value.code == "VY-TEL-007"
    assert ledger.get_intent("intent_invalid_sid").state == "ambiguous"

    client.calls.created_sid = CALL_SID
    ledger.prepare_intent(
        intent_id="intent_no_candidates",
        provider="twilio",
        from_number="+14155550100",
        to_number="+14155550101",
        target={},
    )
    with pytest.raises(VoiceyError) as no_candidates:
        adapter.reconcile_outbound("intent_no_candidates")
    assert no_candidates.value.code == "VY-TEL-007"
    client.calls.list_error = ConnectionError("down")
    with pytest.raises(VoiceyError) as unavailable:
        adapter.reconcile_outbound("intent_no_candidates")
    assert unavailable.value.code == "VY-TEL-011"


def test_status_callback_binds_ambiguous_intent_and_maps_all_terminal_states(
    adapter_bundle: tuple[TwilioAdapter, FakeClient, TelephonyLedger],
) -> None:
    adapter, _, ledger = adapter_bundle
    ledger.prepare_intent(
        intent_id="intent_callback",
        provider="twilio",
        from_number="+14155550100",
        to_number="+14155550101",
        target={},
    )
    ledger.transition_intent(
        "intent_callback",
        expected=("prepared",),
        state="ambiguous",
    )
    event = adapter.parse_event(
        TelephonyRequest(
            scheme="https",
            host="voice.example.test",
            path="/twilio/events/intent_callback",
            headers={},
            form={"CallSid": CALL_SID, "CallStatus": "completed"},
            route_params={"intent_id": "intent_callback"},
        )
    )

    assert event.type == "completed"
    assert event.ended_reason == "provider_hangup"
    assert ledger.get_intent("intent_callback").provider_call_id == CALL_SID
    assert ledger.get_intent("intent_callback").state == "terminal"

    for status in ("busy", "no-answer", "failed", "canceled"):
        failed = adapter.parse_event(
            TelephonyRequest(
                scheme="https",
                host="voice.example.test",
                path="/twilio/events",
                headers={},
                form={"CallSid": CALL_SID, "CallStatus": status},
            )
        )
        assert failed.type == "failed"
        assert failed.ended_reason == "carrier_error"


def test_unique_reconciliation_never_creates_a_second_call(
    adapter_bundle: tuple[TwilioAdapter, FakeClient, TelephonyLedger],
) -> None:
    adapter, client, ledger = adapter_bundle
    ledger.prepare_intent(
        intent_id="intent_reconcile",
        provider="twilio",
        from_number="+14155550100",
        to_number="+14155550101",
        target={},
    )
    ledger.transition_intent(
        "intent_reconcile",
        expected=("prepared",),
        state="ambiguous",
    )
    client.calls.listed = [SimpleNamespace(sid=CALL_SID, status="ringing")]

    reconciled = adapter.reconcile_outbound("intent_reconcile")

    assert reconciled.state == "reconciled"
    assert reconciled.provider_call_id == CALL_SID
    assert client.calls.creates == []


def test_async_amd_holds_media_then_connects_human_or_hangs_up_machine(
    adapter_bundle: tuple[TwilioAdapter, FakeClient, TelephonyLedger],
) -> None:
    adapter, client, _ = adapter_bundle
    adapter.start_call(
        "+14155550100",
        "+14155550101",
        TARGET,
        intent_id="intent_amd",
        amd=True,
    )

    create = client.calls.creates[0]
    assert "<Pause" in cast("str", create["twiml"])
    assert create["async_amd"] == "true"
    assert (
        adapter.resume_after_amd(
            CALL_SID,
            answered_by="human",
            target=TARGET,
        )
        == "connected"
    )
    assert "<Stream" in cast("str", client.calls.updates[-1][1]["twiml"])
    assert (
        adapter.resume_after_amd(
            CALL_SID,
            answered_by="machine_start",
            target=TARGET,
        )
        == "hung_up"
    )
    assert client.calls.updates[-1][1]["status"] == "completed"


def test_dtmf_transfer_hangup_and_recording_event_mapping(
    adapter_bundle: tuple[TwilioAdapter, FakeClient, TelephonyLedger],
) -> None:
    adapter, client, _ = adapter_bundle

    adapter.send_dtmf(CALL_SID, "12#")
    adapter.cold_transfer(CALL_SID, "+14155550122")
    adapter.hangup(CALL_SID)
    recording = adapter.parse_event(
        TelephonyRequest(
            scheme="https",
            host="voice.example.test",
            path="/twilio/recordings",
            headers={},
            form={
                "CallSid": CALL_SID,
                "RecordingStatus": "completed",
                "RecordingSid": RECORDING_SID,
                "RecordingUrl": "https://api.twilio.com/private",
            },
        )
    )
    dtmf = adapter.parse_event(
        TelephonyRequest(
            scheme="https",
            host="voice.example.test",
            path="/twilio/events",
            headers={},
            form={"CallSid": CALL_SID, "Digits": "5"},
        )
    )

    assert "<Play" in cast("str", client.calls.updates[0][1]["twiml"])
    assert "<Dial" in cast("str", client.calls.updates[1][1]["twiml"])
    assert client.calls.updates[2][1]["status"] == "completed"
    assert recording.type == "recording_ready"
    assert recording.recording_sid == RECORDING_SID
    assert dtmf.type == "dtmf"
    assert dtmf.digits == "5"


def test_callback_variants_request_rejection_and_call_update_failure(
    adapter_bundle: tuple[TwilioAdapter, FakeClient, TelephonyLedger],
) -> None:
    adapter, client, _ = adapter_bundle
    statuses = {
        "queued": "initiated",
        "initiated": "initiated",
        "ringing": "ringing",
        "answered": "answered",
        "in-progress": "answered",
    }
    for status, expected in statuses.items():
        event = adapter.parse_event(
            TelephonyRequest(
                scheme="https",
                host="voice.example.test",
                path="/twilio/events",
                headers={},
                form={"CallSid": CALL_SID, "CallStatus": status},
            )
        )
        assert event.type == expected

    amd = adapter.parse_event(
        TelephonyRequest(
            scheme="https",
            host="voice.example.test",
            path="/twilio/amd",
            headers={},
            form={"CallSid": CALL_SID, "AnsweredBy": "human"},
        )
    )
    recording_failed = adapter.parse_event(
        TelephonyRequest(
            scheme="https",
            host="voice.example.test",
            path="/twilio/recording",
            headers={},
            form={"CallSid": CALL_SID, "RecordingStatus": "absent"},
        )
    )
    assert amd.type == "amd"
    assert recording_failed.type == "recording_failed"

    for form in (
        {},
        {"CallSid": "invalid", "CallStatus": "ringing"},
        {"CallSid": CALL_SID, "CallStatus": "new-status"},
    ):
        with pytest.raises(VoiceyError) as invalid:
            adapter.parse_event(
                TelephonyRequest(
                    scheme="https",
                    host="voice.example.test",
                    path="/twilio/events",
                    headers={},
                    form=form,
                )
            )
        assert invalid.value.code == "VY-TEL-008"

    no_signature = TelephonyRequest(
        scheme="https",
        host="voice.example.test",
        path="/twilio/events",
        headers={},
    )
    assert not adapter.verify_request(no_signature)
    adapter.expected_public_base = None
    assert not adapter.verify_request(no_signature)

    client.calls.update_error = ConnectionError("down")
    with pytest.raises(VoiceyError) as update:
        adapter.hangup(CALL_SID)
    assert update.value.code == "VY-TEL-011"


@pytest.mark.asyncio
async def test_recording_download_uses_basic_auth_and_engine_artifact_store(
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

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        ledger = TelephonyLedger(tmp_path / "telephony.sqlite3")
        adapter = TwilioAdapter(
            account_sid=ACCOUNT_SID,
            auth_token=AUTH_TOKEN,
            client=FakeClient(),
            ledger=ledger,
            recording_client=http,
        )
        artifacts = MemoryArtifacts()
        key = await adapter.download_recording(
            RECORDING_SID,
            artifact_store=artifacts,
            storage_key="recordings/rec_1.mp3",
        )
        ledger.close()

    expected_auth = base64.b64encode(f"{ACCOUNT_SID}:{AUTH_TOKEN}".encode()).decode()
    assert requests[0].headers["authorization"] == f"Basic {expected_auth}"
    assert requests[0].url.host == "api.twilio.com"
    assert key == "recordings/rec_1.mp3"
    assert artifacts.values[key] == b"recording-bytes"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "headers", "max_bytes"),
    [
        (200, {"content-type": "text/html"}, 100),
        (
            200,
            {"content-type": "audio/mpeg", "content-length": "1000"},
            10,
        ),
        (503, {"content-type": "audio/mpeg"}, 100),
    ],
)
async def test_recording_download_failures_are_cataloged(
    tmp_path: Path,
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

    class MemoryArtifacts:
        async def put(self, storage_key: str, content: bytes) -> None:
            del storage_key, content

        async def read(self, storage_key: str) -> bytes:
            del storage_key
            return b""

        async def delete(self, storage_key: str) -> None:
            del storage_key

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        ledger = TelephonyLedger(tmp_path / f"telephony-{status}-{max_bytes}.sqlite3")
        adapter = TwilioAdapter(
            account_sid=ACCOUNT_SID,
            auth_token=AUTH_TOKEN,
            client=FakeClient(),
            ledger=ledger,
            recording_client=http,
        )
        with pytest.raises(VoiceyError) as caught:
            await adapter.download_recording(
                RECORDING_SID,
                artifact_store=MemoryArtifacts(),
                storage_key="recordings/rec.mp3",
                max_bytes=max_bytes,
            )
        ledger.close()

    assert caught.value.code == "VY-TEL-009"


@pytest.mark.asyncio
async def test_invalid_recording_download_arguments_are_cataloged(
    adapter_bundle: tuple[TwilioAdapter, FakeClient, TelephonyLedger],
) -> None:
    adapter, _, _ = adapter_bundle

    class MemoryArtifacts:
        async def put(self, storage_key: str, content: bytes) -> None:
            del storage_key, content

        async def read(self, storage_key: str) -> bytes:
            del storage_key
            return b""

        async def delete(self, storage_key: str) -> None:
            del storage_key

    for sid, limit in (("invalid", 1), (RECORDING_SID, 0)):
        with pytest.raises(VoiceyError) as caught:
            await adapter.download_recording(
                sid,
                artifact_store=MemoryArtifacts(),
                storage_key="recordings/rec.mp3",
                max_bytes=limit,
            )
        assert caught.value.code == "VY-TEL-009"
