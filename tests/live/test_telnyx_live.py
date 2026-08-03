"""Credentialed Telnyx Call Control/TeXML certification gates.

Every mutation and paid call requires an explicit acknowledgement value. The
account preflight is read-only and safe to run whenever credentials are present.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from voicey.storage.artifacts import LocalArtifactStore
from voicey.telephony import PipecatTarget
from voicey.telephony.ledger import TelephonyLedger
from voicey.telephony.models import RollbackToken
from voicey.telephony.telnyx import TelnyxAdapter

pytestmark = pytest.mark.live


def _required(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        pytest.skip(f"{name} is not configured")
    return value


def _adapter(tmp_path: Path, name: str) -> tuple[TelnyxAdapter, TelephonyLedger]:
    ledger = TelephonyLedger(tmp_path / f"{name}.sqlite3")
    return (
        TelnyxAdapter(
            api_key=_required("TELNYX_API_KEY"),
            public_key=_required("TELNYX_PUBLIC_KEY"),
            connection_id=_required("TELNYX_CONNECTION_ID"),
            ledger=ledger,
        ),
        ledger,
    )


def test_telnyx_live_account_and_owned_number_are_ready(tmp_path: Path) -> None:
    adapter, ledger = _adapter(tmp_path, "account")
    try:
        state = adapter.account_state()
        matches = [
            number
            for number in adapter.list_numbers()
            if number.number == _required("VOICEY_TELNYX_LIVE_FROM")
        ]
    finally:
        ledger.close()

    assert state.status == "active"
    assert len(matches) == 1
    assert "voice" in matches[0].capabilities


def test_telnyx_live_route_point_and_crash_safe_restore(tmp_path: Path) -> None:
    if os.environ.get("VOICEY_LIVE_ROUTE_CONFIRM") != "I_ACKNOWLEDGE_ROUTE_MUTATION":
        pytest.skip("VOICEY_LIVE_ROUTE_CONFIRM acknowledgement is absent")
    adapter, ledger = _adapter(tmp_path, "route")
    public_base = _required("VOICEY_LIVE_PUBLIC_BASE")
    token: RollbackToken | None = None
    try:
        token = adapter.point_inbound(
            _required("VOICEY_TELNYX_LIVE_FROM"),
            PipecatTarget(public_base),
        )
        assert ledger.get_route(token.token).state == "applied"
    finally:
        try:
            if token is not None:
                adapter.restore(token)
        finally:
            ledger.close()


def test_telnyx_live_paid_pstn_dtmf_recording_and_cold_transfer(
    tmp_path: Path,
) -> None:
    if os.environ.get("VOICEY_LIVE_CONFIRM") != "I_ACKNOWLEDGE_PSTN_CHARGES":
        pytest.skip("VOICEY_LIVE_CONFIRM charge acknowledgement is absent")
    adapter, ledger = _adapter(tmp_path, "paid-call")
    public_base = _required("VOICEY_LIVE_PUBLIC_BASE")
    call_id = adapter.start_call(
        _required("VOICEY_TELNYX_LIVE_FROM"),
        _required("VOICEY_TELNYX_LIVE_TO"),
        PipecatTarget(public_base),
        intent_id="intent_telnyx_live_certification",
        amd=True,
        record=True,
    )
    try:
        wait_seconds = float(os.environ.get("VOICEY_LIVE_ANSWER_WAIT_SECONDS", "15"))
        time.sleep(wait_seconds)
        adapter.send_dtmf(call_id, "12#")
        time.sleep(2)
        adapter.cold_transfer(call_id, _required("VOICEY_TELNYX_TRANSFER_TO"))
        time.sleep(5)
    finally:
        try:
            adapter.hangup(call_id)
        finally:
            ledger.close()

    assert call_id


@pytest.mark.asyncio
async def test_telnyx_live_signed_recording_url_ingests_to_engine_storage(
    tmp_path: Path,
) -> None:
    """Use the URL captured from a verified call.recording.saved callback."""
    adapter, ledger = _adapter(tmp_path, "recording")
    artifacts = LocalArtifactStore(tmp_path / "artifacts")
    try:
        key = await adapter.download_recording(
            _required("VOICEY_TELNYX_LIVE_RECORDING_URL"),
            artifact_store=artifacts,
            storage_key="recordings/telnyx-live-certification.mp3",
        )
        content = await artifacts.read(key)
    finally:
        ledger.close()

    assert content
