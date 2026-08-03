"""Offline certification of the Vobiz Voice API and VobizXML path."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import httpx
import pytest

from voicey.errors import VoiceyError
from voicey.storage.artifacts import LocalArtifactStore
from voicey.telephony import (
    LiveKitTarget,
    PipecatTarget,
    RollbackToken,
    TelephonyAdapter,
    TelephonyRequest,
)
from voicey.telephony.ledger import TelephonyLedger
from voicey.telephony.vobiz import VobizAdapter

AUTH_ID = "MA_TEST1234"
AUTH_TOKEN = "not-a-real-vobiz-token"  # pragma: allowlist secret
NUMBER = "+918071234567"
CALL_ID = "call-1234"
TARGET = PipecatTarget(
    "https://voice.example.test",
    ws_path="/vobiz/media",
    answer_path="/vobiz/answer",
    event_path="/vobiz/events",
    recording_path="/vobiz/recordings",
    amd_path="/vobiz/amd",
)


class FakeVobiz:
    def __init__(self) -> None:
        self.number: dict[str, object] = {
            "id": "number-1",
            "account_id": AUTH_ID,
            "e164": NUMBER,
            "country": "IN",
            "region": "Karnataka",
            "capabilities": {"voice": True, "sms": False},
            "application_id": "old-app",
            "status": "active",
        }
        self.apps: list[dict[str, object]] = []
        self.calls: list[tuple[str, str, dict[str, object] | None]] = []
        self.failures: dict[tuple[str, str], int] = {}
        self.next_call_id = CALL_ID
        self.inventory: list[dict[str, object]] = [
            {
                "id": "inventory-1",
                "e164": "+918071234599",
                "country": "IN",
                "region": "Karnataka",
            }
        ]
        self.purchase_number: object = {
            "id": "number-purchased",
            "account_id": AUTH_ID,
            "e164": "+918071234599",
            "country": "IN",
            "capabilities": {"voice": True},
        }
        self.apply_route_mutations = True

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
        prefix = f"/api/v1/Account/{AUTH_ID}"
        if request.method == "GET" and path == f"{prefix}/balance/INR":
            return httpx.Response(
                200,
                json={
                    "status": "active",
                    "available_balance": 1000,
                    "currency": "INR",
                },
            )
        if request.method == "GET" and path == f"{prefix}/numbers":
            search = request.url.params.get("search")
            items = [self.number] if not search or search == NUMBER else []
            return httpx.Response(
                200,
                json={"items": items, "page": 1, "per_page": 100, "total": len(items)},
            )
        if request.method == "GET" and path == f"{prefix}/inventory/numbers":
            return httpx.Response(
                200,
                json={
                    "items": self.inventory,
                    "page": 1,
                    "per_page": 100,
                    "total": len(self.inventory),
                },
            )
        if request.method == "POST" and path == f"{prefix}/numbers/purchase-from-inventory":
            assert body is not None
            if isinstance(self.purchase_number, dict):
                self.purchase_number["e164"] = body["e164"]
            return httpx.Response(200, json={"number": self.purchase_number})
        if request.method == "DELETE" and path == f"{prefix}/numbers/+918071234567":
            return httpx.Response(200, json={"status": "pending_release"})
        if request.method == "GET" and path == f"{prefix}/Application/":
            return httpx.Response(200, json={"objects": self.apps})
        if request.method == "POST" and path == f"{prefix}/Application/":
            assert body is not None
            app = {**body, "app_id": "managed-app"}
            self.apps.append(app)
            return httpx.Response(201, json={"app_id": "managed-app", "message": "created"})
        if request.method == "DELETE" and path == f"{prefix}/Application/managed-app/":
            self.apps = [app for app in self.apps if app["app_id"] != "managed-app"]
            return httpx.Response(204)
        route_path = f"{prefix}/numbers/+918071234567"
        if request.method == "POST" and path == f"{route_path}/application":
            assert body is not None
            if self.apply_route_mutations:
                self.number["application_id"] = body["application_id"]
                self.number.pop("trunk_group_id", None)
            return httpx.Response(200, json={"message": "attached"})
        if request.method == "DELETE" and path == f"{route_path}/application":
            if self.apply_route_mutations:
                self.number.pop("application_id", None)
            return httpx.Response(204)
        if request.method == "POST" and path == f"{route_path}/assign":
            assert body is not None
            if self.apply_route_mutations:
                self.number["trunk_group_id"] = body["trunk_group_id"]
                self.number.pop("application_id", None)
            return httpx.Response(204)
        if request.method == "DELETE" and path == f"{route_path}/assign":
            if self.apply_route_mutations:
                self.number.pop("trunk_group_id", None)
            return httpx.Response(204)
        if request.method == "POST" and path == f"{prefix}/Call/":
            return httpx.Response(
                200,
                json={
                    **({"request_uuid": self.next_call_id} if self.next_call_id else {}),
                    "message": "Call fired",
                },
            )
        if request.method == "POST" and path == f"{prefix}/Call/{CALL_ID}/Record/":
            return httpx.Response(200, json={"recording_id": "recording-1"})
        if request.method == "POST" and path == f"{prefix}/Call/{CALL_ID}/DTMF/":
            return httpx.Response(202, json={"message": "digits sent"})
        if request.method == "POST" and path == f"{prefix}/Call/{CALL_ID}/":
            return httpx.Response(200, json={"message": "call transferred"})
        if request.method == "DELETE" and path == f"{prefix}/Call/{CALL_ID}/":
            return httpx.Response(204)
        return httpx.Response(404, json={"error": "not_found"})


@pytest.fixture
def adapter_bundle(
    tmp_path: Path,
) -> Iterator[tuple[VobizAdapter, FakeVobiz, TelephonyLedger]]:
    fake = FakeVobiz()
    client = httpx.Client(
        base_url="https://api.vobiz.ai",
        transport=httpx.MockTransport(fake.handler),
    )
    ledger = TelephonyLedger(tmp_path / "vobiz.sqlite3")
    adapter = VobizAdapter(
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
    path: str = "/vobiz/events",
    form: dict[str, str] | None = None,
    nonce: str = "12345678901234567890",
    version: str = "v3",
) -> TelephonyRequest:
    url = f"https://voice.example.test{path}"
    payload = f"{url}.{nonce}" if version == "v3" else f"{url}{nonce}"
    signature = base64.b64encode(
        hmac.new(AUTH_TOKEN.encode(), payload.encode(), hashlib.sha256).digest()
    ).decode()
    return TelephonyRequest(
        scheme="https",
        host="voice.example.test",
        path=path,
        headers={
            f"x-vobiz-signature-{version}": signature,
            f"x-vobiz-signature-{version}-nonce": nonce,
        },
        form=form,
    )


def test_account_number_purchase_and_recoverable_release(
    adapter_bundle: tuple[VobizAdapter, FakeVobiz, TelephonyLedger],
) -> None:
    adapter, fake, _ = adapter_bundle
    assert isinstance(adapter, TelephonyAdapter)
    assert adapter.capabilities.livekit_sip
    assert adapter.capabilities.dtmf_receive
    assert not adapter.capabilities.native_outbound_idempotency
    assert adapter.account_state().balance == "1000"
    assert adapter.list_numbers()[0].number == NUMBER
    bought = adapter.buy_number("in", "80")
    assert bought.number == "+918071234599"
    adapter.release_number(NUMBER)
    assert (
        "DELETE",
        f"/api/v1/Account/{AUTH_ID}/numbers/+918071234567",
        None,
    ) in fake.calls


def test_snapshot_route_create_confirm_restore_and_delete_managed_app(
    adapter_bundle: tuple[VobizAdapter, FakeVobiz, TelephonyLedger],
) -> None:
    adapter, fake, ledger = adapter_bundle
    token = adapter.point_inbound(NUMBER, TARGET)
    route = ledger.get_route(token.token)
    assert route.state == "applied"
    assert route.snapshot == {"application_id": "old-app", "trunk_group_id": None}
    assert fake.number["application_id"] == "managed-app"
    assert len(fake.apps) == 1

    adapter.restore(token)
    assert ledger.get_route(token.token).state == "restored"
    assert fake.number["application_id"] == "old-app"
    assert fake.apps == []
    adapter.restore(token)


def test_route_conflict_and_ambiguous_provider_outcome_are_fenced(
    adapter_bundle: tuple[VobizAdapter, FakeVobiz, TelephonyLedger],
) -> None:
    adapter, fake, ledger = adapter_bundle
    route_path = f"/api/v1/Account/{AUTH_ID}/numbers/+918071234567/application"
    fake.failures[("POST", route_path)] = 503
    with pytest.raises(VoiceyError, match="VY-TEL-006"):
        adapter.point_inbound(NUMBER, TARGET)
    assert ledger.open_routes(provider="vobiz")[0].state == "ambiguous"

    fake.failures.clear()
    fake.apps.clear()
    token = adapter.point_inbound(NUMBER, TARGET)
    fake.number["application_id"] = "human-change"
    with pytest.raises(VoiceyError, match="VY-TEL-006"):
        adapter.restore(token)
    assert ledger.get_route(token.token).state == "conflict"


def test_outbound_intent_and_call_controls_use_official_paths(
    adapter_bundle: tuple[VobizAdapter, FakeVobiz, TelephonyLedger],
) -> None:
    adapter, fake, ledger = adapter_bundle
    call_id = adapter.start_call(
        NUMBER,
        "+919876543210",
        TARGET,
        intent_id="intent_vobiz_cert",
        amd=True,
        send_digits="1w2#",
        record=True,
    )
    assert call_id == CALL_ID
    intent = ledger.get_intent("intent_vobiz_cert")
    assert intent.state == "submitted"
    create = next(
        call for call in fake.calls if call[:2] == ("POST", f"/api/v1/Account/{AUTH_ID}/Call/")
    )
    assert create[2] == {
        "from": NUMBER,
        "to": "+919876543210",
        "answer_url": "https://voice.example.test/vobiz/answer/intent_vobiz_cert",
        "answer_method": "POST",
        "ring_url": "https://voice.example.test/vobiz/events/intent_vobiz_cert",
        "ring_method": "POST",
        "hangup_url": "https://voice.example.test/vobiz/events/intent_vobiz_cert",
        "hangup_method": "POST",
        "hangup_on_ring": 30,
        "send_digits": "1w2#",
        "machine_detection": "true",
        "machine_detection_url": "https://voice.example.test/vobiz/amd/intent_vobiz_cert",
        "machine_detection_method": "POST",
    }
    assert adapter.start_recording(call_id, TARGET) == "recording-1"
    adapter.send_dtmf(call_id, "12#")
    adapter.cold_transfer(call_id, "+918071230000")
    adapter.hangup(call_id)
    assert any(
        call[:2] == ("POST", f"/api/v1/Account/{AUTH_ID}/Call/{CALL_ID}/DTMF/")
        for call in fake.calls
    )


def test_v3_v2_signature_canonicalization_replay_and_negative_cases(
    adapter_bundle: tuple[VobizAdapter, FakeVobiz, TelephonyLedger],
) -> None:
    adapter, _, _ = adapter_bundle
    request = _signed_request()
    assert adapter.verify_request(request)
    assert not adapter.verify_request(request)
    assert adapter.verify_request(_signed_request(nonce="12345678901234567891", version="v2"))
    assert not adapter.verify_request(
        TelephonyRequest(
            scheme="https",
            host="attacker.example",
            path="/vobiz/events",
            headers=request.headers,
        )
    )
    tampered = _signed_request(nonce="12345678901234567892")
    tampered.headers["x-vobiz-signature-v3"] = base64.b64encode(b"bad").decode()
    assert not adapter.verify_request(tampered)


def test_callback_parsing_binds_intent_and_maps_terminal_amd_and_recording(
    adapter_bundle: tuple[VobizAdapter, FakeVobiz, TelephonyLedger],
) -> None:
    adapter, _, ledger = adapter_bundle
    adapter.start_call(
        NUMBER,
        "+919876543210",
        TARGET,
        intent_id="intent_callback",
    )
    ringing = adapter.parse_event(
        TelephonyRequest(
            scheme="https",
            host="voice.example.test",
            path="/vobiz/events/intent_callback",
            headers={},
            form={
                "Event": "Ring",
                "CallStatus": "ringing",
                "CallUUID": CALL_ID,
                "Direction": "outbound",
                "From": NUMBER,
                "To": "+919876543210",
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
            path="/vobiz/amd",
            headers={},
            form={
                "Event": "MachineDetection",
                "CallStatus": "in-progress",
                "CallUUID": CALL_ID,
                "Machine": "true",
            },
        )
    )
    assert amd.type == "amd"
    assert amd.answered_by == "machine"

    recording = adapter.parse_event(
        TelephonyRequest(
            scheme="https",
            host="voice.example.test",
            path="/vobiz/recordings",
            headers={},
            form={
                "CallUUID": CALL_ID,
                "recording_id": "recording-1",
                "record_url": "https://recordings.vobiz.ai/call.mp3",
            },
        )
    )
    assert recording.type == "recording_ready"
    assert recording.recording_url == "https://recordings.vobiz.ai/call.mp3"

    hangup = adapter.parse_event(
        TelephonyRequest(
            scheme="https",
            host="voice.example.test",
            path="/vobiz/events",
            headers={},
            form={
                "Event": "Hangup",
                "CallStatus": "completed",
                "CallUUID": CALL_ID,
                "HangupCause": "NORMAL_CLEARING",
            },
        )
    )
    assert hangup.type == "completed"
    assert hangup.ended_reason == "provider_hangup"


def test_vobizxml_is_bidirectional_pcmu_and_transfer_is_native() -> None:
    adapter = VobizAdapter(
        auth_id=AUTH_ID,
        auth_token=AUTH_TOKEN,
        ledger=cast("TelephonyLedger", object()),
        client=cast("httpx.Client", object()),
        expected_public_base="https://voice.example.test",
    )
    answer = adapter.answer_response(TARGET)
    assert '<Stream bidirectional="true"' in answer
    assert 'contentType="audio/x-mulaw;rate=8000"' in answer
    assert ">wss://voice.example.test/vobiz/media</Stream>" in answer
    transfer = adapter.transfer_response("+918071230000", caller_id=NUMBER)
    assert 'callerId="+918071234567"' in transfer
    assert "<Number>+918071230000</Number>" in transfer


@pytest.mark.parametrize(
    "values",
    [
        {"auth_id": "bad"},
        {"auth_token": ""},
        {"base_url": "http://api.vobiz.ai"},
        {"currency": "usd"},
        {"replay_ttl_s": 59},
        {"expected_public_base": "https://voice.example.test/path"},
    ],
)
def test_adapter_configuration_rejects_unsafe_values(values: dict[str, object]) -> None:
    defaults: dict[str, object] = {
        "auth_id": AUTH_ID,
        "auth_token": AUTH_TOKEN,
        "ledger": cast("TelephonyLedger", object()),
        "client": cast("httpx.Client", object()),
        "expected_public_base": "https://voice.example.test",
    }
    with pytest.raises(VoiceyError) as caught:
        VobizAdapter(**cast("Any", {**defaults, **values}))
    assert caught.value.code == "VY-TEL-002"


def test_number_and_target_validation_fail_closed(
    adapter_bundle: tuple[VobizAdapter, FakeVobiz, TelephonyLedger],
) -> None:
    adapter, fake, _ = adapter_bundle
    with pytest.raises(VoiceyError, match="country"):
        adapter.buy_number("india")
    with pytest.raises(VoiceyError, match="prefix"):
        adapter.buy_number("in", "not-digits")

    fake.inventory.clear()
    with pytest.raises(VoiceyError) as unavailable:
        adapter.buy_number("in")
    assert unavailable.value.code == "VY-TEL-003"

    fake.inventory.append(
        {
            "id": "inventory-1",
            "e164": "+918071234599",
            "country": "IN",
        }
    )
    fake.purchase_number = "malformed"
    with pytest.raises(VoiceyError) as malformed:
        adapter.buy_number("in")
    assert malformed.value.code == "VY-TEL-011"

    fake.number["account_id"] = "another-account"
    with pytest.raises(VoiceyError) as ownership:
        adapter.inbound_route(NUMBER)
    assert ownership.value.code == "VY-TEL-003"

    with pytest.raises(VoiceyError, match="LiveKit targets"):
        adapter.point_inbound(
            NUMBER,
            LiveKitTarget(
                project="project",
                sip_uri="sip:project.sip.livekit.cloud",
            ),
        )


def _managed_application(*, target: PipecatTarget = TARGET) -> dict[str, object]:
    fingerprint = hashlib.sha256(f"{target.answer_url}|{target.event_url()}".encode()).hexdigest()[
        :20
    ]
    return {
        "app_id": "existing-app",
        "app_name": f"voicey-{fingerprint}",
        "answer_url": target.answer_url,
        "answer_method": "POST",
        "hangup_url": target.event_url(),
        "hangup_method": "POST",
    }


def test_existing_application_is_adopted_but_drift_and_duplicates_are_rejected(
    adapter_bundle: tuple[VobizAdapter, FakeVobiz, TelephonyLedger],
) -> None:
    adapter, fake, _ = adapter_bundle
    fake.apps = [_managed_application()]
    token = adapter.point_inbound(NUMBER, TARGET)
    adapter.restore(token)
    assert fake.apps == [_managed_application()]

    fake.apps[0]["answer_url"] = "https://human.example.test/answer"
    with pytest.raises(VoiceyError, match="differs"):
        adapter.point_inbound(NUMBER, TARGET)
    fake.apps = [_managed_application(), _managed_application()]
    with pytest.raises(VoiceyError, match="duplicate"):
        adapter.point_inbound(NUMBER, TARGET)


def test_trunk_route_is_detached_then_restored_and_open_routes_recover(
    adapter_bundle: tuple[VobizAdapter, FakeVobiz, TelephonyLedger],
) -> None:
    adapter, fake, ledger = adapter_bundle
    fake.number.pop("application_id")
    fake.number["trunk_group_id"] = "trunk-before"
    token = adapter.point_inbound(NUMBER, TARGET)
    assert fake.number["application_id"] == "managed-app"
    assert fake.number.get("trunk_group_id") is None
    assert adapter.restore_open_routes() == 1
    assert fake.number["trunk_group_id"] == "trunk-before"
    assert ledger.get_route(token.token).state == "restored"
    assert adapter.restore_open_routes() == 0


def test_route_non_confirmation_definitive_failure_and_restore_failure_are_fenced(
    adapter_bundle: tuple[VobizAdapter, FakeVobiz, TelephonyLedger],
) -> None:
    adapter, fake, ledger = adapter_bundle
    fake.apply_route_mutations = False
    with pytest.raises(VoiceyError) as unconfirmed:
        adapter.point_inbound(NUMBER, TARGET)
    assert unconfirmed.value.code == "VY-TEL-006"
    assert ledger.open_routes(provider="vobiz")[-1].state == "ambiguous"

    fake.apply_route_mutations = True
    route_path = f"/api/v1/Account/{AUTH_ID}/numbers/+918071234567/application"
    fake.failures[("POST", route_path)] = 422
    with pytest.raises(VoiceyError) as definitive:
        adapter.point_inbound(NUMBER, TARGET)
    assert definitive.value.code == "VY-TEL-004"
    fake.failures.clear()
    fake.apps.clear()

    token = adapter.point_inbound(NUMBER, TARGET)
    fake.failures[("DELETE", route_path)] = 503
    with pytest.raises(VoiceyError, match="restore conflicted"):
        adapter.restore(token)
    assert ledger.get_route(token.token).state == "conflict"


def test_restore_rejects_foreign_and_non_restorable_tokens(
    adapter_bundle: tuple[VobizAdapter, FakeVobiz, TelephonyLedger],
) -> None:
    adapter, fake, ledger = adapter_bundle
    with pytest.raises(VoiceyError, match="another carrier"):
        adapter.restore(RollbackToken(provider="twilio", token="route_foreign"))

    foreign = ledger.prepare_route(
        provider="twilio",
        number=NUMBER,
        number_sid="number-1",
        snapshot={"application_id": "old-app", "trunk_group_id": None},
        applied={"application_id": "managed-app", "trunk_group_id": None},
    )
    with pytest.raises(VoiceyError, match="another carrier"):
        adapter.restore(RollbackToken(provider="vobiz", token=foreign.token))

    failed = ledger.prepare_route(
        provider="vobiz",
        number=NUMBER,
        number_sid="number-1",
        snapshot={"application_id": "old-app", "trunk_group_id": None},
        applied={"application_id": "managed-app", "trunk_group_id": None},
    )
    ledger.transition_route(failed.token, expected=("prepared",), state="failed")
    with pytest.raises(VoiceyError, match="cannot restore"):
        adapter.restore(RollbackToken(provider="vobiz", token=failed.token))

    already_current = ledger.prepare_route(
        provider="vobiz",
        number=NUMBER,
        number_sid="number-1",
        snapshot={"application_id": "old-app", "trunk_group_id": None},
        applied={"application_id": "different-app", "trunk_group_id": None},
    )
    ledger.transition_route(already_current.token, expected=("prepared",), state="applied")
    calls_before = len(fake.calls)
    adapter.restore(RollbackToken(provider="vobiz", token=already_current.token))
    assert len(fake.calls) == calls_before + 1
    assert ledger.get_route(already_current.token).state == "restored"


def test_outbound_failures_are_not_retried_and_reconciliation_is_explicit(
    adapter_bundle: tuple[VobizAdapter, FakeVobiz, TelephonyLedger],
) -> None:
    adapter, fake, ledger = adapter_bundle
    call_path = f"/api/v1/Account/{AUTH_ID}/Call/"
    fake.failures[("POST", call_path)] = 422
    with pytest.raises(VoiceyError) as rejected:
        adapter.start_call(NUMBER, "+919876543210", TARGET, intent_id="intent_rejected")
    assert rejected.value.code == "VY-TEL-004"
    assert ledger.get_intent("intent_rejected").state == "rejected"

    fake.failures[("POST", call_path)] = 503
    with pytest.raises(VoiceyError) as ambiguous:
        adapter.start_call(NUMBER, "+919876543210", TARGET, intent_id="intent_ambiguous")
    assert ambiguous.value.code == "VY-TEL-007"
    with pytest.raises(VoiceyError, match="do not retry"):
        adapter.reconcile_outbound("intent_ambiguous")

    fake.failures.clear()
    fake.next_call_id = ""
    with pytest.raises(VoiceyError) as malformed:
        adapter.start_call(NUMBER, "+919876543210", TARGET, intent_id="intent_malformed")
    assert malformed.value.code == "VY-TEL-007"
    assert ledger.get_intent("intent_malformed").state == "ambiguous"

    fake.next_call_id = CALL_ID
    adapter.start_call(NUMBER, "+919876543210", TARGET, intent_id="intent_bound")
    assert adapter.reconcile_outbound("intent_bound").provider_call_id == CALL_ID


def test_call_controls_reject_invalid_ranges_and_missing_transfer_origin(
    adapter_bundle: tuple[VobizAdapter, FakeVobiz, TelephonyLedger],
) -> None:
    adapter, fake, ledger = adapter_bundle
    with pytest.raises(VoiceyError, match="timeout"):
        adapter.start_call(NUMBER, "+919876543210", TARGET, timeout_s=4)
    with pytest.raises(VoiceyError, match="DTMF"):
        adapter.start_call(NUMBER, "+919876543210", TARGET, send_digits="A")
    with pytest.raises(VoiceyError, match="DTMF"):
        adapter.send_dtmf(CALL_ID, "A")

    without_origin = VobizAdapter(
        auth_id=AUTH_ID,
        auth_token=AUTH_TOKEN,
        ledger=ledger,
        client=httpx.Client(
            base_url="https://api.vobiz.ai",
            transport=httpx.MockTransport(fake.handler),
        ),
    )
    try:
        with pytest.raises(VoiceyError, match="expected_public_base"):
            without_origin.cold_transfer(CALL_ID, "+918071230000")
    finally:
        without_origin._client.close()  # pyright: ignore[reportPrivateUsage]


@pytest.mark.asyncio
async def test_recording_download_enforces_transport_type_and_size(
    tmp_path: Path,
) -> None:
    def recording_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/ok.mp3":
            return httpx.Response(200, headers={"content-type": "audio/mpeg"}, content=b"audio")
        if request.url.path == "/declared.mp3":
            return httpx.Response(
                200,
                headers={"content-type": "audio/mpeg", "content-length": "99"},
                content=b"x",
            )
        if request.url.path == "/text":
            return httpx.Response(200, headers={"content-type": "text/plain"}, content=b"x")
        if request.url.path == "/invalid-length":
            return httpx.Response(
                200,
                headers={"content-type": "audio/mpeg", "content-length": "invalid"},
                content=b"x",
            )
        return httpx.Response(503)

    recording_client = httpx.AsyncClient(
        transport=httpx.MockTransport(recording_handler),
    )
    control_client = httpx.Client(
        base_url="https://api.vobiz.ai",
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, json={})),
    )
    ledger = TelephonyLedger(tmp_path / "recording.sqlite3")
    adapter = VobizAdapter(
        auth_id=AUTH_ID,
        auth_token=AUTH_TOKEN,
        ledger=ledger,
        client=control_client,
        recording_client=recording_client,
        expected_public_base="https://voice.example.test",
    )
    artifacts = LocalArtifactStore(tmp_path / "artifacts")
    try:
        key = await adapter.download_recording(
            "https://recordings.vobiz.ai/ok.mp3",
            artifact_store=artifacts,
            storage_key="recordings/ok.mp3",
        )
        assert key == "recordings/ok.mp3"
        assert await artifacts.read(key) == b"audio"
        for url, limit in [
            ("http://recordings.vobiz.ai/ok.mp3", 10),
            ("https://user@recordings.vobiz.ai/ok.mp3", 10),
            ("https://recordings.vobiz.ai/ok.mp3", 0),
            ("https://recordings.vobiz.ai/declared.mp3", 2),
            ("https://recordings.vobiz.ai/text", 10),
            ("https://recordings.vobiz.ai/invalid-length", 10),
            ("https://recordings.vobiz.ai/server-error", 10),
        ]:
            with pytest.raises(VoiceyError) as caught:
                await adapter.download_recording(
                    url,
                    artifact_store=artifacts,
                    storage_key="recordings/rejected.mp3",
                    max_bytes=limit,
                )
            assert caught.value.code == "VY-TEL-009"
    finally:
        await recording_client.aclose()
        await asyncio.to_thread(control_client.close)
        ledger.close()


def test_signature_verification_rejects_unsafe_forms_and_expires_nonce_cache(
    tmp_path: Path,
) -> None:
    now = [1000.0]
    ledger = TelephonyLedger(tmp_path / "signature.sqlite3")
    client = httpx.Client(
        base_url="https://api.vobiz.ai",
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, json={})),
    )
    adapter = VobizAdapter(
        auth_id=AUTH_ID,
        auth_token=AUTH_TOKEN,
        ledger=ledger,
        client=client,
        expected_public_base="https://voice.example.test",
        replay_ttl_s=60,
        clock=lambda: now[0],
    )
    try:
        assert not adapter.verify_request(
            TelephonyRequest(
                scheme="https",
                host="voice.example.test",
                path="/vobiz/events",
                headers={},
                is_websocket=True,
            )
        )
        assert not adapter.verify_request(
            TelephonyRequest(
                scheme="http",
                host="voice.example.test",
                path="/vobiz/events",
                headers={},
            )
        )
        assert not adapter.verify_request(
            TelephonyRequest(
                scheme="https",
                host="voice.example.test",
                path="/vobiz/events",
                headers={"x-vobiz-signature-v3": "not-base64", "x-vobiz-signature-v3-nonce": "x"},
            )
        )
        malformed_signature = _signed_request(nonce="12345678901234567893")
        malformed_signature.headers["x-vobiz-signature-v3"] = "***"
        assert not adapter.verify_request(malformed_signature)
        unsafe_path = _signed_request(path="/vobiz/events?query=bad", nonce="12345678901234567894")
        assert not adapter.verify_request(unsafe_path)

        reusable = _signed_request(nonce="12345678901234567895")
        assert adapter.verify_request(reusable)
        now[0] += 61
        assert adapter.verify_request(reusable)
    finally:
        client.close()
        ledger.close()


@pytest.mark.parametrize(
    ("form", "expected_type", "ended_reason"),
    [
        (
            {
                "CallUUID": CALL_ID,
                "Event": "StartApp",
                "CallStatus": "in-progress",
                "Direction": "incoming",
            },
            "answered",
            None,
        ),
        (
            {
                "CallUUID": CALL_ID,
                "Event": "Init",
                "CallStatus": "queued",
                "Direction": "outgoing",
            },
            "initiated",
            None,
        ),
        (
            {
                "CallUUID": CALL_ID,
                "Event": "Hangup",
                "CallStatus": "busy",
                "HangupCause": "BUSY",
            },
            "failed",
            "carrier_error",
        ),
        (
            {
                "CallUUID": CALL_ID,
                "Digits": "12#",
            },
            "dtmf",
            None,
        ),
        (
            {
                "CallUUID": CALL_ID,
                "Event": "MachineDetection",
                "Machine": "false",
            },
            "amd",
            None,
        ),
    ],
)
def test_callback_parser_maps_nonterminal_and_failure_variants(
    adapter_bundle: tuple[VobizAdapter, FakeVobiz, TelephonyLedger],
    form: dict[str, str],
    expected_type: str,
    ended_reason: str | None,
) -> None:
    adapter, _, _ = adapter_bundle
    event = adapter.parse_event(
        TelephonyRequest(
            scheme="https",
            host="voice.example.test",
            path="/vobiz/events",
            headers={},
            form=form,
        )
    )
    assert event.type == expected_type
    assert event.ended_reason == ended_reason


@pytest.mark.parametrize(
    "form",
    [
        {},
        {"CallUUID": CALL_ID, "Direction": "sideways"},
        {"CallUUID": CALL_ID, "record_url": "https://recordings.vobiz.ai/one.mp3"},
        {"CallUUID": CALL_ID, "Event": "unknown", "CallStatus": "mystery"},
        {"CallUUID": CALL_ID, "Digits": "A"},
        {"CallUUID": ["malformed"]},
    ],
)
def test_callback_parser_rejects_incomplete_or_unknown_payloads(
    adapter_bundle: tuple[VobizAdapter, FakeVobiz, TelephonyLedger],
    form: dict[str, Any],
) -> None:
    adapter, _, _ = adapter_bundle
    with pytest.raises(VoiceyError) as caught:
        adapter.parse_event(
            TelephonyRequest(
                scheme="https",
                host="voice.example.test",
                path="/vobiz/events",
                headers={},
                form=form,
            )
        )
    assert caught.value.code in {"VY-TEL-002", "VY-TEL-008"}


@pytest.mark.parametrize(
    ("response", "operation"),
    [
        (httpx.Response(200, content=b"{"), "invalid JSON"),
        (httpx.Response(200, json=[]), "invalid envelope"),
        (httpx.Response(401, json={"error": "no"}), "http_401"),
        (httpx.Response(503, json={"error": "later"}), "definitive result"),
    ],
)
def test_control_plane_malformed_and_http_failures_are_cataloged(
    tmp_path: Path,
    response: httpx.Response,
    operation: str,
) -> None:
    client = httpx.Client(
        base_url="https://api.vobiz.ai",
        transport=httpx.MockTransport(lambda _request: response),
    )
    ledger = TelephonyLedger(tmp_path / f"{response.status_code}.sqlite3")
    adapter = VobizAdapter(
        auth_id=AUTH_ID,
        auth_token=AUTH_TOKEN,
        ledger=ledger,
        client=client,
    )
    try:
        with pytest.raises(VoiceyError, match=operation):
            adapter.account_state()
    finally:
        client.close()
        ledger.close()


def test_control_plane_network_and_pagination_envelopes_are_strict(tmp_path: Path) -> None:
    def network_failure(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline", request=request)

    network_client = httpx.Client(
        base_url="https://api.vobiz.ai",
        transport=httpx.MockTransport(network_failure),
    )
    network_ledger = TelephonyLedger(tmp_path / "network.sqlite3")
    network_adapter = VobizAdapter(
        auth_id=AUTH_ID,
        auth_token=AUTH_TOKEN,
        ledger=network_ledger,
        client=network_client,
    )
    try:
        with pytest.raises(VoiceyError) as network:
            network_adapter.account_state()
        assert network.value.code == "VY-TEL-011"
    finally:
        network_client.close()
        network_ledger.close()

    responses: list[dict[str, object]] = [
        {"items": [{"id": "number-1", "e164": NUMBER}], "total": 2},
        {
            "items": [
                {
                    "id": "number-2",
                    "e164": "+918071234568",
                    "voice_enabled": True,
                }
            ],
            "total": 2,
        },
    ]

    def paged(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params["page"])
        return httpx.Response(200, json=responses[page - 1])

    client = httpx.Client(
        base_url="https://api.vobiz.ai",
        transport=httpx.MockTransport(paged),
    )
    ledger = TelephonyLedger(tmp_path / "pages.sqlite3")
    adapter = VobizAdapter(
        auth_id=AUTH_ID,
        auth_token=AUTH_TOKEN,
        ledger=ledger,
        client=client,
    )
    try:
        numbers = adapter.list_numbers()
        assert len(numbers) == 2
        assert numbers[1].capabilities == frozenset({"voice"})
    finally:
        client.close()
        ledger.close()

    malformed_documents: list[dict[str, object]] = [
        {"items": "not-a-list"},
        {"items": [1]},
        {"items": [], "total": "not-an-int"},
    ]
    for index, document in enumerate(malformed_documents):

        def malformed_handler(
            _request: httpx.Request,
            payload: dict[str, object] = document,
        ) -> httpx.Response:
            return httpx.Response(200, json=payload)

        malformed_client = httpx.Client(
            base_url="https://api.vobiz.ai",
            transport=httpx.MockTransport(malformed_handler),
        )
        malformed_ledger = TelephonyLedger(tmp_path / f"malformed-{index}.sqlite3")
        malformed_adapter = VobizAdapter(
            auth_id=AUTH_ID,
            auth_token=AUTH_TOKEN,
            ledger=malformed_ledger,
            client=malformed_client,
        )
        try:
            with pytest.raises(VoiceyError):
                malformed_adapter.list_numbers()
        finally:
            malformed_client.close()
            malformed_ledger.close()
