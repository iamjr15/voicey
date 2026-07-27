# pyright: reportPrivateUsage=false

"""Offline beta certification of the Plivo Voice API and media-stream path."""

from __future__ import annotations

import asyncio
import importlib
import json
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any, cast

import httpx
import pytest

import voicekit.telephony.plivo.adapter as plivo_module
from voicekit.errors import VoicekitError
from voicekit.storage.artifacts import LocalArtifactStore
from voicekit.telephony import (
    LiveKitTarget,
    PipecatTarget,
    RollbackToken,
    TelephonyAdapter,
    TelephonyRequest,
)
from voicekit.telephony.ledger import TelephonyLedger
from voicekit.telephony.plivo import PlivoAdapter

AUTH_ID = "MA000000000000000000"
AUTH_TOKEN = "not-a-real-plivo-token"  # pragma: allowlist secret
NUMBER = "+14155550100"
CALL_ID = "call-1234"
TARGET = PipecatTarget(
    "https://voice.example.test",
    ws_path="/plivo/media",
    answer_path="/plivo/answer",
    event_path="/plivo/events",
    recording_path="/plivo/recordings",
    amd_path="/plivo/amd",
)


class FakePlivo:
    def __init__(self) -> None:
        self.number: dict[str, object] = {
            "number": NUMBER.removeprefix("+"),
            "country_iso": "US",
            "region": "California",
            "services": ["voice"],
            "app_id": "old-app",
        }
        self.apps: list[dict[str, object]] = []
        self.calls: list[tuple[str, str, dict[str, object] | None]] = []
        self.failures: dict[tuple[str, str], int] = {}
        self.apply_route_mutations = True
        self.next_call_id: object = [CALL_ID]

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        body: dict[str, object] | None = None
        if request.content:
            loaded: object = json.loads(request.content)
            body = cast("dict[str, object]", loaded)
        self.calls.append((request.method, path, body))
        failure = self.failures.get((request.method, path))
        if failure is not None:
            return httpx.Response(failure, json={"error": "private"})
        prefix = f"/v1/Account/{AUTH_ID}"
        if request.method == "GET" and path == f"{prefix}/":
            return httpx.Response(
                200,
                json={"account_status": "active", "account_type": "standard", "cash_credits": "9"},
            )
        if request.method == "GET" and path == f"{prefix}/Number/":
            return httpx.Response(200, json={"objects": [self.number], "meta": {"next": None}})
        if request.method == "GET" and path == f"{prefix}/Number/{NUMBER[1:]}/":
            return httpx.Response(200, json=self.number)
        if request.method == "GET" and path == f"{prefix}/PhoneNumber/":
            return httpx.Response(
                200,
                json={
                    "objects": [
                        {
                            "number": "14155550199",
                            "country_iso": "US",
                            "region": "California",
                            "city": "San Francisco",
                        }
                    ],
                    "meta": {"next": None},
                },
            )
        if request.method == "POST" and path == f"{prefix}/PhoneNumber/14155550199/":
            return httpx.Response(201, json={"number": "14155550199", "message": "created"})
        if request.method == "DELETE" and path == f"{prefix}/Number/{NUMBER[1:]}/":
            return httpx.Response(204)
        if request.method == "GET" and path == f"{prefix}/Application/":
            return httpx.Response(200, json={"objects": self.apps, "meta": {"next": None}})
        if request.method == "POST" and path == f"{prefix}/Application/":
            assert body is not None
            app = {**body, "app_id": "managed-app"}
            self.apps.append(app)
            return httpx.Response(201, json={"app_id": "managed-app"})
        if request.method == "DELETE" and path == f"{prefix}/Application/managed-app/":
            self.apps.clear()
            return httpx.Response(204)
        if request.method == "POST" and path == f"{prefix}/Number/{NUMBER[1:]}/":
            assert body is not None
            if self.apply_route_mutations:
                self.number["app_id"] = body["app_id"]
            return httpx.Response(202, json={"message": "changed"})
        if request.method == "POST" and path == f"{prefix}/Call/":
            return httpx.Response(202, json={"request_uuid": self.next_call_id})
        if request.method == "POST" and path == f"{prefix}/Call/{CALL_ID}/Record/":
            return httpx.Response(200, json={"recording_id": "recording-1"})
        if request.method == "POST" and path == f"{prefix}/Call/{CALL_ID}/DTMF/":
            return httpx.Response(202, json={"message": "sent"})
        if request.method == "POST" and path == f"{prefix}/Call/{CALL_ID}/":
            return httpx.Response(202, json={"message": "transferred"})
        if request.method == "DELETE" and path == f"{prefix}/Call/{CALL_ID}/":
            return httpx.Response(204)
        return httpx.Response(404, json={"error": "not_found"})


