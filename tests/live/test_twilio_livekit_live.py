"""Credentialed Twilio↔LiveKit SIP mutation and paid-call gates."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from livekit import api
from twilio.rest import Client

from voicey.runtimes.livekit.sip import (
    LiveKitSipDialer,
    TwilioElasticSipBackend,
    TwilioLiveKitSipConfig,
    TwilioLiveKitSipProvisioner,
)
from voicey.telephony.ledger import TelephonyLedger

pytestmark = pytest.mark.live


def _required(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        pytest.skip(f"{name} is not configured")
    return value


def _livekit_api() -> api.LiveKitAPI:
    return api.LiveKitAPI(
        url=_required("LIVEKIT_URL"),
        api_key=_required("LIVEKIT_API_KEY"),
        api_secret=_required("LIVEKIT_API_SECRET"),
    )


def _config() -> TwilioLiveKitSipConfig:
    return TwilioLiveKitSipConfig(
        number=_required("VOICEY_TWILIO_LIVE_FROM"),
        agent_name=_required("VOICEY_LIVEKIT_AGENT_NAME"),
        livekit_sip_uri=_required("VOICEY_LIVEKIT_SIP_URI"),
        twilio_domain_name=_required("VOICEY_TWILIO_SIP_DOMAIN"),
        auth_username=_required("VOICEY_TWILIO_SIP_USERNAME"),
        auth_password=_required("VOICEY_TWILIO_SIP_PASSWORD"),
        record=True,
    )


@pytest.mark.asyncio
async def test_live_twilio_livekit_provision_reuse_and_rollback(tmp_path: Path) -> None:
    if os.environ.get("VOICEY_LIVE_ROUTE_CONFIRM") != "I_ACKNOWLEDGE_ROUTE_MUTATION":
        pytest.skip("VOICEY_LIVE_ROUTE_CONFIRM acknowledgement is absent")
    ledger = TelephonyLedger(tmp_path / "twilio-livekit-provision.sqlite3")
    livekit = _livekit_api()
    twilio = TwilioElasticSipBackend(
        Client(
            _required("TWILIO_ACCOUNT_SID"),
            _required("TWILIO_AUTH_TOKEN"),
        )
    )
    provisioner = TwilioLiveKitSipProvisioner(
        livekit=livekit.sip,
        twilio=twilio,
        ledger=ledger,
    )
    first = None
    try:
        first = await provisioner.provision(_config())
        second = await provisioner.provision(_config())
        assert first.livekit_inbound_trunk_id == second.livekit_inbound_trunk_id
        assert first.livekit_outbound_trunk_id == second.livekit_outbound_trunk_id
        assert first.livekit_dispatch_rule_id == second.livekit_dispatch_rule_id
        assert second.created_resources == 0
    finally:
        if first is not None:
            await provisioner.rollback(first.operation_id)
        await livekit.aclose()
        ledger.close()


@pytest.mark.asyncio
async def test_live_twilio_livekit_paid_outbound_and_sip_status_mapping(
    tmp_path: Path,
) -> None:
    if os.environ.get("VOICEY_LIVE_CONFIRM") != "I_ACKNOWLEDGE_PSTN_CHARGES":
        pytest.skip("VOICEY_LIVE_CONFIRM charge acknowledgement is absent")
    ledger = TelephonyLedger(tmp_path / "twilio-livekit-outbound.sqlite3")
    livekit = _livekit_api()
    dialer = LiveKitSipDialer(
        sip=livekit.sip,
        ledger=ledger,
        provider="twilio",
        trunk_id=_required("LIVEKIT_SIP_OUTBOUND_TRUNK"),
        timeout_s=60,
    )
    try:
        result = await dialer.dial(
            from_number=_required("VOICEY_TWILIO_LIVE_FROM"),
            to_number=_required("VOICEY_TWILIO_LIVE_TO"),
            room_name=_required("VOICEY_LIVEKIT_CERT_ROOM"),
            participant_identity="voicey-live-cert-callee",
            intent_id="intent_twilio_livekit_live_cert",
        )
        assert result.ended_reason is None
        assert result.sip_call_id
    finally:
        await livekit.aclose()
        ledger.close()


def test_live_twilio_livekit_completed_trunk_recording_correlation() -> None:
    backend = TwilioElasticSipBackend(
        Client(
            _required("TWILIO_ACCOUNT_SID"),
            _required("TWILIO_AUTH_TOKEN"),
        )
    )
    recording = backend.completed_trunk_recording(_required("VOICEY_TWILIO_LIVE_CALL_SID"))
    assert recording is not None
    assert recording.recording_sid.startswith("RE")
