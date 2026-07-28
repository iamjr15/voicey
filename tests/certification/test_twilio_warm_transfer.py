from __future__ import annotations

from collections.abc import Callable, Generator
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from xml.etree import ElementTree

import pytest
from twilio.base.exceptions import TwilioRestException

from voicekit.errors import VoicekitError
from voicekit.telephony import PipecatTarget, TelephonyRequest
from voicekit.telephony.ledger import TelephonyLedger
from voicekit.telephony.twilio import TwilioAdapter

ACCOUNT_SID = "AC" + "1" * 32
CALLER_SID = "CA" + "2" * 32
HUMAN_SID = "CA" + "3" * 32
OTHER_SID = "CA" + "4" * 32
CONFERENCE_SID = "CF" + "5" * 32
SECOND_HUMAN_SID = "CA" + "6" * 32
FROM_NUMBER = "+14155550100"
TO_NUMBER = "+14155550101"
TARGET = PipecatTarget(https_base="https://voice.example.test")


class _CallContext:
    def __init__(self, calls: _Calls, sid: str) -> None:
        self.calls = calls
        self.sid = sid

    def update(self, **arguments: object) -> SimpleNamespace:
        if self.calls.update_error is not None:
            raise self.calls.update_error
        self.calls.updates.append((self.sid, dict(arguments)))
        return SimpleNamespace(sid=self.sid, status=arguments.get("status", "in-progress"))


class _Calls:
    def __init__(self) -> None:
        self.creates: list[dict[str, object]] = []
        self.updates: list[tuple[str, dict[str, object]]] = []
        self.create_error: Exception | None = None
        self.update_error: Exception | None = None
        self.on_create: Callable[[str], None] | None = None

    def __call__(self, sid: str) -> _CallContext:
        return _CallContext(self, sid)

    def create(self, **arguments: object) -> SimpleNamespace:
        self.creates.append(dict(arguments))
        if self.create_error is not None:
            raise self.create_error
        sid = HUMAN_SID if len(self.creates) == 1 else SECOND_HUMAN_SID
        if self.on_create is not None:
            self.on_create(sid)
        return SimpleNamespace(sid=sid, status="queued")


class _Client:
    def __init__(self) -> None:
        self.calls = _Calls()


@pytest.fixture
def bundle(
    tmp_path: Path,
) -> Generator[tuple[TwilioAdapter, _Client, TelephonyLedger, Path]]:
    path = tmp_path / "warm.sqlite3"
    client = _Client()
    ledger = TelephonyLedger(path)
    adapter = TwilioAdapter(
        account_sid=ACCOUNT_SID,
        auth_token="test-auth-token",
        client=client,
        ledger=ledger,
        expected_public_base=TARGET.https_base,
    )
    yield adapter, client, ledger, path
    ledger.close()


def _request(
    transfer_id: str,
    action: str,
    form: dict[str, str],
) -> TelephonyRequest:
    return TelephonyRequest(
        scheme="https",
        host="voice.example.test",
        path=f"/twilio/warm-transfer/{transfer_id}/{action}",
        headers={},
        form=form,
        peer_host="203.0.113.10",
        route_params={"transfer_id": transfer_id},
    )


def _start(adapter: TwilioAdapter, *, briefing: str = "Billing needs duplicate-charge help."):
    return adapter.start_warm_transfer(
        caller_call_sid=CALLER_SID,
        from_number=FROM_NUMBER,
        to_number=TO_NUMBER,
        briefing=briefing,
        target=TARGET,
        transfer_id="warm_" + "a" * 32,
        timeout_s=25,
    )


def test_private_briefing_create_is_fenced_and_not_persisted(
    bundle: tuple[TwilioAdapter, _Client, TelephonyLedger, Path],
) -> None:
    adapter, client, ledger, path = bundle
    briefing = "Jordan reports a duplicate charge & requests billing."

    record = _start(adapter, briefing=briefing)

    assert record.state == "dialing"
    assert record.human_call_id == HUMAN_SID
    assert record.briefing_digest
    assert briefing.encode() not in path.read_bytes()
    create = client.calls.creates[0]
    assert create["to"] == TO_NUMBER
    assert create["from_"] == FROM_NUMBER
    assert create["record"] is False
    assert create["timeout"] == 25
    assert create["status_callback"] == (
        f"{TARGET.https_base}/twilio/warm-transfer/{record.transfer_id}/events"
    )
    root = ElementTree.fromstring(str(create["twiml"]))
    gather = root.find("Gather")
    assert gather is not None
    assert gather.attrib["numDigits"] == "1"
    assert gather.attrib["action"].endswith(f"/{record.transfer_id}/accept")
    assert briefing in "".join(root.itertext())
    assert client.calls.updates == []
    assert ledger.open_warm_transfers(provider="twilio") == (record,)


