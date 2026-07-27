from __future__ import annotations

from pathlib import Path

import pytest
from livekit.api import SipCallError
from livekit.protocol import sip as lk_sip

from voicekit.errors import VoicekitError
from voicekit.runtimes.livekit.sip import (
    LiveKitSipDialer,
    ManagedSipResource,
    TwilioLiveKitSipConfig,
    TwilioLiveKitSipProvisioner,
)
from voicekit.telephony.ledger import TelephonyLedger


class FakeLiveKitSip:
    def __init__(self) -> None:
        self.trunks: dict[str, lk_sip.SIPInboundTrunkInfo] = {}
        self.outbound_trunks: dict[str, lk_sip.SIPOutboundTrunkInfo] = {}
        self.rules: dict[str, lk_sip.SIPDispatchRuleInfo] = {}
        self.participant: lk_sip.SIPParticipantInfo | Exception = lk_sip.SIPParticipantInfo(
            participant_id="PA1",
            participant_identity="sip-callee",
            room_name="call-outbound",
            sip_call_id="sip-call-1",
        )
        self.deleted: list[tuple[str, str]] = []

    async def create_inbound_trunk(
        self,
        create: lk_sip.CreateSIPInboundTrunkRequest,
    ) -> lk_sip.SIPInboundTrunkInfo:
        trunk = lk_sip.SIPInboundTrunkInfo()
        trunk.CopyFrom(create.trunk)
        trunk.sip_trunk_id = f"ST{len(self.trunks) + 1}"
        self.trunks[trunk.sip_trunk_id] = trunk
        return trunk

    async def list_inbound_trunk(
        self,
        list: lk_sip.ListSIPInboundTrunkRequest,
    ) -> lk_sip.ListSIPInboundTrunkResponse:
        items = [
            item
            for item in self.trunks.values()
            if not list.numbers or any(number in item.numbers for number in list.numbers)
        ]
        return lk_sip.ListSIPInboundTrunkResponse(items=items)

    async def delete_sip_trunk(
        self,
        delete: lk_sip.DeleteSIPTrunkRequest,
    ) -> lk_sip.SIPTrunkInfo:
        self.trunks.pop(delete.sip_trunk_id, None)
        self.outbound_trunks.pop(delete.sip_trunk_id, None)
        self.deleted.append(("trunk", delete.sip_trunk_id))
        return lk_sip.SIPTrunkInfo(sip_trunk_id=delete.sip_trunk_id)

    async def create_outbound_trunk(
        self,
        create: lk_sip.CreateSIPOutboundTrunkRequest,
    ) -> lk_sip.SIPOutboundTrunkInfo:
        trunk = lk_sip.SIPOutboundTrunkInfo()
        trunk.CopyFrom(create.trunk)
        trunk.sip_trunk_id = f"SOT{len(self.outbound_trunks) + 1}"
        self.outbound_trunks[trunk.sip_trunk_id] = trunk
        return trunk

    async def list_outbound_trunk(
        self,
        list: lk_sip.ListSIPOutboundTrunkRequest,
    ) -> lk_sip.ListSIPOutboundTrunkResponse:
        items = [
            item
            for item in self.outbound_trunks.values()
            if not list.numbers or any(number in item.numbers for number in list.numbers)
        ]
        return lk_sip.ListSIPOutboundTrunkResponse(items=items)

    async def create_dispatch_rule(
        self,
        create: lk_sip.CreateSIPDispatchRuleRequest,
    ) -> lk_sip.SIPDispatchRuleInfo:
        rule = lk_sip.SIPDispatchRuleInfo(
            sip_dispatch_rule_id=f"SDR{len(self.rules) + 1}",
            rule=create.rule,
            trunk_ids=create.trunk_ids,
            name=create.name,
            metadata=create.metadata,
            room_config=create.room_config,
        )
        self.rules[rule.sip_dispatch_rule_id] = rule
        return rule

    async def list_dispatch_rule(
        self,
        list: lk_sip.ListSIPDispatchRuleRequest,
    ) -> lk_sip.ListSIPDispatchRuleResponse:
        items = [
            item
            for item in self.rules.values()
            if not list.trunk_ids or any(trunk_id in item.trunk_ids for trunk_id in list.trunk_ids)
        ]
        return lk_sip.ListSIPDispatchRuleResponse(items=items)

    async def delete_dispatch_rule(
        self,
        delete: lk_sip.DeleteSIPDispatchRuleRequest,
    ) -> lk_sip.SIPDispatchRuleInfo:
        item = self.rules.pop(delete.sip_dispatch_rule_id)
        self.deleted.append(("dispatch", delete.sip_dispatch_rule_id))
        return item

    async def create_sip_participant(
        self,
        create: lk_sip.CreateSIPParticipantRequest,
        *,
        timeout: float | None = None,  # noqa: ASYNC109 - SDK parity fake
        trunk_id: str | None = None,
        outbound_trunk_config: lk_sip.SIPOutboundConfig | None = None,
    ) -> lk_sip.SIPParticipantInfo:
        del create, timeout, trunk_id, outbound_trunk_config
        if isinstance(self.participant, Exception):
            raise self.participant
        return self.participant


