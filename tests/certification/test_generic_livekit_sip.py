"""Offline certification for the explicit LiveKit generic-SIP beta."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import pytest
from livekit.protocol import sip as lk_sip

from voicey.errors import VoiceyError
from voicey.runtimes.livekit.generic_sip import (
    GenericSipConfig,
    GenericSipProvisioner,
)
from voicey.runtimes.livekit.sip import LiveKitSipAPI
from voicey.telephony.ledger import TelephonyLedger

NUMBER = "+14155550100"


class FakeLiveKit:
    def __init__(self) -> None:
        self.inbound: list[lk_sip.SIPInboundTrunkInfo] = []
        self.outbound: list[lk_sip.SIPOutboundTrunkInfo] = []
        self.dispatch: list[lk_sip.SIPDispatchRuleInfo] = []
        self.deleted: list[tuple[str, str]] = []
        self.fail_create = False
        self.fail_delete = False

    async def list_inbound_trunk(
        self,
        _request: lk_sip.ListSIPInboundTrunkRequest,
    ) -> lk_sip.ListSIPInboundTrunkResponse:
        return lk_sip.ListSIPInboundTrunkResponse(items=self.inbound)

    async def create_inbound_trunk(
        self,
        request: lk_sip.CreateSIPInboundTrunkRequest,
    ) -> lk_sip.SIPInboundTrunkInfo:
        if self.fail_create:
            raise RuntimeError("unknown provider result")
        item = lk_sip.SIPInboundTrunkInfo()
        item.CopyFrom(request.trunk)
        item.sip_trunk_id = f"inbound-{len(self.inbound) + 1}"
        self.inbound.append(item)
        return item

    async def list_outbound_trunk(
        self,
        _request: lk_sip.ListSIPOutboundTrunkRequest,
    ) -> lk_sip.ListSIPOutboundTrunkResponse:
        return lk_sip.ListSIPOutboundTrunkResponse(items=self.outbound)

    async def create_outbound_trunk(
        self,
        request: lk_sip.CreateSIPOutboundTrunkRequest,
    ) -> lk_sip.SIPOutboundTrunkInfo:
        item = lk_sip.SIPOutboundTrunkInfo()
        item.CopyFrom(request.trunk)
        item.sip_trunk_id = f"outbound-{len(self.outbound) + 1}"
        self.outbound.append(item)
        return item

    async def list_dispatch_rule(
        self,
        _request: lk_sip.ListSIPDispatchRuleRequest,
    ) -> lk_sip.ListSIPDispatchRuleResponse:
        return lk_sip.ListSIPDispatchRuleResponse(items=self.dispatch)

    async def create_dispatch_rule(
        self,
        request: lk_sip.CreateSIPDispatchRuleRequest,
    ) -> lk_sip.SIPDispatchRuleInfo:
        item = lk_sip.SIPDispatchRuleInfo(
            sip_dispatch_rule_id=f"dispatch-{len(self.dispatch) + 1}",
            rule=request.rule,
            trunk_ids=request.trunk_ids,
            name=request.name,
            metadata=request.metadata,
            room_config=request.room_config,
        )
        self.dispatch.append(item)
        return item

    async def delete_dispatch_rule(
        self,
        request: lk_sip.DeleteSIPDispatchRuleRequest,
    ) -> lk_sip.SIPDispatchRuleInfo:
        if self.fail_delete:
            raise RuntimeError("delete failed")
        self.deleted.append(("dispatch", request.sip_dispatch_rule_id))
        item = next(
            item
            for item in self.dispatch
            if item.sip_dispatch_rule_id == request.sip_dispatch_rule_id
        )
        self.dispatch.remove(item)
        return item

    async def delete_trunk(
        self,
        request: lk_sip.DeleteSIPTrunkRequest,
    ) -> lk_sip.SIPTrunkInfo:
        self.deleted.append(("trunk", request.sip_trunk_id))
        for inbound in tuple(self.inbound):
            if inbound.sip_trunk_id == request.sip_trunk_id:
                self.inbound.remove(inbound)
                return lk_sip.SIPTrunkInfo(sip_trunk_id=request.sip_trunk_id)
        for outbound in tuple(self.outbound):
            if outbound.sip_trunk_id == request.sip_trunk_id:
                self.outbound.remove(outbound)
                return lk_sip.SIPTrunkInfo(sip_trunk_id=request.sip_trunk_id)
        return lk_sip.SIPTrunkInfo(sip_trunk_id=request.sip_trunk_id)


def _config(**values: object) -> GenericSipConfig:
    defaults: dict[str, object] = {
        "number": NUMBER,
        "agent_name": "booking",
        "outbound_address": "pbx.example.com:5061",
        "auth_username": "voiceyuser",
        "auth_password": "credential-secret",  # pragma: allowlist secret
        "allowed_addresses": ("203.0.113.0/24",),
        "transport": "tls",
        "media_encryption": "require",
    }
    return GenericSipConfig(**cast("Any", {**defaults, **values}))


async def test_provisions_explicit_secure_generic_sip_beta(tmp_path: Path) -> None:
    livekit = FakeLiveKit()
    ledger = TelephonyLedger(tmp_path / "generic-sip.sqlite3")
    provisioner = GenericSipProvisioner(
        livekit=cast("LiveKitSipAPI", livekit),
        ledger=ledger,
    )
    try:
        result = await provisioner.provision(_config())
        assert result.created_resources == 3
        assert ledger.get_provisioning(result.operation_id).state == "applied"
        inbound = livekit.inbound[0]
        outbound = livekit.outbound[0]
        dispatch = livekit.dispatch[0]
        assert inbound.numbers == [NUMBER]
        assert inbound.allowed_addresses == ["203.0.113.0/24"]
        assert inbound.auth_username == "voiceyuser"
        assert inbound.media_encryption == lk_sip.SIP_MEDIA_ENCRYPT_REQUIRE
        assert outbound.address == "pbx.example.com:5061"
        assert outbound.transport == lk_sip.SIP_TRANSPORT_TLS
        assert outbound.media_encryption == lk_sip.SIP_MEDIA_ENCRYPT_REQUIRE
        assert dispatch.room_config.agents[0].agent_name == "booking"
        assert json.loads(dispatch.room_config.agents[0].metadata)["tier"] == "beta"
    finally:
        ledger.close()


async def test_idempotent_adoption_and_reverse_rollback(tmp_path: Path) -> None:
    livekit = FakeLiveKit()
    ledger = TelephonyLedger(tmp_path / "generic-sip-idempotent.sqlite3")
    provisioner = GenericSipProvisioner(
        livekit=cast("LiveKitSipAPI", livekit),
        ledger=ledger,
    )
    try:
        first = await provisioner.provision(_config())
        second = await provisioner.provision(_config())
        assert first.created_resources == 3
        assert second.created_resources == 0
        assert len(livekit.inbound) == len(livekit.outbound) == len(livekit.dispatch) == 1
        rolled_back = await provisioner.rollback(first.operation_id)
        again = await provisioner.rollback(first.operation_id)
        assert rolled_back.state == again.state == "rolled_back"
        assert livekit.deleted == [
            ("trunk", "outbound-1"),
            ("dispatch", "dispatch-1"),
            ("trunk", "inbound-1"),
        ]
    finally:
        ledger.close()


async def test_drift_and_unknown_control_plane_outcome_stop(tmp_path: Path) -> None:
    livekit = FakeLiveKit()
    ledger = TelephonyLedger(tmp_path / "generic-sip-failure.sqlite3")
    provisioner = GenericSipProvisioner(
        livekit=cast("LiveKitSipAPI", livekit),
        ledger=ledger,
    )
    try:
        await provisioner.provision(_config())
        livekit.inbound[0].allowed_addresses[:] = ["198.51.100.1/32"]
        with pytest.raises(VoiceyError, match="differs"):
            await provisioner.provision(_config())

        other = FakeLiveKit()
        other.fail_create = True
        ambiguous = GenericSipProvisioner(
            livekit=cast("LiveKitSipAPI", other),
            ledger=ledger,
        )
        with pytest.raises(VoiceyError) as caught:
            await ambiguous.provision(_config(number="+14155550101"))
        assert caught.value.code == "VY-TEL-006"
        open_operations = ledger.open_provisioning(provider="sip-livekit")
        assert any(operation.state == "ambiguous" for operation in open_operations)
    finally:
        ledger.close()


async def test_rollback_conflict_is_durable(tmp_path: Path) -> None:
    livekit = FakeLiveKit()
    ledger = TelephonyLedger(tmp_path / "generic-sip-rollback.sqlite3")
    provisioner = GenericSipProvisioner(
        livekit=cast("LiveKitSipAPI", livekit),
        ledger=ledger,
    )
    try:
        result = await provisioner.provision(_config())
        livekit.fail_delete = True
        with pytest.raises(VoiceyError, match="VY-TEL-006"):
            await provisioner.rollback(result.operation_id)
        assert ledger.get_provisioning(result.operation_id).state == "conflict"
    finally:
        ledger.close()


@pytest.mark.parametrize(
    "values",
    [
        {"number": "not-e164"},
        {"agent_name": "Bad Name"},
        {"outbound_address": "sip:pbx.example.com"},
        {"outbound_address": "pbx.example.com:99999"},
        {"auth_username": ""},
        {"auth_password": "short"},  # pragma: allowlist secret
        {"allowed_addresses": ("not-cidr",)},
        {"allowed_addresses": ("203.0.113.1/32", "203.0.113.1/32")},
        {"room_prefix": ""},
        {"transport": "tls", "media_encryption": "disable"},
    ],
)
def test_config_rejects_unsafe_or_ambiguous_values(values: dict[str, object]) -> None:
    with pytest.raises(VoiceyError, match="VY-TEL-002"):
        _config(**values)


def test_config_fingerprint_redacts_password_and_tracks_security_mode() -> None:
    config = _config()
    changed = _config(media_encryption="allow")
    assert config.config_fingerprint != changed.config_fingerprint
    assert "credential-secret" not in repr(config)