@pytest.fixture
def adapter_bundle(
    tmp_path: Path,
) -> Iterator[tuple[PlivoAdapter, FakePlivo, TelephonyLedger]]:
    fake = FakePlivo()
    client = httpx.Client(
        base_url="https://api.plivo.com",
        transport=httpx.MockTransport(fake.handler),
    )
    ledger = TelephonyLedger(tmp_path / "plivo.sqlite3")
    adapter = PlivoAdapter(
        auth_id=AUTH_ID,
        auth_token=AUTH_TOKEN,
        ledger=ledger,
        client=client,
        expected_public_base="https://voice.example.test",
        clock=lambda: 1000.0,
    )
    try:
        yield adapter, fake, ledger
    finally:
        client.close()
        ledger.close()


def _signed_request(
    *,
    path: str = "/plivo/events",
    form: dict[str, str] | None = None,
    nonce: str = "1700000000000",
) -> TelephonyRequest:
    signature_v3 = importlib.import_module("plivo.utils.signature_v3")
    url = f"https://voice.example.test{path}"
    base_url = signature_v3.construct_post_url(url, dict(form or {}))
    signature = signature_v3.get_signature_v3(
        AUTH_TOKEN.encode(),
        base_url,
        nonce.encode(),
    ).decode()
    return TelephonyRequest(
        scheme="https",
        host="voice.example.test",
        path=path,
        headers={
            "X-Plivo-Signature-V3": signature,
            "X-Plivo-Signature-V3-Nonce": nonce,
        },
        form=form,
    )


def test_account_numbers_purchase_release_and_protocol(
    adapter_bundle: tuple[PlivoAdapter, FakePlivo, TelephonyLedger],
) -> None:
    adapter, fake, _ = adapter_bundle
    assert isinstance(adapter, TelephonyAdapter)
    assert adapter.capabilities.livekit_sip
    assert adapter.capabilities.dtmf_receive
    assert not adapter.capabilities.native_outbound_idempotency
    assert adapter.account_state().balance == "9"
    assert adapter.list_numbers()[0].number == NUMBER
    assert adapter.buy_number("us", "415").number == "+14155550199"
    adapter.release_number(NUMBER)
    assert ("DELETE", f"/v1/Account/{AUTH_ID}/Number/{NUMBER[1:]}/", None) in fake.calls


def test_route_snapshot_confirm_restore_and_delete_managed_application(
    adapter_bundle: tuple[PlivoAdapter, FakePlivo, TelephonyLedger],
) -> None:
    adapter, fake, ledger = adapter_bundle
    token = adapter.point_inbound(NUMBER, TARGET)
    assert ledger.get_route(token.token).state == "applied"
    assert fake.number["app_id"] == "managed-app"
    adapter.restore(token)
    assert ledger.get_route(token.token).state == "restored"
    assert fake.number["app_id"] == "old-app"
    assert fake.apps == []
    adapter.restore(token)


def test_route_ambiguity_and_conflict_are_fenced(
    adapter_bundle: tuple[PlivoAdapter, FakePlivo, TelephonyLedger],
) -> None:
    adapter, fake, ledger = adapter_bundle
    route_path = f"/v1/Account/{AUTH_ID}/Number/{NUMBER[1:]}/"
    fake.failures[("POST", route_path)] = 503
    with pytest.raises(VoicekitError, match="VK-TEL-006"):
        adapter.point_inbound(NUMBER, TARGET)
    assert ledger.open_routes(provider="plivo")[0].state == "ambiguous"

    fake.failures.clear()
    fake.apps.clear()
    token = adapter.point_inbound(NUMBER, TARGET)
    fake.number["app_id"] = "human-change"
    with pytest.raises(VoicekitError, match="VK-TEL-006"):
        adapter.restore(token)
    assert ledger.get_route(token.token).state == "conflict"


