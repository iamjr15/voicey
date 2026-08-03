"""Idempotent LiveKit/Twilio SIP provisioning and outbound call control."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import uuid
from dataclasses import dataclass, field
from typing import Any, Protocol, cast
from urllib.parse import urlsplit

from livekit.api import SipCallError
from livekit.protocol import agent_dispatch as lk_dispatch
from livekit.protocol import room as lk_room
from livekit.protocol import sip as lk_sip

from voicey.errors import VoiceyError
from voicey.storage.artifacts import ArtifactStore
from voicey.storage.models import EndedReason, RecordingReady
from voicey.storage.repository import StorageRepository
from voicey.telephony.ledger import (
    ProvisioningRecord,
    TelephonyLedger,
)
from voicey.telephony.models import validate_e164

_SAFE_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
_TWILIO_CALL_SID = re.compile(r"^CA[0-9a-fA-F]{32}$")
_TWILIO_RECORDING_SID = re.compile(r"^RE[0-9a-fA-F]{32}$")
_TWILIO_ROUTE_FIELDS = (
    "voice_url",
    "voice_method",
    "voice_fallback_url",
    "voice_fallback_method",
    "status_callback",
    "status_callback_method",
    "voice_application_sid",
    "trunk_sid",
)


class LiveKitSipAPI(Protocol):
    """Installed livekit-api 1.2.0 SIP methods used by the provisioner."""

    async def create_inbound_trunk(
        self,
        create: lk_sip.CreateSIPInboundTrunkRequest,
    ) -> lk_sip.SIPInboundTrunkInfo: ...

    async def list_inbound_trunk(
        self,
        list: lk_sip.ListSIPInboundTrunkRequest,
    ) -> lk_sip.ListSIPInboundTrunkResponse: ...

    async def delete_trunk(
        self,
        delete: lk_sip.DeleteSIPTrunkRequest,
    ) -> lk_sip.SIPTrunkInfo: ...

    async def create_outbound_trunk(
        self,
        create: lk_sip.CreateSIPOutboundTrunkRequest,
    ) -> lk_sip.SIPOutboundTrunkInfo: ...

    async def list_outbound_trunk(
        self,
        list: lk_sip.ListSIPOutboundTrunkRequest,
    ) -> lk_sip.ListSIPOutboundTrunkResponse: ...

    async def create_dispatch_rule(
        self,
        create: lk_sip.CreateSIPDispatchRuleRequest,
    ) -> lk_sip.SIPDispatchRuleInfo: ...

    async def list_dispatch_rule(
        self,
        list: lk_sip.ListSIPDispatchRuleRequest,
    ) -> lk_sip.ListSIPDispatchRuleResponse: ...

    async def delete_dispatch_rule(
        self,
        delete: lk_sip.DeleteSIPDispatchRuleRequest,
    ) -> lk_sip.SIPDispatchRuleInfo: ...

    async def create_sip_participant(
        self,
        create: lk_sip.CreateSIPParticipantRequest,
        *,
        timeout: float | None = None,  # noqa: ASYNC109 - installed SDK parameter
        trunk_id: str | None = None,
        outbound_trunk_config: lk_sip.SIPOutboundConfig | None = None,
    ) -> lk_sip.SIPParticipantInfo: ...


@dataclass(frozen=True, slots=True)
class ManagedSipResource:
    kind: str
    resource_id: str
    created: bool
    parent_id: str | None = None

    def wire(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "resource_id": self.resource_id,
            "created": self.created,
            "parent_id": self.parent_id,
        }


class TwilioSipBackend(Protocol):
    """High-level Twilio Elastic SIP operations with idempotent ensure semantics."""

    def snapshot_number(self, number: str) -> dict[str, object]: ...

    def ensure_trunk(self, *, name: str, domain_name: str) -> ManagedSipResource: ...

    def ensure_origination(
        self,
        *,
        trunk_sid: str,
        name: str,
        sip_uri: str,
    ) -> ManagedSipResource: ...

    def ensure_recording(
        self,
        *,
        trunk_sid: str,
        enabled: bool,
        allow_update: bool,
    ) -> None: ...

    def ensure_credential_list(self, *, name: str) -> ManagedSipResource: ...

    def ensure_credential(
        self,
        *,
        credential_list_sid: str,
        username: str,
        password: str,
    ) -> ManagedSipResource: ...

    def ensure_credential_binding(
        self,
        *,
        trunk_sid: str,
        credential_list_sid: str,
    ) -> ManagedSipResource: ...

    def attach_number(self, *, trunk_sid: str, number: str) -> ManagedSipResource: ...

    def restore_number(self, snapshot: dict[str, object]) -> None: ...

    def delete_resource(self, resource: ManagedSipResource) -> None: ...


class TwilioRecordingDownloader(Protocol):
    """Existing authenticated Twilio media ingestion surface."""

    async def download_recording(
        self,
        recording_sid: str,
        *,
        artifact_store: ArtifactStore,
        storage_key: str,
        max_bytes: int = 100 * 1024 * 1024,
    ) -> str: ...


@dataclass(frozen=True, slots=True)
class TwilioLiveKitSipConfig:
    """All inputs for one deterministic, resumable Twilio↔LiveKit route."""

    number: str
    agent_name: str
    livekit_sip_uri: str
    twilio_domain_name: str
    auth_username: str
    auth_password: str = field(repr=False)
    resource_prefix: str = "voicey"
    room_prefix: str = "call-"
    record: bool = False

    def __post_init__(self) -> None:
        validate_e164(self.number)
        if not re.fullmatch(
            r"^sip:[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?(?::[0-9]{1,5})?$",
            self.livekit_sip_uri,
        ):
            raise VoiceyError(
                "VY-TEL-002",
                detail="LiveKit SIP URI must be sip:host with no embedded credentials.",
            )
        if not re.fullmatch(
            r"^[a-z0-9][a-z0-9.-]+$", self.twilio_domain_name
        ) or not self.twilio_domain_name.endswith(".pstn.twilio.com"):
            raise VoiceyError("VY-TEL-002", detail="Twilio SIP domain name is invalid.")
        if not _SAFE_NAME.fullmatch(self.agent_name) or not _SAFE_NAME.fullmatch(
            self.resource_prefix
        ):
            raise VoiceyError(
                "VY-TEL-002",
                detail="LiveKit agent and SIP resource names must be lowercase slug values.",
            )
        if not self.auth_username or not self.auth_password:
            raise VoiceyError("VY-TEL-002", detail="SIP username and password are required.")
        if (
            len(self.auth_password) < 12
            or re.search(r"[a-z]", self.auth_password) is None
            or re.search(r"[A-Z]", self.auth_password) is None
            or re.search(r"[0-9]", self.auth_password) is None
        ):
            raise VoiceyError(
                "VY-TEL-002",
                detail=(
                    "Twilio SIP password must contain at least 12 characters, "
                    "one lowercase letter, one uppercase letter, and one number."
                ),
            )
        if not self.room_prefix or len(self.room_prefix) > 32:
            raise VoiceyError("VY-TEL-002", detail="SIP room prefix must contain 1-32 chars.")

    @property
    def base_name(self) -> str:
        digits = self.number.removeprefix("+")
        return f"{self.resource_prefix}-{self.agent_name}-{digits}"

    @property
    def config_fingerprint(self) -> str:
        wire = {
            "number": self.number,
            "agent_name": self.agent_name,
            "livekit_sip_uri": self.livekit_sip_uri,
            "twilio_domain_name": self.twilio_domain_name,
            "auth_username": self.auth_username,
            "auth_password_sha256": hashlib.sha256(self.auth_password.encode()).hexdigest(),
            "room_prefix": self.room_prefix,
            "record": self.record,
        }
        encoded = json.dumps(wire, sort_keys=True, separators=(",", ":")).encode()
        return f"sha256:{hashlib.sha256(encoded).hexdigest()}"

    @property
    def origination_uri(self) -> str:
        """Use TLS signaling to match the secure Twilio trunk setting."""
        return f"{self.livekit_sip_uri};transport=tls"


@dataclass(frozen=True, slots=True)
class SipProvisioningResult:
    operation_id: str
    livekit_inbound_trunk_id: str
    livekit_outbound_trunk_id: str
    livekit_dispatch_rule_id: str
    twilio_trunk_sid: str
    created_resources: int


class TwilioLiveKitSipProvisioner:
    """Provision both sides as one ledgered operation with reverse rollback."""

    provider = "twilio-livekit"

    def __init__(
        self,
        *,
        livekit: LiveKitSipAPI,
        twilio: TwilioSipBackend,
        ledger: TelephonyLedger,
    ) -> None:
        self.livekit = livekit
        self.twilio = twilio
        self.ledger = ledger

    async def provision(self, config: TwilioLiveKitSipConfig) -> SipProvisioningResult:
        snapshot = self.twilio.snapshot_number(config.number)
        operation = self.ledger.prepare_provisioning(
            provider=self.provider,
            number=config.number,
            snapshot=snapshot,
            planned={
                "name": config.base_name,
                "config_fingerprint": config.config_fingerprint,
                "livekit_sip_uri": config.origination_uri,
                "twilio_domain_name": config.twilio_domain_name,
            },
        )
        try:
            livekit_trunk = await self._ensure_livekit_trunk(config)
            operation = self._record(operation, livekit_trunk)
            dispatch = await self._ensure_dispatch(config, livekit_trunk.resource_id)
            operation = self._record(operation, dispatch)
            outbound = await self._ensure_livekit_outbound(config)
            operation = self._record(operation, outbound)
            twilio_trunk = self.twilio.ensure_trunk(
                name=config.base_name,
                domain_name=config.twilio_domain_name,
            )
            operation = self._record(operation, twilio_trunk)
            self.twilio.ensure_recording(
                trunk_sid=twilio_trunk.resource_id,
                enabled=config.record,
                allow_update=twilio_trunk.created,
            )
            operation = self._record(
                operation,
                self.twilio.ensure_origination(
                    trunk_sid=twilio_trunk.resource_id,
                    name=config.base_name,
                    sip_uri=config.origination_uri,
                ),
            )
            credential_list = self.twilio.ensure_credential_list(name=config.base_name)
            operation = self._record(operation, credential_list)
            operation = self._record(
                operation,
                self.twilio.ensure_credential(
                    credential_list_sid=credential_list.resource_id,
                    username=config.auth_username,
                    password=config.auth_password,
                ),
            )
            operation = self._record(
                operation,
                self.twilio.ensure_credential_binding(
                    trunk_sid=twilio_trunk.resource_id,
                    credential_list_sid=credential_list.resource_id,
                ),
            )
            operation = self._record(
                operation,
                self.twilio.attach_number(
                    trunk_sid=twilio_trunk.resource_id,
                    number=config.number,
                ),
            )
            operation = self.ledger.transition_provisioning(
                operation.operation_id,
                expected=("prepared", "applying"),
                state="applied",
            )
        except VoiceyError:
            await self._rollback_after_definitive_failure(operation.operation_id)
            raise
        except Exception as exc:
            self.ledger.transition_provisioning(
                operation.operation_id,
                expected=("prepared", "applying"),
                state="ambiguous",
            )
            raise VoiceyError(
                "VY-TEL-006",
                detail=(
                    f"SIP provisioning outcome is ambiguous for "
                    f"{operation.operation_id!r}; reconcile before retry."
                ),
            ) from exc
        return SipProvisioningResult(
            operation_id=operation.operation_id,
            livekit_inbound_trunk_id=livekit_trunk.resource_id,
            livekit_outbound_trunk_id=outbound.resource_id,
            livekit_dispatch_rule_id=dispatch.resource_id,
            twilio_trunk_sid=twilio_trunk.resource_id,
            created_resources=sum(
                bool(resource.get("created")) for resource in operation.resources
            ),
        )

    async def rollback(self, operation_id: str) -> ProvisioningRecord:
        operation = self.ledger.get_provisioning(operation_id)
        if operation.provider != self.provider:
            raise VoiceyError("VY-TEL-006", detail="provisioning token has another provider.")
        if operation.state == "rolled_back":
            return operation
        operation = self.ledger.transition_provisioning(
            operation_id,
            expected=("prepared", "applying", "applied", "ambiguous", "failed"),
            state="rolling_back",
        )
        try:
            for wire in reversed(operation.resources):
                resource = _resource(wire)
                if not resource.created:
                    continue
                if resource.kind == "livekit_dispatch_rule":
                    await self.livekit.delete_dispatch_rule(
                        lk_sip.DeleteSIPDispatchRuleRequest(
                            sip_dispatch_rule_id=resource.resource_id
                        )
                    )
                elif resource.kind in {
                    "livekit_inbound_trunk",
                    "livekit_outbound_trunk",
                }:
                    await self.livekit.delete_trunk(
                        lk_sip.DeleteSIPTrunkRequest(sip_trunk_id=resource.resource_id)
                    )
                elif resource.kind == "twilio_number_binding":
                    self.twilio.restore_number(operation.snapshot)
                else:
                    self.twilio.delete_resource(resource)
        except Exception as exc:
            self.ledger.transition_provisioning(
                operation_id,
                expected=("rolling_back",),
                state="conflict",
            )
            raise VoiceyError(
                "VY-TEL-006",
                detail=f"SIP rollback conflicted for {operation_id!r}.",
            ) from exc
        return self.ledger.transition_provisioning(
            operation_id,
            expected=("rolling_back",),
            state="rolled_back",
        )

    async def _rollback_after_definitive_failure(self, operation_id: str) -> None:
        try:
            await self.rollback(operation_id)
        except VoiceyError:
            raise

    def _record(
        self,
        operation: ProvisioningRecord,
        resource: ManagedSipResource,
    ) -> ProvisioningRecord:
        if not resource.created:
            return operation
        return self.ledger.append_provisioned_resource(
            operation.operation_id,
            resource=resource.wire(),
        )

    async def _ensure_livekit_trunk(
        self,
        config: TwilioLiveKitSipConfig,
    ) -> ManagedSipResource:
        response = await self.livekit.list_inbound_trunk(
            lk_sip.ListSIPInboundTrunkRequest(numbers=[config.number])
        )
        matching = [item for item in response.items if item.name == config.base_name]
        if len(matching) > 1:
            raise VoiceyError("VY-TEL-006", detail="duplicate managed LiveKit SIP trunks.")
        metadata = _managed_metadata(config)
        if matching:
            item = matching[0]
            if (
                list(item.numbers) != [config.number]
                or item.auth_username
                or item.auth_password
                or item.metadata != metadata
                or item.media_encryption != lk_sip.SIP_MEDIA_ENCRYPT_ALLOW
            ):
                raise VoiceyError(
                    "VY-TEL-006",
                    detail="managed LiveKit inbound trunk differs from desired config.",
                )
            return ManagedSipResource(
                "livekit_inbound_trunk",
                item.sip_trunk_id,
                False,
            )
        created = await self.livekit.create_inbound_trunk(
            lk_sip.CreateSIPInboundTrunkRequest(
                trunk=lk_sip.SIPInboundTrunkInfo(
                    name=config.base_name,
                    metadata=metadata,
                    numbers=[config.number],
                    media_encryption=lk_sip.SIP_MEDIA_ENCRYPT_ALLOW,
                )
            )
        )
        return ManagedSipResource(
            "livekit_inbound_trunk",
            created.sip_trunk_id,
            True,
        )

    async def _ensure_livekit_outbound(
        self,
        config: TwilioLiveKitSipConfig,
    ) -> ManagedSipResource:
        response = await self.livekit.list_outbound_trunk(
            lk_sip.ListSIPOutboundTrunkRequest(numbers=[config.number])
        )
        matching = [item for item in response.items if item.name == config.base_name]
        if len(matching) > 1:
            raise VoiceyError("VY-TEL-006", detail="duplicate managed LiveKit outbound trunks.")
        metadata = _managed_metadata(config)
        if matching:
            item = matching[0]
            if (
                list(item.numbers) != [config.number]
                or item.address != config.twilio_domain_name
                or item.auth_username != config.auth_username
                or item.metadata != metadata
                or item.transport != lk_sip.SIP_TRANSPORT_TLS
                or item.media_encryption != lk_sip.SIP_MEDIA_ENCRYPT_ALLOW
            ):
                raise VoiceyError(
                    "VY-TEL-006",
                    detail="managed LiveKit outbound trunk differs from desired config.",
                )
            return ManagedSipResource(
                "livekit_outbound_trunk",
                item.sip_trunk_id,
                False,
            )
        created = await self.livekit.create_outbound_trunk(
            lk_sip.CreateSIPOutboundTrunkRequest(
                trunk=lk_sip.SIPOutboundTrunkInfo(
                    name=config.base_name,
                    metadata=metadata,
                    address=config.twilio_domain_name,
                    numbers=[config.number],
                    auth_username=config.auth_username,
                    auth_password=config.auth_password,
                    transport=lk_sip.SIP_TRANSPORT_TLS,
                    media_encryption=lk_sip.SIP_MEDIA_ENCRYPT_ALLOW,
                )
            )
        )
        return ManagedSipResource(
            "livekit_outbound_trunk",
            created.sip_trunk_id,
            True,
        )

    async def _ensure_dispatch(
        self,
        config: TwilioLiveKitSipConfig,
        trunk_id: str,
    ) -> ManagedSipResource:
        response = await self.livekit.list_dispatch_rule(
            lk_sip.ListSIPDispatchRuleRequest(trunk_ids=[trunk_id])
        )
        matching = [item for item in response.items if item.name == config.base_name]
        if len(matching) > 1:
            raise VoiceyError("VY-TEL-006", detail="duplicate managed LiveKit dispatch rules.")
        metadata = _managed_metadata(config)
        if matching:
            item = matching[0]
            individual = item.rule.dispatch_rule_individual
            agents = list(item.room_config.agents)
            if (
                list(item.trunk_ids) != [trunk_id]
                or individual.room_prefix != config.room_prefix
                or item.metadata != metadata
                or len(agents) != 1
                or agents[0].agent_name != config.agent_name
            ):
                raise VoiceyError(
                    "VY-TEL-006",
                    detail="managed LiveKit dispatch rule differs from desired config.",
                )
            return ManagedSipResource(
                "livekit_dispatch_rule",
                item.sip_dispatch_rule_id,
                False,
            )
        created = await self.livekit.create_dispatch_rule(
            lk_sip.CreateSIPDispatchRuleRequest(
                rule=lk_sip.SIPDispatchRule(
                    dispatch_rule_individual=lk_sip.SIPDispatchRuleIndividual(
                        room_prefix=config.room_prefix
                    )
                ),
                trunk_ids=[trunk_id],
                name=config.base_name,
                metadata=metadata,
                room_config=lk_room.RoomConfiguration(
                    agents=[
                        lk_dispatch.RoomAgentDispatch(
                            agent_name=config.agent_name,
                            metadata=json.dumps(
                                {
                                    "channel": "phone",
                                    "direction": "inbound",
                                    "provider": "twilio",
                                },
                                sort_keys=True,
                                separators=(",", ":"),
                            ),
                        )
                    ]
                ),
            )
        )
        return ManagedSipResource(
            "livekit_dispatch_rule",
            created.sip_dispatch_rule_id,
            True,
        )


@dataclass(frozen=True, slots=True)
class OutboundSipCall:
    intent_id: str
    participant_identity: str | None
    sip_call_id: str | None
    ended_reason: EndedReason | None
    sip_status_code: int | None = None


@dataclass(frozen=True, slots=True)
class TwilioTrunkRecording:
    """One completed Core Recording correlated from a LiveKit SIP participant."""

    recording_sid: str
    call_sid: str
    duration_s: int | None


class LiveKitSipDialer:
    """Durably intent-led outbound SIP participant creation."""

    def __init__(
        self,
        *,
        sip: LiveKitSipAPI,
        ledger: TelephonyLedger,
        provider: str,
        trunk_id: str | None = None,
        outbound_config: lk_sip.SIPOutboundConfig | None = None,
        timeout_s: float = 30.0,
    ) -> None:
        if (trunk_id is None) == (outbound_config is None):
            raise VoiceyError(
                "VY-TEL-002",
                detail="configure exactly one LiveKit outbound trunk id or inline SIP config.",
            )
        self.sip = sip
        self.ledger = ledger
        self.provider = provider
        self.trunk_id = trunk_id
        self.outbound_config = outbound_config
        self.timeout_s = timeout_s

    async def dial(
        self,
        *,
        from_number: str,
        to_number: str,
        room_name: str,
        participant_identity: str,
        intent_id: str | None = None,
    ) -> OutboundSipCall:
        validate_e164(from_number)
        validate_e164(to_number)
        stable_intent = intent_id or f"intent_{uuid.uuid4().hex}"
        self.ledger.prepare_intent(
            intent_id=stable_intent,
            provider=self.provider,
            from_number=from_number,
            to_number=to_number,
            target={"runtime": "livekit", "room_name": room_name},
        )
        request = lk_sip.CreateSIPParticipantRequest(
            sip_trunk_id=self.trunk_id or "",
            sip_call_to=to_number,
            sip_number=from_number,
            room_name=room_name,
            participant_identity=participant_identity,
            participant_metadata=json.dumps(
                {
                    "call_id": stable_intent,
                    "channel": "phone",
                    "direction": "outbound",
                    "provider": self.provider,
                },
                sort_keys=True,
                separators=(",", ":"),
            ),
            wait_until_answered=True,
        )
        try:
            participant = await self.sip.create_sip_participant(
                request,
                timeout=self.timeout_s,
                trunk_id=self.trunk_id,
                outbound_trunk_config=self.outbound_config,
            )
        except SipCallError as exc:
            status = exc.sip_status_code
            self.ledger.transition_intent(
                stable_intent,
                expected=("prepared",),
                state="rejected",
                last_status=f"sip_{status or 'unknown'}",
            )
            return OutboundSipCall(
                intent_id=stable_intent,
                participant_identity=None,
                sip_call_id=None,
                ended_reason="carrier_error",
                sip_status_code=status,
            )
        except Exception as exc:
            self.ledger.transition_intent(
                stable_intent,
                expected=("prepared",),
                state="ambiguous",
                last_status="livekit_sip_create_ambiguous",
            )
            raise VoiceyError(
                "VY-TEL-007",
                detail=f"outbound SIP intent {stable_intent!r} is ambiguous.",
            ) from exc
        self.ledger.transition_intent(
            stable_intent,
            expected=("prepared",),
            state="submitted",
            provider_call_id=participant.sip_call_id or participant.participant_id,
            last_status="answered",
        )
        return OutboundSipCall(
            intent_id=stable_intent,
            participant_identity=participant.participant_identity,
            sip_call_id=participant.sip_call_id,
            ended_reason=None,
        )


class TwilioElasticSipBackend:
    """Installed Twilio 9.10.9 Elastic SIP implementation."""

    def __init__(self, client: Any) -> None:
        self._client = client

    def snapshot_number(self, number: str) -> dict[str, object]:
        resource = self._number(number)
        return {
            "number": str(resource.phone_number),
            "number_sid": str(resource.sid),
            "route": {
                field: _optional(getattr(resource, field, None)) for field in _TWILIO_ROUTE_FIELDS
            },
        }

    def ensure_trunk(self, *, name: str, domain_name: str) -> ManagedSipResource:
        matches = [
            item
            for item in cast("list[Any]", self._client.trunking.v1.trunks.list())
            if str(item.friendly_name) == name
        ]
        if len(matches) > 1:
            raise VoiceyError("VY-TEL-006", detail="duplicate managed Twilio SIP trunks.")
        if matches:
            item = matches[0]
            if str(item.domain_name) != domain_name:
                raise VoiceyError(
                    "VY-TEL-006",
                    detail="managed Twilio SIP trunk domain differs from desired config.",
                )
            return ManagedSipResource("twilio_trunk", str(item.sid), False)
        item = self._client.trunking.v1.trunks.create(
            friendly_name=name,
            domain_name=domain_name,
            transfer_mode="enable-all",
            secure=True,
        )
        return ManagedSipResource("twilio_trunk", str(item.sid), True)

    def ensure_origination(
        self,
        *,
        trunk_sid: str,
        name: str,
        sip_uri: str,
    ) -> ManagedSipResource:
        collection = self._client.trunking.v1.trunks(trunk_sid).origination_urls
        matches = [
            item for item in cast("list[Any]", collection.list()) if str(item.friendly_name) == name
        ]
        if len(matches) > 1:
            raise VoiceyError(
                "VY-TEL-006",
                detail="duplicate managed Twilio origination URLs.",
            )
        if matches:
            item = matches[0]
            if str(item.sip_url) != sip_uri or not bool(item.enabled):
                raise VoiceyError(
                    "VY-TEL-006",
                    detail="managed Twilio origination URL differs from desired config.",
                )
            return ManagedSipResource(
                "twilio_origination",
                str(item.sid),
                False,
                trunk_sid,
            )
        item = collection.create(
            weight=10,
            priority=10,
            enabled=True,
            friendly_name=name,
            sip_url=sip_uri,
        )
        return ManagedSipResource(
            "twilio_origination",
            str(item.sid),
            True,
            trunk_sid,
        )

    def ensure_recording(
        self,
        *,
        trunk_sid: str,
        enabled: bool,
        allow_update: bool,
    ) -> None:
        context = self._client.trunking.v1.trunks(trunk_sid).recordings()
        current = context.fetch()
        desired = "record-from-answer-dual" if enabled else "do-not-record"
        if str(current.mode) == desired:
            return
        if not allow_update:
            raise VoiceyError(
                "VY-TEL-006",
                detail="managed Twilio trunk recording mode differs from desired config.",
            )
        updated = context.update(
            mode=desired,
            trim="trim-silence" if enabled else "do-not-trim",
        )
        if str(updated.mode) != desired:
            raise VoiceyError(
                "VY-TEL-006",
                detail="Twilio did not confirm the requested trunk recording mode.",
            )

    def completed_trunk_recording(
        self,
        call_sid: str,
    ) -> TwilioTrunkRecording | None:
        """Poll Core Recordings because Elastic SIP auto-recording has no callback."""
        if not _TWILIO_CALL_SID.fullmatch(call_sid):
            raise VoiceyError("VY-TEL-009", detail="invalid Twilio CallSid for recording.")
        items = cast(
            "list[Any]",
            self._client.recordings.list(call_sid=call_sid, limit=20),
        )
        completed = [
            item
            for item in items
            if str(getattr(item, "source", "")).casefold() == "trunking"
            and str(getattr(item, "status", "")).casefold() == "completed"
        ]
        if len(completed) > 1:
            raise VoiceyError(
                "VY-TEL-009",
                detail="multiple completed Twilio trunk recordings match one call.",
            )
        if not completed:
            return None
        item = completed[0]
        recording_sid = str(getattr(item, "sid", ""))
        if not _TWILIO_RECORDING_SID.fullmatch(recording_sid):
            raise VoiceyError(
                "VY-TEL-009",
                detail="Twilio returned an invalid trunk RecordingSid.",
            )
        raw_duration = getattr(item, "duration", None)
        try:
            duration = (
                None
                if raw_duration is None or raw_duration == ""
                else int(cast("str | int", raw_duration))
            )
        except (TypeError, ValueError) as exc:
            raise VoiceyError(
                "VY-TEL-009",
                detail="Twilio returned an invalid trunk recording duration.",
            ) from exc
        return TwilioTrunkRecording(
            recording_sid=recording_sid,
            call_sid=call_sid,
            duration_s=duration,
        )

    def ensure_credential_list(self, *, name: str) -> ManagedSipResource:
        collection = self._client.sip.credential_lists
        matches = [
            item for item in cast("list[Any]", collection.list()) if str(item.friendly_name) == name
        ]
        if len(matches) > 1:
            raise VoiceyError(
                "VY-TEL-006",
                detail="duplicate managed Twilio credential lists.",
            )
        if matches:
            return ManagedSipResource(
                "twilio_credential_list",
                str(matches[0].sid),
                False,
            )
        item = collection.create(friendly_name=name)
        return ManagedSipResource("twilio_credential_list", str(item.sid), True)

    def ensure_credential(
        self,
        *,
        credential_list_sid: str,
        username: str,
        password: str,
    ) -> ManagedSipResource:
        collection = self._client.sip.credential_lists(credential_list_sid).credentials
        matches = [
            item for item in cast("list[Any]", collection.list()) if str(item.username) == username
        ]
        if len(matches) > 1:
            raise VoiceyError("VY-TEL-006", detail="duplicate managed Twilio credentials.")
        if matches:
            return ManagedSipResource(
                "twilio_credential",
                str(matches[0].sid),
                False,
                credential_list_sid,
            )
        item = collection.create(username=username, password=password)
        return ManagedSipResource(
            "twilio_credential",
            str(item.sid),
            True,
            credential_list_sid,
        )

    def ensure_credential_binding(
        self,
        *,
        trunk_sid: str,
        credential_list_sid: str,
    ) -> ManagedSipResource:
        collection = self._client.trunking.v1.trunks(trunk_sid).credentials_lists
        matches = [
            item
            for item in cast("list[Any]", collection.list())
            if str(getattr(item, "credential_list_sid", None) or item.sid) == credential_list_sid
        ]
        if matches:
            return ManagedSipResource(
                "twilio_credential_binding",
                str(matches[0].sid),
                False,
                trunk_sid,
            )
        item = collection.create(credential_list_sid=credential_list_sid)
        return ManagedSipResource(
            "twilio_credential_binding",
            str(item.sid),
            True,
            trunk_sid,
        )

    def attach_number(self, *, trunk_sid: str, number: str) -> ManagedSipResource:
        current = self._number(number)
        number_sid = str(current.sid)
        if _optional(current.trunk_sid) == trunk_sid:
            return ManagedSipResource(
                "twilio_number_binding",
                number_sid,
                False,
                trunk_sid,
            )
        self._client.trunking.v1.trunks(trunk_sid).phone_numbers.create(phone_number_sid=number_sid)
        confirmed = self._client.incoming_phone_numbers(number_sid).fetch()
        if _optional(confirmed.trunk_sid) != trunk_sid:
            raise VoiceyError(
                "VY-TEL-006",
                detail="Twilio did not confirm the number-to-trunk binding.",
            )
        return ManagedSipResource(
            "twilio_number_binding",
            number_sid,
            True,
            trunk_sid,
        )

    def restore_number(self, snapshot: dict[str, object]) -> None:
        number_sid = str(snapshot["number_sid"])
        route = cast("dict[str, object]", snapshot["route"])
        previous_trunk = _optional(route.get("trunk_sid"))
        if previous_trunk:
            self._client.trunking.v1.trunks(previous_trunk).phone_numbers.create(
                phone_number_sid=number_sid
            )
        else:
            arguments = {field: str(route.get(field) or "") for field in _TWILIO_ROUTE_FIELDS}
            self._client.incoming_phone_numbers(number_sid).update(**arguments)
        confirmed = self._client.incoming_phone_numbers(number_sid).fetch()
        current = {
            field: _optional(getattr(confirmed, field, None)) for field in _TWILIO_ROUTE_FIELDS
        }
        expected = {field: _optional(route.get(field)) for field in _TWILIO_ROUTE_FIELDS}
        if current != expected:
            raise VoiceyError("VY-TEL-006", detail="Twilio route rollback did not compare equal.")

    def delete_resource(self, resource: ManagedSipResource) -> None:
        if resource.kind == "twilio_origination" and resource.parent_id:
            deleted = (
                self._client.trunking.v1.trunks(resource.parent_id)
                .origination_urls(resource.resource_id)
                .delete()
            )
        elif resource.kind == "twilio_credential_binding" and resource.parent_id:
            deleted = (
                self._client.trunking.v1.trunks(resource.parent_id)
                .credentials_lists(resource.resource_id)
                .delete()
            )
        elif resource.kind == "twilio_credential" and resource.parent_id:
            deleted = (
                self._client.sip.credential_lists(resource.parent_id)
                .credentials(resource.resource_id)
                .delete()
            )
        elif resource.kind == "twilio_credential_list":
            deleted = self._client.sip.credential_lists(resource.resource_id).delete()
        elif resource.kind == "twilio_trunk":
            deleted = self._client.trunking.v1.trunks(resource.resource_id).delete()
        else:
            raise VoiceyError(
                "VY-TEL-006",
                detail=f"unknown Twilio SIP rollback resource {resource.kind!r}.",
            )
        if not bool(deleted):
            raise VoiceyError(
                "VY-TEL-006",
                detail=f"Twilio did not confirm deletion of {resource.kind}.",
            )

    def _number(self, number: str) -> Any:
        validate_e164(number)
        matches = [
            item
            for item in cast(
                "list[Any]",
                self._client.incoming_phone_numbers.list(phone_number=number, limit=2),
            )
            if str(item.phone_number) == number
        ]
        if len(matches) != 1:
            raise VoiceyError(
                "VY-TEL-003",
                detail=f"Twilio number lookup returned {len(matches)} exact matches.",
            )
        return matches[0]


class TwilioTrunkRecordingReconciler:
    """Turn callback-less Elastic SIP recordings into the shared ready event."""

    def __init__(
        self,
        *,
        backend: TwilioElasticSipBackend,
        downloader: TwilioRecordingDownloader,
        repository: StorageRepository,
        artifact_store: ArtifactStore,
        access_base: str,
    ) -> None:
        parsed = urlsplit(access_base.rstrip("/"))
        if parsed.scheme != "https" or not parsed.hostname or parsed.query or parsed.fragment:
            raise VoiceyError(
                "VY-TEL-009",
                detail="recording access base must be an HTTPS base URL.",
            )
        self.backend = backend
        self.downloader = downloader
        self.repository = repository
        self.artifact_store = artifact_store
        self.access_base = access_base.rstrip("/")

    async def reconcile(self, *, call_id: str, twilio_call_sid: str) -> bool:
        """Perform one idempotent lookup/download/recording-ready attempt."""
        snapshot = await self.repository.get_recording_for_call(call_id)
        if snapshot is None:
            raise VoiceyError(
                "VY-TEL-009",
                detail=f"call {call_id!r} has no pending recording reference.",
            )
        if snapshot.status == "ready":
            return True
        recording = await asyncio.to_thread(
            self.backend.completed_trunk_recording,
            twilio_call_sid,
        )
        if recording is None:
            return False
        storage_key = f"recordings/{snapshot.recording_id}.mp3"
        await self.downloader.download_recording(
            recording.recording_sid,
            artifact_store=self.artifact_store,
            storage_key=storage_key,
        )
        await self.repository.mark_recording_ready(
            RecordingReady(
                recording_id=snapshot.recording_id,
                access_url=f"{self.access_base}/recordings/{snapshot.recording_id}",
                storage_key=storage_key,
            )
        )
        return True

    async def wait_until_ready(
        self,
        *,
        call_id: str,
        twilio_call_sid: str,
        timeout_s: float = 120.0,
        poll_interval_s: float = 2.0,
    ) -> bool:
        """Bound post-call polling; false remains visibly pending for recovery."""
        if timeout_s <= 0 or poll_interval_s <= 0:
            raise VoiceyError(
                "VY-TEL-009",
                detail="recording reconciliation timing must be positive.",
            )
        deadline = asyncio.get_running_loop().time() + timeout_s
        while True:
            if await self.reconcile(
                call_id=call_id,
                twilio_call_sid=twilio_call_sid,
            ):
                return True
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                return False
            await asyncio.sleep(min(poll_interval_s, remaining))


def _managed_metadata(config: TwilioLiveKitSipConfig) -> str:
    return json.dumps(
        {
            "managed_by": "voicey",
            "config_fingerprint": config.config_fingerprint,
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def _resource(wire: dict[str, object]) -> ManagedSipResource:
    return ManagedSipResource(
        kind=str(wire["kind"]),
        resource_id=str(wire["resource_id"]),
        created=bool(wire["created"]),
        parent_id=None if wire.get("parent_id") is None else str(wire["parent_id"]),
    )


def _optional(value: object) -> str | None:
    if value is None or value == "":
        return None
    return str(value)