def test_initiated_callback_can_bind_before_create_response_returns(
    bundle: tuple[TwilioAdapter, _Client, TelephonyLedger, Path],
) -> None:
    adapter, client, ledger, _ = bundle
    transfer_id = "warm_" + "a" * 32

    def early_callback(sid: str) -> None:
        adapter.parse_warm_transfer_event(
            _request(
                transfer_id,
                "events",
                {"CallSid": sid, "CallStatus": "initiated"},
            )
        )

    client.calls.on_create = early_callback

    record = _start(adapter)

    assert record.state == "dialing"
    assert record.human_call_id == HUMAN_SID
    assert record.last_status == "queued"
    assert ledger.get_warm_transfer(transfer_id) == record


def test_accept_callback_can_complete_before_create_response_returns(
    bundle: tuple[TwilioAdapter, _Client, TelephonyLedger, Path],
) -> None:
    adapter, client, ledger, _ = bundle
    transfer_id = "warm_" + "a" * 32

    def early_accept(sid: str) -> None:
        adapter.warm_transfer_accept_response(
            _request(
                transfer_id,
                "accept",
                {"CallSid": sid, "Digits": "1"},
            )
        )

    client.calls.on_create = early_accept

    record = _start(adapter)

    assert record.state == "accepted"
    assert record.human_call_id == HUMAN_SID
    assert ledger.get_warm_transfer(transfer_id) == record


@pytest.mark.parametrize(
    ("status", "state"),
    [("completed", "declined"), ("failed", "failed")],
)
def test_terminal_callback_can_complete_before_create_response_returns(
    bundle: tuple[TwilioAdapter, _Client, TelephonyLedger, Path],
    status: str,
    state: str,
) -> None:
    adapter, client, ledger, _ = bundle
    transfer_id = "warm_" + "a" * 32

    def early_terminal(sid: str) -> None:
        adapter.parse_warm_transfer_event(
            _request(
                transfer_id,
                "events",
                {"CallSid": sid, "CallStatus": status},
            )
        )

    client.calls.on_create = early_terminal

    record = _start(adapter)

    assert record.state == state
    assert record.human_call_id == HUMAN_SID
    assert ledger.get_warm_transfer(transfer_id) == record


def test_accept_then_bridge_uses_waiting_human_and_starts_with_caller(
    bundle: tuple[TwilioAdapter, _Client, TelephonyLedger, Path],
) -> None:
    adapter, client, ledger, _ = bundle
    record = _start(adapter)

    human_xml = adapter.warm_transfer_accept_response(
        _request(
            record.transfer_id,
            "accept",
            {"CallSid": HUMAN_SID, "Digits": "1"},
        )
    )
    human_conference = ElementTree.fromstring(human_xml).find("./Dial/Conference")
    assert human_conference is not None
    assert human_conference.text == record.conference_name
    assert human_conference.attrib["startConferenceOnEnter"] == "false"
    assert human_conference.attrib["endConferenceOnExit"] == "false"
    assert human_conference.attrib["participantLabel"] == "human"
    assert ledger.get_warm_transfer(record.transfer_id).state == "accepted"

    bridged = adapter.bridge_warm_transfer(record.transfer_id)

    assert bridged.state == "bridged"
    assert client.calls.updates[0][0] == CALLER_SID
    caller_xml = ElementTree.fromstring(str(client.calls.updates[0][1]["twiml"]))
    caller_conference = caller_xml.find("./Dial/Conference")
    assert caller_conference is not None
    assert caller_conference.text == record.conference_name
    assert caller_conference.attrib["startConferenceOnEnter"] == "true"
    assert caller_conference.attrib["endConferenceOnExit"] == "true"
    assert caller_conference.attrib["participantLabel"] == "caller"

    completed = adapter.parse_warm_conference_event(
        _request(
            record.transfer_id,
            "conference",
            {
                "ConferenceSid": CONFERENCE_SID,
                "StatusCallbackEvent": "conference-end",
            },
        )
    )
    assert completed.state == "completed"
    assert completed.conference_id == CONFERENCE_SID


