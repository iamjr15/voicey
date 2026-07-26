import sqlite3
import stat
from datetime import UTC, datetime
from pathlib import Path

import pytest

from voicekit.errors import VoicekitError
from voicekit.telephony import (
    LiveKitTarget,
    PipecatTarget,
    RollbackToken,
    TelephonyAdapter,
    adapter_names,
    load_adapter,
)
from voicekit.telephony.ledger import TelephonyLedger

ACCOUNT_SID = "AC" + "1" * 32


def test_pipecat_target_builds_https_and_wss_routes_without_query_data() -> None:
    target = PipecatTarget(
        https_base="https://voice.example.test/prefix",
        ws_path="/media",
        custom_parameters={"agent": "clinic"},
    )

    assert target.stream_url == "wss://voice.example.test/prefix/media"
    assert target.answer_url == "https://voice.example.test/prefix/twilio/answer"
    assert target.event_url("intent_1") == (
        "https://voice.example.test/prefix/twilio/events/intent_1"
    )
    assert "?" not in target.stream_url


@pytest.mark.parametrize(
    "arguments",
    [
        {"https_base": "http://voice.example.test"},
        {"https_base": "https://voice.example.test?secret=value"},
        {
            "https_base": "https://voice.example.test",
            "custom_parameters": {"not valid": "value"},
        },
    ],
)
def test_invalid_pipecat_target_is_cataloged(arguments: dict[str, object]) -> None:
    with pytest.raises(VoicekitError) as caught:
        PipecatTarget(**arguments)  # type: ignore[arg-type]

    assert caught.value.code == "VK-TEL-002"


def test_telephony_ledger_reopens_routes_and_intents_with_private_permissions(
    tmp_path: Path,
) -> None:
    path = tmp_path / "private" / "telephony.sqlite3"
    created_at = datetime(2026, 7, 26, 10, tzinfo=UTC)
    ledger = TelephonyLedger(path)
    route = ledger.prepare_route(
        provider="twilio",
        number="+14155550100",
        number_sid="PN" + "2" * 32,
        snapshot={"voice_url": "https://old.example.test"},
        applied={"voice_url": "https://new.example.test"},
        now=created_at,
    )
    ledger.prepare_intent(
        intent_id="intent_reopen",
        provider="twilio",
        from_number="+14155550100",
        to_number="+14155550101",
        target={"runtime": "pipecat"},
        now=created_at,
    )
    ledger.close()

    reopened = TelephonyLedger(path)

    assert reopened.get_route(route.token).snapshot == {"voice_url": "https://old.example.test"}
    assert reopened.get_intent("intent_reopen").state == "prepared"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    reopened.close()


def test_telephony_ledger_cas_and_callback_conflicts_are_visible(tmp_path: Path) -> None:
    ledger = TelephonyLedger(tmp_path / "telephony.sqlite3")
    route = ledger.prepare_route(
        provider="twilio",
        number="+14155550100",
        number_sid="PN" + "2" * 32,
        snapshot={},
        applied={},
    )
    ledger.transition_route(route.token, expected=("prepared",), state="applied")
    with pytest.raises(VoicekitError) as stale:
        ledger.transition_route(route.token, expected=("prepared",), state="restored")
    assert stale.value.code == "VK-TEL-006"

    ledger.prepare_intent(
        intent_id="intent_conflict",
        provider="twilio",
        from_number="+14155550100",
        to_number="+14155550101",
        target={},
    )
    ledger.bind_callback(
        "intent_conflict",
        provider_call_id="CA" + "3" * 32,
        provider_status="ringing",
        terminal=False,
    )
    with pytest.raises(VoicekitError) as conflict:
        ledger.bind_callback(
            "intent_conflict",
            provider_call_id="CA" + "4" * 32,
            provider_status="ringing",
            terminal=False,
        )
    assert conflict.value.code == "VK-TEL-007"
    assert ledger.get_intent("intent_conflict").state == "conflict"


def test_duplicate_outbound_intent_is_never_overwritten(tmp_path: Path) -> None:
    ledger = TelephonyLedger(tmp_path / "telephony.sqlite3")
    ledger.prepare_intent(
        intent_id="intent_duplicate",
        provider="twilio",
        from_number="+14155550100",
        to_number="+14155550101",
        target={},
    )

    with pytest.raises(VoicekitError) as caught:
        ledger.prepare_intent(
            intent_id="intent_duplicate",
            provider="twilio",
            from_number="+14155550100",
            to_number="+14155550101",
            target={},
        )

    assert caught.value.code == "VK-TEL-007"
    assert ledger.get_intent("intent_duplicate").from_number == "+14155550100"


def test_ledger_unknown_records_naive_time_and_newer_schema_fail_closed(
    tmp_path: Path,
) -> None:
    ledger = TelephonyLedger(tmp_path / "telephony.sqlite3")
    with pytest.raises(VoicekitError) as route:
        ledger.get_route("route_missing")
    with pytest.raises(VoicekitError) as intent:
        ledger.get_intent("intent_missing")
    with pytest.raises(VoicekitError) as naive:
        ledger.prepare_intent(
            intent_id="intent_naive",
            provider="twilio",
            from_number="+14155550100",
            to_number="+14155550101",
            target={},
            now=datetime(2026, 7, 26),
        )
    assert route.value.code == "VK-TEL-006"
    assert intent.value.code == "VK-TEL-007"
    assert naive.value.code == "VK-TEL-005"
    assert ledger.unresolved_intents(provider="twilio") == ()
    ledger.close()

    newer = tmp_path / "newer.sqlite3"
    connection = sqlite3.connect(newer)
    connection.execute("PRAGMA user_version=99")
    connection.close()
    with pytest.raises(VoicekitError) as schema:
        TelephonyLedger(newer)
    assert schema.value.code == "VK-TEL-005"


def test_entry_point_registry_loads_twilio_without_importing_other_carriers(
    tmp_path: Path,
) -> None:
    assert "twilio" in adapter_names()

    class EmptyCollection:
        def list(self, **kwargs: object) -> list[object]:
            del kwargs
            return []

    class Client:
        incoming_phone_numbers = EmptyCollection()

    adapter = load_adapter(
        "twilio",
        account_sid=ACCOUNT_SID,
        auth_token="test-token",
        client=Client(),
        ledger_path=tmp_path / "telephony.sqlite3",
    )

    assert isinstance(adapter, TelephonyAdapter)
    assert adapter.provider == "twilio"
    assert not adapter.capabilities.native_outbound_idempotency
    assert adapter.list_numbers() == []

    with pytest.raises(VoicekitError) as missing:
        load_adapter("not-installed")
    assert missing.value.code == "VK-TEL-001"


def test_livekit_target_is_present_but_not_claimed_by_p1_twilio_capability() -> None:
    target = LiveKitTarget(project="example", sip_uri="sip:example@sip.livekit.cloud")

    assert target.project == "example"
    assert target.sip_uri.startswith("sip:")
    assert RollbackToken(provider="twilio", token="route_1").provider == "twilio"