def test_outbound_intent_controls_and_recording_use_installed_contract(
    adapter_bundle: tuple[PlivoAdapter, FakePlivo, TelephonyLedger],
) -> None:
    adapter, fake, ledger = adapter_bundle
    call_id = adapter.start_call(
        NUMBER,
        "+14155550101",
        TARGET,
        intent_id="intent_plivo_cert",
        amd=True,
        send_digits="1w2#",
        record=True,
    )
    assert call_id == CALL_ID
    assert ledger.get_intent("intent_plivo_cert").state == "submitted"
    create = next(
        call for call in fake.calls if call[:2] == ("POST", f"/v1/Account/{AUTH_ID}/Call/")
    )
    assert create[2] == {
        "from": NUMBER,
        "to": "+14155550101",
        "answer_url": "https://voice.example.test/plivo/answer/intent_plivo_cert",
        "answer_method": "POST",
        "ring_url": "https://voice.example.test/plivo/events/intent_plivo_cert",
        "ring_method": "POST",
        "hangup_url": "https://voice.example.test/plivo/events/intent_plivo_cert",
        "hangup_method": "POST",
        "ring_timeout": 30,
        "send_digits": "1w2#",
        "machine_detection": "true",
        "machine_detection_url": "https://voice.example.test/plivo/amd/intent_plivo_cert",
        "machine_detection_method": "POST",
    }
    assert adapter.start_recording(call_id, TARGET) == "recording-1"
    adapter.send_dtmf(call_id, "12#")
    adapter.cold_transfer(call_id, "+14155550102")
    adapter.hangup(call_id)


def test_installed_sdk_v3_signature_form_canonicalization_replay_and_negatives(
    adapter_bundle: tuple[PlivoAdapter, FakePlivo, TelephonyLedger],
) -> None:
    adapter, _, _ = adapter_bundle
    request = _signed_request(form={"To": NUMBER, "CallUUID": CALL_ID})
    assert adapter.verify_request(request)
    assert not adapter.verify_request(request)
    assert not adapter.verify_request(
        TelephonyRequest(
            scheme="https",
            host="attacker.example",
            path="/plivo/events",
            headers=request.headers,
            form=request.form,
        )
    )
    tampered = _signed_request(nonce="1700000000001", form={"To": NUMBER})
    tampered.headers["X-Plivo-Signature-V3"] = "invalid"
    assert not adapter.verify_request(tampered)


def test_callback_parsing_binds_intent_and_maps_terminal_amd_recording(
    adapter_bundle: tuple[PlivoAdapter, FakePlivo, TelephonyLedger],
) -> None:
    adapter, _, ledger = adapter_bundle
    adapter.start_call(NUMBER, "+14155550101", TARGET, intent_id="intent_callback")
    ringing = adapter.parse_event(
        TelephonyRequest(
            scheme="https",
            host="voice.example.test",
            path="/plivo/events/intent_callback",
            headers={},
            form={
                "Event": "Ring",
                "CallStatus": "ringing",
                "CallUUID": CALL_ID,
                "Direction": "outbound",
                "From": NUMBER,
                "To": "+14155550101",
            },
            route_params={"intent_id": "intent_callback"},
        )
    )
    assert ringing.type == "ringing"
    assert ledger.get_intent("intent_callback").last_status == "Ring"
    amd = adapter.parse_event(
        TelephonyRequest(
            scheme="https",
            host="voice.example.test",
            path="/plivo/amd",
            headers={},
            form={
                "Event": "MachineDetection",
                "CallStatus": "in-progress",
                "CallUUID": CALL_ID,
                "Machine": "true",
            },
        )
    )
    assert amd.answered_by == "machine"
    recording = adapter.parse_event(
        TelephonyRequest(
            scheme="https",
            host="voice.example.test",
            path="/plivo/recordings",
            headers={},
            form={
                "CallUUID": CALL_ID,
                "recording_id": "recording-1",
                "record_url": "https://media.plivo.com/call.mp3",
            },
        )
    )
    assert recording.type == "recording_ready"
    terminal = adapter.parse_event(
        TelephonyRequest(
            scheme="https",
            host="voice.example.test",
            path="/plivo/events",
            headers={},
            form={
                "Event": "Hangup",
                "CallStatus": "completed",
                "CallUUID": CALL_ID,
                "HangupCause": "NORMAL_CLEARING",
            },
        )
    )
    assert terminal.type == "completed"
    assert terminal.ended_reason == "provider_hangup"