def test_duplicate_accept_is_idempotent_and_decline_is_terminal(
    bundle: tuple[TwilioAdapter, _Client, TelephonyLedger, Path],
) -> None:
    adapter, _, ledger, _ = bundle
    record = _start(adapter)
    accept = _request(
        record.transfer_id,
        "accept",
        {"CallSid": HUMAN_SID, "Digits": "1"},
    )

    first = adapter.warm_transfer_accept_response(accept)
    second = adapter.warm_transfer_accept_response(accept)

    assert first == second
    assert ledger.get_warm_transfer(record.transfer_id).state == "accepted"

    other = adapter.start_warm_transfer(
        caller_call_sid=OTHER_SID,
        from_number=FROM_NUMBER,
        to_number=TO_NUMBER,
        briefing="Caller asks for a human.",
        target=TARGET,
        transfer_id="warm_" + "b" * 32,
    )
    declined_xml = adapter.warm_transfer_accept_response(
        _request(
            other.transfer_id,
            "accept",
            {"CallSid": SECOND_HUMAN_SID, "Digits": "9"},
        )
    )
    assert ElementTree.fromstring(declined_xml).find("Hangup") is not None
    assert ledger.get_warm_transfer(other.transfer_id).state == "declined"


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("busy", "declined"),
        ("no-answer", "declined"),
        ("failed", "failed"),
        ("ringing", "dialing"),
    ],
)
def test_human_status_callback_terminalizes_before_bridge(
    bundle: tuple[TwilioAdapter, _Client, TelephonyLedger, Path],
    status: str,
    expected: str,
) -> None:
    adapter, _, _, _ = bundle
    record = _start(adapter)

    updated = adapter.parse_warm_transfer_event(
        _request(
            record.transfer_id,
            "events",
            {"CallSid": HUMAN_SID, "CallStatus": status},
        )
    )

    assert updated.state == expected
    assert updated.last_status == status


def test_create_and_bridge_uncertainty_are_never_retried(
    bundle: tuple[TwilioAdapter, _Client, TelephonyLedger, Path],
) -> None:
    adapter, client, ledger, _ = bundle
    client.calls.create_error = TimeoutError("network")

    with pytest.raises(VoicekitError) as create:
        _start(adapter)

    assert create.value.code == "VK-TEL-012"
    assert ledger.get_warm_transfer("warm_" + "a" * 32).state == "ambiguous"
    assert len(client.calls.creates) == 1

    client.calls.create_error = None
    record = adapter.start_warm_transfer(
        caller_call_sid=CALLER_SID,
        from_number=FROM_NUMBER,
        to_number=TO_NUMBER,
        briefing="A separate accepted transfer.",
        target=TARGET,
        transfer_id="warm_" + "b" * 32,
    )
    adapter.warm_transfer_accept_response(
        _request(
            record.transfer_id,
            "accept",
            {"CallSid": SECOND_HUMAN_SID, "Digits": "1"},
        )
    )
    client.calls.update_error = TimeoutError("network")

    with pytest.raises(VoicekitError) as bridge:
        adapter.bridge_warm_transfer(record.transfer_id)

    assert bridge.value.code == "VK-TEL-012"
    assert ledger.get_warm_transfer(record.transfer_id).state == "ambiguous"
    assert len(client.calls.updates) == 0


def test_definitive_create_rejection_and_bridge_rejection_are_terminal(
    bundle: tuple[TwilioAdapter, _Client, TelephonyLedger, Path],
) -> None:
    adapter, client, ledger, _ = bundle
    client.calls.create_error = TwilioRestException(400, "/Calls", code=21211)

    with pytest.raises(VoicekitError) as create:
        _start(adapter)

    assert create.value.code == "VK-TEL-004"
    assert ledger.get_warm_transfer("warm_" + "a" * 32).state == "failed"

    client.calls.create_error = None
    record = adapter.start_warm_transfer(
        caller_call_sid=CALLER_SID,
        from_number=FROM_NUMBER,
        to_number=TO_NUMBER,
        briefing="A separate accepted transfer.",
        target=TARGET,
        transfer_id="warm_" + "b" * 32,
    )
    adapter.warm_transfer_accept_response(
        _request(
            record.transfer_id,
            "accept",
            {"CallSid": SECOND_HUMAN_SID, "Digits": "1"},
        )
    )
    client.calls.update_error = TwilioRestException(404, "/Calls", code=20404)

    with pytest.raises(VoicekitError) as bridge:
        adapter.bridge_warm_transfer(record.transfer_id)

    assert bridge.value.code == "VK-TEL-012"
    assert ledger.get_warm_transfer(record.transfer_id).state == "failed"


