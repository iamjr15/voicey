"""Credentialed Plivo↔LiveKit SIP provisioning and paid-call beta gates."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from livekit import api

from voicekit.runtimes.livekit.plivo import (
    PlivoLiveKitSipConfig,
    PlivoLiveKitSipProvisioner,
    PlivoSipHTTPBackend,
)
from voicekit.runtimes.livekit.sip import LiveKitSipDialer
from voicekit.telephony.ledger import TelephonyLedger

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


def _config() -> PlivoLiveKitSipConfig:
    return PlivoLiveKitSipConfig(
        number=_required("VOICEKIT_PLIVO_LIVE_FROM"),
        agent_name=_required("VOICEKIT_LIVEKIT_AGENT_NAME"),
        livekit_sip_uri=_required("VOICEKIT_LIVEKIT_SIP_URI"),
        auth_username=_required("VOICEKIT_PLIVO_SIP_USERNAME"),
        auth_password=_required("VOICEKIT_PLIVO_SIP_PASSWORD"),
    )


async def test_live_plivo_livekit_provision_reuse_and_rollback(tmp_path: Path) -> None:
    if os.environ.get("VOICEKIT_LIVE_ROUTE_CONFIRM") != "I_ACKNOWLEDGE_ROUTE_MUTATION":
        pytest.skip("VOICEKIT_LIVE_ROUTE_CONFIRM acknowledgement is absent")
    ledger = TelephonyLedger(tmp_path / "plivo-livekit-provision.sqlite3")
    livekit = _livekit_api()
    provisioner = PlivoLiveKitSipProvisioner(
        livekit=livekit.sip,
        plivo=PlivoSipHTTPBackend(
            auth_id=_required("PLIVO_AUTH_ID"),
            auth_token=_required("PLIVO_AUTH_TOKEN"),
        ),
        ledger=ledger,
    )
    first = None
    try:
        first = await provisioner.provision(_config())
        second = await provisioner.provision(_config())
        assert first.livekit_inbound_trunk_id == second.livekit_inbound_trunk_id
        assert first.livekit_outbound_trunk_id == second.livekit_outbound_trunk_id
        assert first.plivo_inbound_trunk_id == second.plivo_inbound_trunk_id
        assert first.plivo_outbound_trunk_id == second.plivo_outbound_trunk_id
        assert second.created_resources == 0
    finally:
        if first is not None:
            await provisioner.rollback(first.operation_id)
        await livekit.aclose()
        ledger.close()


async def test_live_plivo_livekit_paid_outbound_and_status_mapping(tmp_path: Path) -> None:
    if os.environ.get("VOICEKIT_LIVE_CONFIRM") != "I_ACKNOWLEDGE_PSTN_CHARGES":
        pytest.skip("VOICEKIT_LIVE_CONFIRM charge acknowledgement is absent")
    ledger = TelephonyLedger(tmp_path / "plivo-livekit-outbound.sqlite3")
    livekit = _livekit_api()
    dialer = LiveKitSipDialer(
        sip=livekit.sip,
        ledger=ledger,
        provider="plivo",
        trunk_id=_required("LIVEKIT_SIP_OUTBOUND_TRUNK"),
        timeout_s=60,
    )
    try:
        result = await dialer.dial(
            from_number=_required("VOICEKIT_PLIVO_LIVE_FROM"),
            to_number=_required("VOICEKIT_PLIVO_LIVE_TO"),
            room_name=_required("VOICEKIT_LIVEKIT_CERT_ROOM"),
            participant_identity="voicekit-plivo-cert-callee",
            intent_id="intent_plivo_livekit_live_cert",
        )
        assert result.ended_reason is None
        assert result.sip_call_id
    finally:
        await livekit.aclose()
        ledger.close()