def test_plivoxml_is_bidirectional_pcmu_and_transfer_is_native() -> None:
    adapter = PlivoAdapter(
        auth_id=AUTH_ID,
        auth_token=AUTH_TOKEN,
        ledger=cast("TelephonyLedger", object()),
        client=cast("httpx.Client", object()),
        expected_public_base="https://voice.example.test",
    )
    answer = adapter.answer_response(TARGET)
    assert '<Stream bidirectional="true"' in answer
    assert 'contentType="audio/x-mulaw;rate=8000"' in answer
    assert 'statusCallbackMethod="POST"' in answer
    assert ">wss://voice.example.test/plivo/media</Stream>" in answer
    transfer = adapter.transfer_response("+14155550102", caller_id=NUMBER)
    assert 'callerId="+14155550100"' in transfer


async def test_recording_download_is_authenticated_bounded_and_engine_owned(
    tmp_path: Path,
) -> None:
    seen_auth: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_auth.append(request.headers.get("authorization", ""))
        return httpx.Response(200, content=b"audio", headers={"content-type": "audio/mpeg"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = PlivoAdapter(
            auth_id=AUTH_ID,
            auth_token=AUTH_TOKEN,
            ledger=cast("TelephonyLedger", object()),
            client=cast("httpx.Client", object()),
            recording_client=client,
        )
        store = LocalArtifactStore(tmp_path / "artifacts")
        key = await adapter.download_recording(
            "https://media.plivo.com/call.mp3",
            artifact_store=store,
            storage_key="recordings/call.mp3",
        )
    assert key == "recordings/call.mp3"
    assert await store.read(key) == b"audio"
    # An injected client owns its auth policy; the production-created client uses Basic auth.
    assert seen_auth == [""]


@pytest.mark.parametrize(
    "values",
    [
        {"auth_id": "bad"},
        {"auth_token": ""},
        {"base_url": "http://api.plivo.com"},
        {"replay_ttl_s": 59},
        {"expected_public_base": "https://voice.example.test/path"},
    ],
)
def test_adapter_config_and_target_validation_fail_closed(values: dict[str, object]) -> None:
    defaults: dict[str, object] = {
        "auth_id": AUTH_ID,
        "auth_token": AUTH_TOKEN,
        "ledger": cast("TelephonyLedger", object()),
        "client": cast("httpx.Client", object()),
        "expected_public_base": "https://voice.example.test",
    }
    with pytest.raises(VoicekitError, match="VK-TEL-002"):
        PlivoAdapter(**cast("Any", {**defaults, **values}))


def test_invalid_inputs_ambiguous_create_and_livekit_target_fail_closed(
    adapter_bundle: tuple[PlivoAdapter, FakePlivo, TelephonyLedger],
) -> None:
    adapter, fake, ledger = adapter_bundle
    with pytest.raises(VoicekitError, match="country"):
        adapter.buy_number("usa")
    with pytest.raises(VoicekitError, match="prefix"):
        adapter.buy_number("us", "bad")
    with pytest.raises(VoicekitError, match="LiveKit targets"):
        adapter.point_inbound(
            NUMBER,
            LiveKitTarget(project="project", sip_uri="sip:project.sip.livekit.cloud"),
        )
    fake.next_call_id = []
    with pytest.raises(VoicekitError, match="VK-TEL-007"):
        adapter.start_call(NUMBER, "+14155550101", TARGET, intent_id="intent_ambiguous")
    assert ledger.get_intent("intent_ambiguous").state == "ambiguous"


def test_nonconfirmation_open_route_recovery_and_wrong_token_are_safe(
    adapter_bundle: tuple[PlivoAdapter, FakePlivo, TelephonyLedger],
) -> None:
    adapter, fake, ledger = adapter_bundle
    fake.apply_route_mutations = False
    with pytest.raises(VoicekitError, match="VK-TEL-006"):
        adapter.point_inbound(NUMBER, TARGET)
    assert ledger.open_routes(provider="plivo")[-1].state == "ambiguous"
    fake.apply_route_mutations = True
    adapter.restore_open_routes()
    assert adapter.restore_open_routes() == 0
    with pytest.raises(VoicekitError, match="another carrier"):
        adapter.restore(RollbackToken(provider="twilio", token="route_missing"))


def test_recording_download_rejects_bad_url_type_and_size(tmp_path: Path) -> None:
    async def run() -> None:
        async def response_handler(request: httpx.Request) -> httpx.Response:
            del request
            return httpx.Response(200, content=b"x" * 6, headers={"content-type": "text/plain"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(response_handler)) as client:
            adapter = PlivoAdapter(
                auth_id=AUTH_ID,
                auth_token=AUTH_TOKEN,
                ledger=cast("TelephonyLedger", object()),
                client=cast("httpx.Client", object()),
                recording_client=client,
            )
            store = LocalArtifactStore(tmp_path / "artifacts")
            with pytest.raises(VoicekitError, match="safe HTTPS"):
                await adapter.download_recording(
                    "http://media.plivo.com/call.mp3",
                    artifact_store=store,
                    storage_key="bad",
                )
            with pytest.raises(VoicekitError, match="unexpected content"):
                await adapter.download_recording(
                    "https://media.plivo.com/call.mp3",
                    artifact_store=store,
                    storage_key="bad",
                    max_bytes=10,
                )

    asyncio.run(run())


def test_callback_variants_and_malformed_payloads_fail_closed(
    adapter_bundle: tuple[PlivoAdapter, FakePlivo, TelephonyLedger],
) -> None:
    adapter, _, _ = adapter_bundle

    human = adapter.parse_event(
        TelephonyRequest(
            scheme="https",
            host="voice.example.test",
            path="/plivo/amd",
            headers={},
            form={
                "RequestUUID": CALL_ID,
                "Event": "MachineDetection",
                "Machine": "false",
                "Direction": "incoming",
            },
        )
    )
    dtmf = adapter.parse_event(
        TelephonyRequest(
            scheme="https",
            host="voice.example.test",
            path="/plivo/events",
            headers={},
            form={
                "CallUUID": CALL_ID,
                "Digits": "*9#",
                "Direction": "outgoing",
            },
        )
    )
    answered = adapter.parse_event(
        TelephonyRequest(
            scheme="https",
            host="voice.example.test",
            path="/plivo/events",
            headers={},
            form={"CallUUID": CALL_ID, "Event": "StartApp"},
        )
    )
    initiated = adapter.parse_event(
        TelephonyRequest(
            scheme="https",
            host="voice.example.test",
            path="/plivo/events",
            headers={},
            form={"CallUUID": CALL_ID, "CallStatus": "queued"},
        )
    )
    failed = adapter.parse_event(
        TelephonyRequest(
            scheme="https",
            host="voice.example.test",
            path="/plivo/events",
            headers={},
            form={
                "CallUUID": CALL_ID,
                "Event": "Hangup",
                "CallStatus": "failed",
                "HangupCauseCode": "17",
            },
        )
    )

    assert human.answered_by == "human"
    assert human.direction == "inbound"
    assert dtmf.type == "dtmf"
    assert dtmf.digits == "*9#"
    assert dtmf.direction == "outbound"
    assert answered.type == "answered"
    assert initiated.type == "initiated"
    assert failed.type == "failed"
    assert failed.ended_reason == "carrier_error"
    assert "callerId" not in adapter.transfer_response("+14155550102")

    invalid_forms: tuple[dict[str, object], ...] = (
        {"CallUUID": CALL_ID, "recording_id": "recording-only"},
        {"CallUUID": CALL_ID, "Direction": "sideways"},
        {"CallUUID": CALL_ID, "Event": "unknown", "CallStatus": "mystery"},
        {"Event": "Ring"},
        {"CallUUID": []},
        {"CallUUID": CALL_ID, "Digits": "A"},
    )
    for form in invalid_forms:
        with pytest.raises(VoicekitError, match=r"VK-TEL-00[28]"):
            adapter.parse_event(
                TelephonyRequest(
                    scheme="https",
                    host="voice.example.test",
                    path="/plivo/events",
                    headers={},
                    form=form,
                )
            )


def test_signature_transport_origin_and_sdk_exception_edges(
    adapter_bundle: tuple[PlivoAdapter, FakePlivo, TelephonyLedger],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter, _, _ = adapter_bundle
    signed = _signed_request(nonce="signature-edges")

    assert not adapter.verify_request(
        TelephonyRequest(
            scheme="http",
            host=signed.host,
            path=signed.path,
            headers=signed.headers,
            form=signed.form,
        )
    )
    assert not adapter.verify_request(
        TelephonyRequest(
            scheme="https",
            host=signed.host,
            path=signed.path,
            headers=signed.headers,
            form=signed.form,
            is_websocket=True,
        )
    )
    assert not adapter.verify_request(
        TelephonyRequest("https", signed.host, signed.path, {}, form=signed.form)
    )
    assert not adapter.verify_request(
        TelephonyRequest(
            "https",
            signed.host,
            signed.path,
            {
                "X-Plivo-Signature-V3": "value",
                "X-Plivo-Signature-V3-Nonce": "contains space",
            },
        )
    )
    assert not adapter.verify_request(
        TelephonyRequest(
            "https",
            "user@voice.example.test",
            signed.path,
            signed.headers,
            form=signed.form,
        )
    )
    assert not adapter.verify_request(
        TelephonyRequest(
            "https",
            signed.host,
            "relative",
            signed.headers,
            form=signed.form,
        )
    )

    def sdk_error(
        _method: str,
        _url: str,
        _nonce: str,
        _token: str,
        _signature: str,
        _params: dict[str, str],
    ) -> bool:
        raise TypeError("sdk mismatch")

    monkeypatch.setattr(plivo_module, "_validate_v3", sdk_error)
    assert not adapter.verify_request(signed)

    dynamic = PlivoAdapter(
        auth_id=AUTH_ID,
        auth_token=AUTH_TOKEN,
        ledger=cast("TelephonyLedger", object()),
        client=cast("httpx.Client", object()),
        expected_public_base=None,
        clock=lambda: 1000,
    )

    def sdk_valid(
        _method: str,
        _url: str,
        _nonce: str,
        _token: str,
        _signature: str,
        _params: dict[str, str],
    ) -> bool:
        return True

    monkeypatch.setattr(plivo_module, "_validate_v3", sdk_valid)
    assert dynamic.verify_request(signed)
    assert dynamic._claim_nonce("old")
    object.__setattr__(dynamic, "_clock", lambda: 2000)
    assert dynamic._claim_nonce("old")


def test_managed_application_adoption_drift_duplicate_and_pagination(
    adapter_bundle: tuple[PlivoAdapter, FakePlivo, TelephonyLedger],
    tmp_path: Path,
) -> None:
    adapter, fake, _ = adapter_bundle
    app_id, created = adapter._ensure_application(TARGET)
    adopted_id, adopted = adapter._ensure_application(TARGET)
    assert (app_id, created) == ("managed-app", True)
    assert (adopted_id, adopted) == ("managed-app", False)

    fake.apps[0]["answer_method"] = "GET"
    with pytest.raises(VoicekitError, match="differs"):
        adapter._ensure_application(TARGET)
    fake.apps[0]["answer_method"] = "POST"
    fake.apps.append(dict(fake.apps[0]))
    with pytest.raises(VoicekitError, match="duplicate"):
        adapter._ensure_application(TARGET)

    pages: list[str] = []

    def paged(request: httpx.Request) -> httpx.Response:
        offset = request.url.params.get("offset", "")
        pages.append(offset)
        item = {
            "number": "14155550100" if offset == "0" else "14155550101",
            "voice_enabled": True,
            "alias": "managed",
        }
        return httpx.Response(
            200,
            json={
                "objects": [item],
                "meta": {"next": "/next" if offset == "0" else None},
            },
        )

    client = httpx.Client(
        base_url="https://api.plivo.com",
        transport=httpx.MockTransport(paged),
    )
    ledger = TelephonyLedger(tmp_path / "pagination.sqlite3")
    paged_adapter = PlivoAdapter(
        auth_id=AUTH_ID,
        auth_token=AUTH_TOKEN,
        ledger=ledger,
        client=client,
    )
    try:
        numbers = paged_adapter.list_numbers()
        assert [number.number for number in numbers] == [
            "+14155550100",
            "+14155550101",
        ]
        assert numbers[0].capabilities == frozenset({"voice"})
        assert pages == ["0", "1"]
    finally:
        client.close()
        ledger.close()


def test_definitive_route_and_restore_nonconfirmation_are_fenced(
    adapter_bundle: tuple[PlivoAdapter, FakePlivo, TelephonyLedger],
) -> None:
    adapter, fake, ledger = adapter_bundle
    route_path = f"/v1/Account/{AUTH_ID}/Number/{NUMBER[1:]}/"
    fake.failures[("POST", route_path)] = 400
    with pytest.raises(VoicekitError) as definitive:
        adapter.point_inbound(NUMBER, TARGET)
    assert definitive.value.code == "VK-TEL-004"
    assert ledger.open_routes(provider="plivo") == ()
    failed = ledger.prepare_route(
        provider="plivo",
        number=NUMBER,
        number_sid=NUMBER[1:],
        snapshot={"app_id": "old-app"},
        applied={"app_id": "managed-app"},
    )
    ledger.transition_route(failed.token, expected=("prepared",), state="failed")
    with pytest.raises(VoicekitError, match="cannot restore"):
        adapter.restore(RollbackToken(provider="plivo", token=failed.token))

    fake.failures.clear()
    fake.apps.clear()
    token = adapter.point_inbound(NUMBER, TARGET)
    fake.apply_route_mutations = False
    with pytest.raises(VoicekitError, match="restore conflicted"):
        adapter.restore(token)
    assert ledger.get_route(token.token).state == "conflict"


def test_outbound_rejection_uncertainty_reconciliation_and_input_guards(
    adapter_bundle: tuple[PlivoAdapter, FakePlivo, TelephonyLedger],
) -> None:
    adapter, fake, ledger = adapter_bundle
    call_path = f"/v1/Account/{AUTH_ID}/Call/"

    with pytest.raises(VoicekitError, match="timeout"):
        adapter.start_call(NUMBER, "+14155550101", TARGET, timeout_s=4)
    with pytest.raises(VoicekitError, match="DTMF"):
        adapter.start_call(NUMBER, "+14155550101", TARGET, send_digits="A")
    with pytest.raises(VoicekitError, match="expected_public_base"):
        PlivoAdapter(
            auth_id=AUTH_ID,
            auth_token=AUTH_TOKEN,
            ledger=ledger,
            client=cast("httpx.Client", object()),
        ).cold_transfer(CALL_ID, "+14155550102")

    fake.failures[("POST", call_path)] = 400
    with pytest.raises(VoicekitError) as rejected:
        adapter.start_call(
            NUMBER,
            "+14155550101",
            TARGET,
            intent_id="intent_rejected",
        )
    assert rejected.value.code == "VK-TEL-004"
    assert ledger.get_intent("intent_rejected").state == "rejected"

    fake.failures[("POST", call_path)] = 503
    with pytest.raises(VoicekitError) as ambiguous:
        adapter.start_call(
            NUMBER,
            "+14155550101",
            TARGET,
            intent_id="intent_uncertain",
        )
    assert ambiguous.value.code == "VK-TEL-007"
    assert ledger.get_intent("intent_uncertain").state == "ambiguous"
    with pytest.raises(VoicekitError, match="do not retry"):
        adapter.reconcile_outbound("intent_uncertain")

    fake.failures.clear()
    adapter.start_call(
        NUMBER,
        "+14155550101",
        TARGET,
        intent_id="intent_reconciled",
    )
    assert adapter.reconcile_outbound("intent_reconciled").provider_call_id == CALL_ID


@pytest.mark.parametrize(
    ("response", "operation"),
    [
        (httpx.Response(401, json={"error": "denied"}), "http_401"),
        (httpx.Response(503, json={"error": "down"}), "definitive result"),
        (httpx.Response(200, content=b"{"), "invalid JSON"),
        (httpx.Response(200, json=[]), "invalid envelope"),
        (httpx.Response(200, json={"objects": "bad"}), "malformed"),
        (httpx.Response(200, json={"objects": ["bad"]}), "malformed"),
    ],
)
def test_http_and_list_envelope_failures_are_classified(
    tmp_path: Path,
    response: httpx.Response,
    operation: str,
) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return response

    client = httpx.Client(
        base_url="https://api.plivo.com",
        transport=httpx.MockTransport(handler),
    )
    ledger = TelephonyLedger(tmp_path / f"{operation.replace(' ', '-')}.sqlite3")
    adapter = PlivoAdapter(
        auth_id=AUTH_ID,
        auth_token=AUTH_TOKEN,
        ledger=ledger,
        client=client,
    )
    try:
        action = adapter.list_numbers if "malformed" in operation else adapter.account_state
        with pytest.raises(VoicekitError, match=operation):
            action()
    finally:
        client.close()
        ledger.close()


def test_network_ownership_empty_search_and_scalar_failures_are_safe(tmp_path: Path) -> None:
    def network(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline", request=request)

    client = httpx.Client(
        base_url="https://api.plivo.com",
        transport=httpx.MockTransport(network),
    )
    ledger = TelephonyLedger(tmp_path / "network.sqlite3")
    adapter = PlivoAdapter(
        auth_id=AUTH_ID,
        auth_token=AUTH_TOKEN,
        ledger=ledger,
        client=client,
    )
    try:
        with pytest.raises(VoicekitError, match="definitive result"):
            adapter.account_state()
    finally:
        client.close()
        ledger.close()

    assert plivo_module._optional("") is None
    assert plivo_module._optional(3) == "3"
    assert plivo_module._form_dict(["not", "a", "mapping"]) == {}
    with pytest.raises(VoicekitError, match="invalid scalar"):
        plivo_module._optional([])
    fake_adapter, fake, fake_ledger = _one_off_fake(tmp_path / "ownership")
    try:
        fake.number["number"] = "14155550101"
        with pytest.raises(VoicekitError, match="ownership"):
            fake_adapter.inbound_route(NUMBER)
    finally:
        fake_adapter._client.close()
        fake_ledger.close()

    empty_adapter, _empty_fake, empty_ledger = _one_off_fake(tmp_path / "empty")
    try:
        original = empty_adapter._client
        empty_adapter._client = httpx.Client(
            base_url="https://api.plivo.com",
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(
                    200,
                    json={"objects": [], "meta": {"next": None}},
                )
            ),
        )
        original.close()
        with pytest.raises(VoicekitError, match="no Plivo number"):
            empty_adapter.buy_number("US")
    finally:
        empty_adapter._client.close()
        empty_ledger.close()


def _one_off_fake(path: Path) -> tuple[PlivoAdapter, FakePlivo, TelephonyLedger]:
    path.mkdir(parents=True, exist_ok=True)
    fake = FakePlivo()
    client = httpx.Client(
        base_url="https://api.plivo.com",
        transport=httpx.MockTransport(fake.handler),
    )
    ledger = TelephonyLedger(path / "plivo.sqlite3")
    return (
        PlivoAdapter(
            auth_id=AUTH_ID,
            auth_token=AUTH_TOKEN,
            ledger=ledger,
            client=client,
            expected_public_base="https://voice.example.test",
        ),
        fake,
        ledger,
    )


async def test_recording_download_declared_stream_and_transport_failures(
    tmp_path: Path,
) -> None:
    class ChunkStream(httpx.AsyncByteStream):
        def __aiter__(self) -> AsyncIterator[bytes]:
            async def chunks() -> AsyncIterator[bytes]:
                yield b"123"
                yield b"456"

            return chunks()

    responses = [
        httpx.Response(
            200,
            content=b"audio",
            headers={"content-type": "audio/mpeg", "content-length": "99"},
        ),
        httpx.Response(
            200,
            stream=ChunkStream(),
            headers={"content-type": "audio/mpeg"},
        ),
        httpx.Response(
            200,
            content=b"audio",
            headers={"content-type": "audio/mpeg", "content-length": "bad"},
        ),
        httpx.Response(503),
    ]

    async def handler(_request: httpx.Request) -> httpx.Response:
        return responses.pop(0)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = PlivoAdapter(
            auth_id=AUTH_ID,
            auth_token=AUTH_TOKEN,
            ledger=cast("TelephonyLedger", object()),
            client=cast("httpx.Client", object()),
            recording_client=client,
        )
        store = LocalArtifactStore(tmp_path / "artifacts")
        with pytest.raises(VoicekitError, match="positive"):
            await adapter.download_recording(
                "https://media.plivo.com/call.mp3",
                artifact_store=store,
                storage_key="recordings/zero.mp3",
                max_bytes=0,
            )
        for storage_key in ("declared", "stream", "length", "status"):
            with pytest.raises(VoicekitError, match="recording"):
                await adapter.download_recording(
                    "https://media.plivo.com/call.mp3",
                    artifact_store=store,
                    storage_key=f"recordings/{storage_key}.mp3",
                    max_bytes=5,
                )
