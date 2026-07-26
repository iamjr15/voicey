"""Credentialed Twilio gates.

These tests never silently fall back from test credentials to paid credentials.
The paid cases additionally require explicit acknowledgement environment values.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest
from twilio.rest import Client

from voicekit.telephony import PipecatTarget
from voicekit.telephony.ledger import TelephonyLedger
from voicekit.telephony.twilio import TwilioAdapter

pytestmark = pytest.mark.live

TEST_FROM = "+15005550006"
TEST_TO = "+14108675310"


def _required(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        pytest.skip(f"{name} is not configured")
    return value


def test_twilio_test_credentials_accept_outbound_contract_without_charge(
    tmp_path: Path,
) -> None:
    account_sid = _required("TWILIO_TEST_ACCOUNT_SID")
    auth_token = _required("TWILIO_TEST_AUTH_TOKEN")
    ledger = TelephonyLedger(tmp_path / "test-credentials.sqlite3")
    adapter = TwilioAdapter(
        account_sid=account_sid,
        auth_token=auth_token,
        ledger=ledger,
    )
    try:
        call_sid = adapter.start_call(
            TEST_FROM,
            TEST_TO,
            PipecatTarget(https_base="https://example.invalid"),
            intent_id="intent_test_credentials",
        )
    finally:
        ledger.close()

    assert call_sid.startswith("CA")


def test_twilio_live_account_and_owned_number_are_ready() -> None:
    account_sid = _required("TWILIO_ACCOUNT_SID")
    auth_token = _required("TWILIO_AUTH_TOKEN")
    from_number = _required("VOICEKIT_TWILIO_LIVE_FROM")
    client = Client(account_sid, auth_token)

    account = client.api.accounts(account_sid).fetch()
    matches = client.incoming_phone_numbers.list(
        phone_number=from_number,
        limit=2,
    )

    assert account.status == "active"
    assert len(matches) == 1


def test_twilio_live_route_point_and_crash_safe_restore(tmp_path: Path) -> None:
    if os.environ.get("VOICEKIT_LIVE_ROUTE_CONFIRM") != "I_ACKNOWLEDGE_ROUTE_MUTATION":
        pytest.skip("VOICEKIT_LIVE_ROUTE_CONFIRM acknowledgement is absent")
    account_sid = _required("TWILIO_ACCOUNT_SID")
    auth_token = _required("TWILIO_AUTH_TOKEN")
    from_number = _required("VOICEKIT_TWILIO_LIVE_FROM")
    public_base = _required("VOICEKIT_LIVE_PUBLIC_BASE")
    ledger = TelephonyLedger(tmp_path / "live-route.sqlite3")
    adapter = TwilioAdapter(
        account_sid=account_sid,
        auth_token=auth_token,
        ledger=ledger,
        expected_public_base=public_base,
    )

    token = adapter.point_inbound(from_number, PipecatTarget(https_base=public_base))
    try:
        assert ledger.get_route(token.token).state == "applied"
    finally:
        adapter.restore(token)
        ledger.close()


def test_twilio_live_paid_pstn_dtmf_recording_and_cold_transfer(
    tmp_path: Path,
) -> None:
    if os.environ.get("VOICEKIT_LIVE_CONFIRM") != "I_ACKNOWLEDGE_PSTN_CHARGES":
        pytest.skip("VOICEKIT_LIVE_CONFIRM charge acknowledgement is absent")
    account_sid = _required("TWILIO_ACCOUNT_SID")
    auth_token = _required("TWILIO_AUTH_TOKEN")
    from_number = _required("VOICEKIT_TWILIO_LIVE_FROM")
    to_number = _required("VOICEKIT_TWILIO_LIVE_TO")
    transfer_to = _required("VOICEKIT_TWILIO_TRANSFER_TO")
    public_base = _required("VOICEKIT_LIVE_PUBLIC_BASE")
    ledger = TelephonyLedger(tmp_path / "live-call.sqlite3")
    client = Client(account_sid, auth_token)
    adapter = TwilioAdapter(
        account_sid=account_sid,
        auth_token=auth_token,
        ledger=ledger,
        client=client,
        expected_public_base=public_base,
    )
    call_sid = adapter.start_call(
        from_number,
        to_number,
        PipecatTarget(https_base=public_base),
        intent_id="intent_live_certification",
        record=True,
    )
    try:
        deadline = time.monotonic() + 120
        status = ""
        while time.monotonic() < deadline:
            status = str(client.calls(call_sid).fetch().status)
            if status == "in-progress":
                break
            if status in {"busy", "failed", "no-answer", "canceled", "completed"}:
                pytest.fail(f"call became terminal before answer: {status}")
            time.sleep(1)
        assert status == "in-progress"

        adapter.send_dtmf(call_sid, "12#")
        time.sleep(2)
        adapter.cold_transfer(call_sid, transfer_to)
        time.sleep(5)
    finally:
        try:
            adapter.hangup(call_sid)
        finally:
            ledger.close()

    recordings = client.recordings.list(call_sid=call_sid, limit=5)
    assert recordings