class FakeTwilioSip:
    def __init__(self) -> None:
        self.state: dict[str, str] = {}
        self.deleted: list[str] = []
        self.restored = False
        self.origination_uri: str | None = None
        self.recording_enabled: bool | None = None
        self.fail_kind: str | None = None
        self.unknown_failure = False

    def snapshot_number(self, number: str) -> dict[str, object]:
        return {
            "number": number,
            "number_sid": "PN1",
            "route": {"trunk_sid": None, "voice_url": "https://old.example.test"},
        }

    def ensure_trunk(self, *, name: str, domain_name: str) -> ManagedSipResource:
        del name, domain_name
        return self._ensure("twilio_trunk", "TK1")

    def ensure_origination(
        self,
        *,
        trunk_sid: str,
        name: str,
        sip_uri: str,
    ) -> ManagedSipResource:
        del name
        self.origination_uri = sip_uri
        return self._ensure("twilio_origination", "OU1", trunk_sid)

    def ensure_recording(
        self,
        *,
        trunk_sid: str,
        enabled: bool,
        allow_update: bool,
    ) -> None:
        del trunk_sid, allow_update
        self.recording_enabled = enabled
        self.state["twilio_recording"] = "enabled" if enabled else "disabled"

    def ensure_credential_list(self, *, name: str) -> ManagedSipResource:
        del name
        return self._ensure("twilio_credential_list", "CL1")

    def ensure_credential(
        self,
        *,
        credential_list_sid: str,
        username: str,
        password: str,
    ) -> ManagedSipResource:
        del username, password
        return self._ensure("twilio_credential", "CR1", credential_list_sid)

    def ensure_credential_binding(
        self,
        *,
        trunk_sid: str,
        credential_list_sid: str,
    ) -> ManagedSipResource:
        del credential_list_sid
        return self._ensure("twilio_credential_binding", "CLB1", trunk_sid)

    def attach_number(self, *, trunk_sid: str, number: str) -> ManagedSipResource:
        del number
        return self._ensure("twilio_number_binding", "PN1", trunk_sid)

    def restore_number(self, snapshot: dict[str, object]) -> None:
        assert snapshot["number_sid"] == "PN1"
        self.restored = True
        self.state.pop("twilio_number_binding", None)

    def delete_resource(self, resource: ManagedSipResource) -> None:
        self.deleted.append(resource.kind)
        self.state.pop(resource.kind, None)

    def _ensure(
        self,
        kind: str,
        resource_id: str,
        parent_id: str | None = None,
    ) -> ManagedSipResource:
        if self.fail_kind == kind:
            if self.unknown_failure:
                raise ConnectionError("unknown carrier outcome")
            raise VoicekitError("VK-TEL-004", detail=f"{kind} rejected.")
        created = kind not in self.state
        self.state[kind] = resource_id
        return ManagedSipResource(kind, resource_id, created, parent_id)


def _config() -> TwilioLiveKitSipConfig:
    return TwilioLiveKitSipConfig(
        number="+14155550100",
        agent_name="appointment-agent",
        livekit_sip_uri="sip:project.sip.livekit.cloud",
        twilio_domain_name="voicekit-example.pstn.twilio.com",
        auth_username="voicekit-user",
        auth_password="long-random-password",  # pragma: allowlist secret
        record=True,
    )


@pytest.mark.asyncio
async def test_twilio_livekit_provisioning_is_idempotent_and_ledgered(
    tmp_path: Path,
) -> None:
    ledger = TelephonyLedger(tmp_path / "telephony.sqlite3")
    livekit = FakeLiveKitSip()
    twilio = FakeTwilioSip()
    provisioner = TwilioLiveKitSipProvisioner(
        livekit=livekit,
        twilio=twilio,
        ledger=ledger,
    )

    first = await provisioner.provision(_config())
    second = await provisioner.provision(_config())

    assert first.created_resources == 9
    assert second.created_resources == 0
    assert first.livekit_inbound_trunk_id == second.livekit_inbound_trunk_id == "ST1"
    assert first.livekit_outbound_trunk_id == second.livekit_outbound_trunk_id == "SOT1"
    assert first.livekit_dispatch_rule_id == second.livekit_dispatch_rule_id == "SDR1"
    inbound = livekit.trunks[first.livekit_inbound_trunk_id]
    assert list(inbound.numbers) == [_config().number]
    assert inbound.auth_username == ""
    assert inbound.auth_password == ""
    assert inbound.media_encryption == lk_sip.SIP_MEDIA_ENCRYPT_ALLOW
    outbound = livekit.outbound_trunks[first.livekit_outbound_trunk_id]
    assert outbound.address == _config().twilio_domain_name
    assert outbound.auth_username == _config().auth_username
    assert outbound.auth_password == _config().auth_password
    assert outbound.transport == lk_sip.SIP_TRANSPORT_TLS
    assert outbound.media_encryption == lk_sip.SIP_MEDIA_ENCRYPT_ALLOW
    assert twilio.origination_uri == f"{_config().livekit_sip_uri};transport=tls"
    assert twilio.recording_enabled is True
    assert ledger.get_provisioning(first.operation_id).state == "applied"
    assert ledger.get_provisioning(second.operation_id).resources == ()
    ledger.close()


