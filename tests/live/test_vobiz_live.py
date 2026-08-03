"""Credentialed Vobiz Voice API and media-stream certification gates."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from voicey.storage.artifacts import LocalArtifactStore
from voicey.telephony import PipecatTarget
from voicey.telephony.ledger import TelephonyLedger
from voicey.telephony.models import RollbackToken
from voicey.telephony.vobiz import VobizAdapter

pytestmark = pytest.mark.live


def _required(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        pytest.skip(f"{name} is not configured")
    return value


def _adapter(tmp_path: Path, name: str) -> tuple[VobizAdapter, TelephonyLedger]:
    ledger = TelephonyLedger(tmp_path / f"{name}.sqlite3")
    return (
        VobizAdapter(
            auth_id=_required("VOBIZ_AUTH_ID"),
            auth_token=_required("VOBIZ_AUTH_TOKEN"),
            ledger=ledger,
            expected_public_base=os.environ.get("VOICEY_LIVE_PUBLIC_BASE"),
        ),
        ledger,
    )


def test_vobiz_live_account_and_owned_number_are_ready(tmp_path: Path) -> None:
    adapter, ledger = _adapter(tmp_path, "account")
    try:
        state = adapter.account_state()
        matches = [
            number
            for number in adapter.list_numbers()
            if number.number == _required("VOICEY_VOBIZ_LIVE_FROM")
        ]
    finally:
        ledger.close()

    assert state.status.casefold() == "active"
    assert len(matches) == 1
    assert "voice" in matches[0].capabilities


def test_vobiz_live_route_point_and_crash_safe_restore(tmp_path: Path) -> None:
    if os.environ.get("VOICEY_LIVE_ROUTE_CONFIRM") != "I_ACKNOWLEDGE_ROUTE_MUTATION":
        pytest.skip("VOICEY_LIVE_ROUTE_CONFIRM acknowledgement is absent")
    adapter, ledger = _adapter(tmp_path, "route")
    token: RollbackToken | None = None
    try:
        token = adapter.point_inbound(
            _required("VOICEY_VOBIZ_LIVE_FROM"),
            PipecatTarget(_required("VOICEY_LIVE_PUBLIC_BASE")),
        )
        assert ledger.get_route(token.token).state == "applied"
    finally:
        try:
            if token is not None:
                adapter.restore(token)
        finally:
            ledger.close()


def test_vobiz_live_paid_pstn_amd_dtmf_recording_and_transfer(
    tmp_path: Path,
) -> None:
    if os.environ.get("VOICEY_LIVE_CONFIRM") != "I_ACKNOWLEDGE_PSTN_CHARGES":
        pytest.skip("VOICEY_LIVE_CONFIRM charge acknowledgement is absent")
    adapter, ledger = _adapter(tmp_path, "paid-call")
    target = PipecatTarget(_required("VOICEY_LIVE_PUBLIC_BASE"))
    call_id = adapter.start_call(
        _required("VOICEY_VOBIZ_LIVE_FROM"),
        _required("VOICEY_VOBIZ_LIVE_TO"),
        target,
        intent_id="intent_vobiz_live_certification",
        amd=True,
        send_digits="1w2#",
        record=True,
    )
    try:
        wait_seconds = float(os.environ.get("VOICEY_LIVE_ANSWER_WAIT_SECONDS", "15"))
        time.sleep(wait_seconds)
        adapter.send_dtmf(call_id, "12#")
        time.sleep(2)
        adapter.cold_transfer(call_id, _required("VOICEY_VOBIZ_TRANSFER_TO"))
        time.sleep(5)
    finally:
        try:
            adapter.hangup(call_id)
        finally:
            ledger.close()
    assert call_id


@pytest.mark.asyncio
async def test_vobiz_live_signed_recording_url_ingests_to_engine_storage(
    tmp_path: Path,
) -> None:
    adapter, ledger = _adapter(tmp_path, "recording")
    artifacts = LocalArtifactStore(tmp_path / "artifacts")
    try:
        key = await adapter.download_recording(
            _required("VOICEY_VOBIZ_LIVE_RECORDING_URL"),
            artifact_store=artifacts,
            storage_key="recordings/vobiz-live-certification.mp3",
        )
        content = await artifacts.read(key)
    finally:
        ledger.close()
    assert content
