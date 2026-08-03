"""Credentialed operator-managed generic SIP beta gates."""

from __future__ import annotations

import os
from pathlib import Path
from typing import cast

import pytest
from livekit import api

from voicey.runtimes.livekit.generic_sip import (
    GenericSipConfig,
    GenericSipProvisioner,
    SipMediaEncryption,
    SipTransport,
)
from voicey.runtimes.livekit.sip import LiveKitSipDialer
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


def _config() -> GenericSipConfig:
    allowed = tuple(
        value.strip()
        for value in os.environ.get("VOICEY_SIP_ALLOWED_ADDRESSES", "").split(",")
        if value.strip()
    )
    return GenericSipConfig(
        number=_required("VOICEY_SIP_LIVE_FROM"),
        agent_name=_required("VOICEY_LIVEKIT_AGENT_NAME"),
        outbound_address=_required("VOICEY_SIP_ADDRESS"),
        auth_username=_required("VOICEY_SIP_USERNAME"),
        auth_password=_required("VOICEY_SIP_PASSWORD"),
        allowed_addresses=allowed,
        transport=cast("SipTransport", _required("VOICEY_SIP_TRANSPORT").casefold()),
        media_encryption=cast(
            "SipMediaEncryption",
            _required("VOICEY_SIP_MEDIA_ENCRYPTION").casefold(),
        ),
    )


async def test_live_generic_sip_provision_reuse_and_rollback(tmp_path: Path) -> None:
    if os.environ.get("VOICEY_LIVE_ROUTE_CONFIRM") != "I_ACKNOWLEDGE_ROUTE_MUTATION":
        pytest.skip("VOICEY_LIVE_ROUTE_CONFIRM acknowledgement is absent")
    ledger = TelephonyLedger(tmp_path / "generic-sip-provision.sqlite3")
    livekit = _livekit_api()
    provisioner = GenericSipProvisioner(livekit=livekit.sip, ledger=ledger)
    first = None
    try:
        first = await provisioner.provision(_config())
        second = await provisioner.provision(_config())
        assert first.livekit_inbound_trunk_id == second.livekit_inbound_trunk_id
        assert first.livekit_outbound_trunk_id == second.livekit_outbound_trunk_id
        assert second.created_resources == 0
    finally:
        if first is not None:
            await provisioner.rollback(first.operation_id)
        await livekit.aclose()
        ledger.close()


async def test_live_generic_sip_paid_loopback_and_status_mapping(tmp_path: Path) -> None:
    if os.environ.get("VOICEY_LIVE_CONFIRM") != "I_ACKNOWLEDGE_PSTN_CHARGES":
        pytest.skip("VOICEY_LIVE_CONFIRM charge acknowledgement is absent")
    ledger = TelephonyLedger(tmp_path / "generic-sip-outbound.sqlite3")
    livekit = _livekit_api()
    dialer = LiveKitSipDialer(
        sip=livekit.sip,
        ledger=ledger,
        provider="sip",
        trunk_id=_required("LIVEKIT_SIP_OUTBOUND_TRUNK"),
        timeout_s=60,
    )
    try:
        result = await dialer.dial(
            from_number=_required("VOICEY_SIP_LIVE_FROM"),
            to_number=_required("VOICEY_SIP_LIVE_TO"),
            room_name=_required("VOICEY_LIVEKIT_CERT_ROOM"),
            participant_identity="voicey-generic-sip-cert-callee",
            intent_id="intent_generic_sip_live_cert",
        )
        assert result.ended_reason is None
        assert result.sip_call_id
    finally:
        await livekit.aclose()
        ledger.close()