@pytest.mark.asyncio
async def test_twilio_livekit_provisioning_rolls_back_definitive_failure(
    tmp_path: Path,
) -> None:
    ledger = TelephonyLedger(tmp_path / "telephony.sqlite3")
    livekit = FakeLiveKitSip()
    twilio = FakeTwilioSip()
    twilio.fail_kind = "twilio_credential"
    provisioner = TwilioLiveKitSipProvisioner(
        livekit=livekit,
        twilio=twilio,
        ledger=ledger,
    )

    with pytest.raises(VoicekitError) as caught:
        await provisioner.provision(_config())

    assert caught.value.code == "VK-TEL-004"
    records = ledger.provisioning_records(provider="twilio-livekit")
    assert len(records) == 1
    assert records[0].state == "rolled_back"
    assert livekit.trunks == {}
    assert livekit.outbound_trunks == {}
    assert livekit.rules == {}
    assert twilio.deleted == [
        "twilio_credential_list",
        "twilio_origination",
        "twilio_trunk",
    ]
    ledger.close()


@pytest.mark.asyncio
async def test_twilio_livekit_unknown_failure_stays_ambiguous_without_deleting(
    tmp_path: Path,
) -> None:
    ledger = TelephonyLedger(tmp_path / "telephony.sqlite3")
    livekit = FakeLiveKitSip()
    twilio = FakeTwilioSip()
    twilio.fail_kind = "twilio_credential"
    twilio.unknown_failure = True
    provisioner = TwilioLiveKitSipProvisioner(
        livekit=livekit,
        twilio=twilio,
        ledger=ledger,
    )

    with pytest.raises(VoicekitError) as caught:
        await provisioner.provision(_config())

    assert caught.value.code == "VK-TEL-006"
    open_operations = ledger.open_provisioning(provider="twilio-livekit")
    assert len(open_operations) == 1
    assert open_operations[0].state == "ambiguous"
    assert livekit.trunks
    assert twilio.deleted == []
    ledger.close()


@pytest.mark.asyncio
async def test_livekit_sip_dialer_maps_success_rejection_and_ambiguity(
    tmp_path: Path,
) -> None:
    ledger = TelephonyLedger(tmp_path / "telephony.sqlite3")
    livekit = FakeLiveKitSip()
    dialer = LiveKitSipDialer(
        sip=livekit,
        ledger=ledger,
        provider="twilio",
        trunk_id="ST-outbound",
    )
    success = await dialer.dial(
        from_number="+14155550100",
        to_number="+14155550101",
        room_name="call-outbound",
        participant_identity="sip-callee",
        intent_id="intent_success",
    )
    assert success.sip_call_id == "sip-call-1"
    assert ledger.get_intent("intent_success").state == "submitted"

    livekit.participant = SipCallError(
        "sip_error",
        "busy",
        status=409,
        metadata={"sip_status_code": "486", "sip_status": "Busy Here"},
    )
    rejected = await dialer.dial(
        from_number="+14155550100",
        to_number="+14155550102",
        room_name="call-rejected",
        participant_identity="sip-rejected",
        intent_id="intent_rejected",
    )
    assert rejected.ended_reason == "carrier_error"
    assert rejected.sip_status_code == 486
    assert ledger.get_intent("intent_rejected").state == "rejected"

    livekit.participant = ConnectionError("unknown")
    with pytest.raises(VoicekitError) as ambiguous:
        await dialer.dial(
            from_number="+14155550100",
            to_number="+14155550103",
            room_name="call-ambiguous",
            participant_identity="sip-ambiguous",
            intent_id="intent_ambiguous",
        )
    assert ambiguous.value.code == "VK-TEL-007"
    assert ledger.get_intent("intent_ambiguous").state == "ambiguous"
    ledger.close()