def test_callback_identity_conflicts_fail_closed(
    bundle: tuple[TwilioAdapter, _Client, TelephonyLedger, Path],
) -> None:
    adapter, _, ledger, _ = bundle
    record = _start(adapter)

    with pytest.raises(VoicekitError) as conflict:
        adapter.warm_transfer_accept_response(
            _request(
                record.transfer_id,
                "accept",
                {"CallSid": OTHER_SID, "Digits": "1"},
            )
        )

    assert conflict.value.code == "VK-TEL-012"
    assert ledger.get_warm_transfer(record.transfer_id).state == "conflict"


def test_unknown_status_and_conference_participant_are_rejected(
    bundle: tuple[TwilioAdapter, _Client, TelephonyLedger, Path],
) -> None:
    adapter, _, ledger, _ = bundle
    record = _start(adapter)
    with pytest.raises(VoicekitError) as status:
        adapter.parse_warm_transfer_event(
            _request(
                record.transfer_id,
                "events",
                {"CallSid": HUMAN_SID, "CallStatus": "new-status"},
            )
        )
    assert status.value.code == "VK-TEL-008"
    assert ledger.get_warm_transfer(record.transfer_id).state == "dialing"

    adapter.warm_transfer_accept_response(
        _request(
            record.transfer_id,
            "accept",
            {"CallSid": HUMAN_SID, "Digits": "1"},
        )
    )
    with pytest.raises(VoicekitError) as conference_event:
        adapter.parse_warm_conference_event(
            _request(
                record.transfer_id,
                "conference",
                {
                    "ConferenceSid": CONFERENCE_SID,
                    "StatusCallbackEvent": "conference-new-event",
                },
            )
        )
    assert conference_event.value.code == "VK-TEL-008"
    assert ledger.get_warm_transfer(record.transfer_id).state == "accepted"

    with pytest.raises(VoicekitError) as participant:
        adapter.parse_warm_conference_event(
            _request(
                record.transfer_id,
                "conference",
                {
                    "ConferenceSid": CONFERENCE_SID,
                    "StatusCallbackEvent": "participant-join",
                    "CallSid": OTHER_SID,
                },
            )
        )
    assert participant.value.code == "VK-TEL-012"
    assert ledger.get_warm_transfer(record.transfer_id).state == "conflict"


def test_timeout_abort_and_startup_recovery_hang_up_only_known_human_legs(
    bundle: tuple[TwilioAdapter, _Client, TelephonyLedger, Path],
) -> None:
    adapter, client, ledger, _ = bundle
    record = _start(adapter)

    aborted = adapter.abort_warm_transfer(record.transfer_id, reason="accept timeout")

    assert aborted.state == "failed"
    assert client.calls.updates == [(HUMAN_SID, {"status": "completed"})]

    recovered_record = adapter.start_warm_transfer(
        caller_call_sid=OTHER_SID,
        from_number=FROM_NUMBER,
        to_number=TO_NUMBER,
        briefing="Recover this orphan.",
        target=TARGET,
        transfer_id="warm_" + "b" * 32,
    )
    assert adapter.recover_warm_transfers() == 1
    assert ledger.get_warm_transfer(recovered_record.transfer_id).state == "recovered"
    assert client.calls.updates[-1] == (SECOND_HUMAN_SID, {"status": "completed"})


@pytest.mark.parametrize(
    "overrides",
    [
        {"briefing": ""},
        {"briefing": "x" * 501},
        {"timeout_s": 4},
        {"transfer_id": "warm_invalid"},
    ],
)
def test_warm_transfer_input_validation_precedes_provider_mutation(
    bundle: tuple[TwilioAdapter, _Client, TelephonyLedger, Path],
    overrides: dict[str, Any],
) -> None:
    adapter, client, _, _ = bundle
    arguments: dict[str, Any] = {
        "caller_call_sid": CALLER_SID,
        "from_number": FROM_NUMBER,
        "to_number": TO_NUMBER,
        "briefing": "Valid briefing.",
        "target": TARGET,
        "transfer_id": "warm_" + "a" * 32,
        "timeout_s": 30,
    }
    arguments.update(overrides)

    with pytest.raises(VoicekitError):
        adapter.start_warm_transfer(**arguments)

    assert client.calls.creates == []


def test_warm_transfer_requires_the_verified_callback_origin_before_dial(
    bundle: tuple[TwilioAdapter, _Client, TelephonyLedger, Path],
) -> None:
    adapter, client, _, _ = bundle
    adapter.expected_public_base = None

    with pytest.raises(VoicekitError) as caught:
        _start(adapter)

    assert caught.value.code == "VK-TEL-002"
    assert client.calls.creates == []
