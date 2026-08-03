"""Explicit, crash-safe generic SIP beta for the LiveKit runtime."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Literal

from livekit.protocol import agent_dispatch as lk_dispatch
from livekit.protocol import room as lk_room
from livekit.protocol import sip as lk_sip

from voicey.errors import VoiceyError
from voicey.runtimes.livekit.sip import LiveKitSipAPI, ManagedSipResource
from voicey.telephony.ledger import ProvisioningRecord, TelephonyLedger
from voicey.telephony.models import validate_e164

SipTransport = Literal["udp", "tcp", "tls"]
SipMediaEncryption = Literal["disable", "allow", "require"]

_SAFE_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
_SIP_ADDRESS = re.compile(r"^(?:[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?|\[[0-9a-f:]+\])(?::[0-9]{1,5})?$")


@dataclass(frozen=True, slots=True)
class GenericSipConfig:
    """Operator-supplied desired state; voicey never mutates the PBX/carrier."""

    number: str
    agent_name: str
    outbound_address: str
    auth_username: str
    auth_password: str = field(repr=False)
    allowed_addresses: tuple[str, ...] = ()
    transport: SipTransport = "tls"
    media_encryption: SipMediaEncryption = "require"
    resource_prefix: str = "voicey"
    room_prefix: str = "call-"

    def __post_init__(self) -> None:
        validate_e164(self.number)
        if not _SAFE_NAME.fullmatch(self.agent_name) or not _SAFE_NAME.fullmatch(
            self.resource_prefix
        ):
            raise VoiceyError(
                "VY-TEL-002",
                detail="generic SIP agent and resource names must be lowercase slugs.",
            )
        match = _SIP_ADDRESS.fullmatch(self.outbound_address.casefold())
        if match is None or self.outbound_address.startswith(("sip:", "sips:")):
            raise VoiceyError(
                "VY-TEL-002",
                detail="generic SIP address must be a host with optional port and no scheme.",
            )
        port_text = self.outbound_address.rsplit(":", maxsplit=1)[-1]
        if port_text.isdecimal() and not 1 <= int(port_text) <= 65535:
            raise VoiceyError("VY-TEL-002", detail="generic SIP port is invalid.")
        if not 1 <= len(self.auth_username) <= 128 or not 8 <= len(self.auth_password) <= 128:
            raise VoiceyError(
                "VY-TEL-002",
                detail="generic SIP credentials require a 1-128 char user and 8-128 char password.",
            )
        for address in self.allowed_addresses:
            try:
                ipaddress.ip_network(address, strict=False)
            except ValueError as exc:
                raise VoiceyError(
                    "VY-TEL-002",
                    detail=f"generic SIP allowlist entry {address!r} is not CIDR.",
                ) from exc
        if len(set(self.allowed_addresses)) != len(self.allowed_addresses):
            raise VoiceyError("VY-TEL-002", detail="generic SIP allowlist has duplicates.")
        if not self.room_prefix or len(self.room_prefix) > 32:
            raise VoiceyError("VY-TEL-002", detail="SIP room prefix must contain 1-32 chars.")
        if self.transport == "tls" and self.media_encryption == "disable":
            raise VoiceyError(
                "VY-TEL-002",
                detail="TLS generic SIP must allow or require encrypted media.",
            )

    @property
    def base_name(self) -> str:
        return f"{self.resource_prefix}-{self.agent_name}-{self.number.removeprefix('+')}"

    @property
    def config_fingerprint(self) -> str:
        wire = {
            "number": self.number,
            "agent_name": self.agent_name,
            "outbound_address": self.outbound_address.casefold(),
            "auth_username": self.auth_username,
            "auth_password_sha256": hashlib.sha256(self.auth_password.encode()).hexdigest(),
            "allowed_addresses": sorted(self.allowed_addresses),
            "transport": self.transport,
            "media_encryption": self.media_encryption,
            "room_prefix": self.room_prefix,
        }
        encoded = json.dumps(wire, sort_keys=True, separators=(",", ":")).encode()
        return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


@dataclass(frozen=True, slots=True)
class GenericSipProvisioningResult:
    operation_id: str
    livekit_inbound_trunk_id: str
    livekit_outbound_trunk_id: str
    livekit_dispatch_rule_id: str
    created_resources: int


class GenericSipProvisioner:
    """Provision only LiveKit resources; PBX routing remains an operator action."""

    provider = "sip-livekit"

    def __init__(self, *, livekit: LiveKitSipAPI, ledger: TelephonyLedger) -> None:
        self.livekit = livekit
        self.ledger = ledger

    async def provision(self, config: GenericSipConfig) -> GenericSipProvisioningResult:
        operation = self.ledger.prepare_provisioning(
            provider=self.provider,
            number=config.number,
            snapshot={"external_sip_route": "operator-managed"},
            planned={
                "name": config.base_name,
                "config_fingerprint": config.config_fingerprint,
                "outbound_address": config.outbound_address,
                "transport": config.transport.upper(),
                "media_encryption": config.media_encryption,
            },
        )
        try:
            inbound = await self._ensure_inbound(config)
            operation = self._record(operation, inbound)
            dispatch = await self._ensure_dispatch(config, inbound.resource_id)
            operation = self._record(operation, dispatch)
            outbound = await self._ensure_outbound(config)
            operation = self._record(operation, outbound)
            operation = self.ledger.transition_provisioning(
                operation.operation_id,
                expected=("prepared", "applying"),
                state="applied",
            )
        except VoiceyError as exc:
            if exc.code == "VY-TEL-011":
                self.ledger.transition_provisioning(
                    operation.operation_id,
                    expected=("prepared", "applying"),
                    state="ambiguous",
                )
                raise VoiceyError(
                    "VY-TEL-006",
                    detail=(
                        f"generic SIP provisioning outcome is ambiguous for "
                        f"{operation.operation_id!r}; reconcile before retry."
                    ),
                ) from exc
            await self.rollback(operation.operation_id)
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
                    f"generic SIP provisioning outcome is ambiguous for "
                    f"{operation.operation_id!r}; reconcile before retry."
                ),
            ) from exc
        return GenericSipProvisioningResult(
            operation_id=operation.operation_id,
            livekit_inbound_trunk_id=inbound.resource_id,
            livekit_outbound_trunk_id=outbound.resource_id,
            livekit_dispatch_rule_id=dispatch.resource_id,
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
                else:
                    await self.livekit.delete_trunk(
                        lk_sip.DeleteSIPTrunkRequest(sip_trunk_id=resource.resource_id)
                    )
        except Exception as exc:
            self.ledger.transition_provisioning(
                operation_id,
                expected=("rolling_back",),
                state="conflict",
            )
            raise VoiceyError(
                "VY-TEL-006",
                detail=f"generic SIP rollback conflicted for {operation_id!r}.",
            ) from exc
        return self.ledger.transition_provisioning(
            operation_id,
            expected=("rolling_back",),
            state="rolled_back",
        )

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

    async def _ensure_inbound(self, config: GenericSipConfig) -> ManagedSipResource:
        response = await self.livekit.list_inbound_trunk(
            lk_sip.ListSIPInboundTrunkRequest(numbers=[config.number])
        )
        name = f"{config.base_name}-in"
        matching = [item for item in response.items if item.name == name]
        if len(matching) > 1:
            raise VoiceyError("VY-TEL-006", detail="duplicate managed generic SIP trunks.")
        metadata = _metadata(config)
        encryption = _media_encryption(config.media_encryption)
        if matching:
            item = matching[0]
            if (
                list(item.numbers) != [config.number]
                or list(item.allowed_addresses) != list(config.allowed_addresses)
                or item.auth_username != config.auth_username
                or item.metadata != metadata
                or item.media_encryption != encryption
            ):
                raise VoiceyError(
                    "VY-TEL-006",
                    detail="managed generic SIP inbound trunk differs from desired config.",
                )
            return ManagedSipResource("livekit_inbound_trunk", item.sip_trunk_id, False)
        created = await self.livekit.create_inbound_trunk(
            lk_sip.CreateSIPInboundTrunkRequest(
                trunk=lk_sip.SIPInboundTrunkInfo(
                    name=name,
                    metadata=metadata,
                    numbers=[config.number],
                    allowed_addresses=list(config.allowed_addresses),
                    auth_username=config.auth_username,
                    auth_password=config.auth_password,
                    media_encryption=encryption,
                )
            )
        )
        return ManagedSipResource("livekit_inbound_trunk", created.sip_trunk_id, True)

    async def _ensure_outbound(self, config: GenericSipConfig) -> ManagedSipResource:
        response = await self.livekit.list_outbound_trunk(
            lk_sip.ListSIPOutboundTrunkRequest(numbers=[config.number])
        )
        name = f"{config.base_name}-out"
        matching = [item for item in response.items if item.name == name]
        if len(matching) > 1:
            raise VoiceyError(
                "VY-TEL-006",
                detail="duplicate managed generic SIP outbound trunks.",
            )
        metadata = _metadata(config)
        transport = _transport(config.transport)
        encryption = _media_encryption(config.media_encryption)
        if matching:
            item = matching[0]
            if (
                list(item.numbers) != [config.number]
                or item.address != config.outbound_address
                or item.auth_username != config.auth_username
                or item.metadata != metadata
                or item.transport != transport
                or item.media_encryption != encryption
            ):
                raise VoiceyError(
                    "VY-TEL-006",
                    detail="managed generic SIP outbound trunk differs from desired config.",
                )
            return ManagedSipResource("livekit_outbound_trunk", item.sip_trunk_id, False)
        created = await self.livekit.create_outbound_trunk(
            lk_sip.CreateSIPOutboundTrunkRequest(
                trunk=lk_sip.SIPOutboundTrunkInfo(
                    name=name,
                    metadata=metadata,
                    address=config.outbound_address,
                    numbers=[config.number],
                    auth_username=config.auth_username,
                    auth_password=config.auth_password,
                    transport=transport,
                    media_encryption=encryption,
                )
            )
        )
        return ManagedSipResource("livekit_outbound_trunk", created.sip_trunk_id, True)

    async def _ensure_dispatch(
        self,
        config: GenericSipConfig,
        trunk_id: str,
    ) -> ManagedSipResource:
        response = await self.livekit.list_dispatch_rule(
            lk_sip.ListSIPDispatchRuleRequest(trunk_ids=[trunk_id])
        )
        matching = [item for item in response.items if item.name == config.base_name]
        if len(matching) > 1:
            raise VoiceyError("VY-TEL-006", detail="duplicate managed generic SIP dispatch.")
        metadata = _metadata(config)
        if matching:
            item = matching[0]
            agents = list(item.room_config.agents)
            if (
                list(item.trunk_ids) != [trunk_id]
                or item.rule.dispatch_rule_individual.room_prefix != config.room_prefix
                or item.metadata != metadata
                or len(agents) != 1
                or agents[0].agent_name != config.agent_name
            ):
                raise VoiceyError(
                    "VY-TEL-006",
                    detail="managed generic SIP dispatch differs from desired config.",
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
                                    "provider": "sip",
                                    "tier": "beta",
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


def _metadata(config: GenericSipConfig) -> str:
    return json.dumps(
        {
            "managed_by": "voicey",
            "provider": "sip",
            "tier": "beta",
            "config_fingerprint": config.config_fingerprint,
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def _transport(value: SipTransport) -> lk_sip.SIPTransport:
    return {
        "udp": lk_sip.SIP_TRANSPORT_UDP,
        "tcp": lk_sip.SIP_TRANSPORT_TCP,
        "tls": lk_sip.SIP_TRANSPORT_TLS,
    }[value]


def _media_encryption(value: SipMediaEncryption) -> lk_sip.SIPMediaEncryption:
    return {
        "disable": lk_sip.SIP_MEDIA_ENCRYPT_DISABLE,
        "allow": lk_sip.SIP_MEDIA_ENCRYPT_ALLOW,
        "require": lk_sip.SIP_MEDIA_ENCRYPT_REQUIRE,
    }[value]


def _resource(wire: Mapping[str, object]) -> ManagedSipResource:
    return ManagedSipResource(
        kind=str(wire["kind"]),
        resource_id=str(wire["resource_id"]),
        created=bool(wire["created"]),
        parent_id=None if wire.get("parent_id") is None else str(wire["parent_id"]),
    )
