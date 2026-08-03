"""Crash-safe Telnyx FQDN SIP provisioning for LiveKit."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Protocol, cast

import httpx
from livekit.protocol import agent_dispatch as lk_dispatch
from livekit.protocol import room as lk_room
from livekit.protocol import sip as lk_sip

from voicey.errors import VoiceyError
from voicey.runtimes.livekit.sip import LiveKitSipAPI, ManagedSipResource
from voicey.telephony.ledger import ProvisioningRecord, TelephonyLedger
from voicey.telephony.models import validate_e164

_SAFE_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
_SIP_URI = re.compile(r"^sip:([a-z0-9](?:[a-z0-9.-]*[a-z0-9])?)(?::([0-9]{1,5}))?$")


class TelnyxSipBackend(Protocol):
    """Idempotent Telnyx resources needed for one FQDN SIP connection."""

    def snapshot_number(self, number: str) -> dict[str, object]: ...

    def ensure_outbound_profile(self, *, name: str) -> ManagedSipResource: ...

    def ensure_connection(
        self,
        *,
        name: str,
        username: str,
        password: str,
        outbound_profile_id: str,
        config_fingerprint: str,
        anchorsite: str,
    ) -> ManagedSipResource: ...

    def ensure_fqdn(
        self,
        *,
        connection_id: str,
        fqdn: str,
        port: int,
    ) -> ManagedSipResource: ...

    def attach_number(self, *, connection_id: str, number: str) -> ManagedSipResource: ...

    def restore_number(self, snapshot: dict[str, object]) -> None: ...

    def delete_resource(self, resource: ManagedSipResource) -> None: ...


@dataclass(frozen=True, slots=True)
class TelnyxLiveKitSipConfig:
    """Deterministic desired state for a Telnyx↔LiveKit SIP route."""

    number: str
    agent_name: str
    livekit_sip_uri: str
    auth_username: str
    auth_password: str = field(repr=False)
    resource_prefix: str = "voicey"
    room_prefix: str = "call-"
    anchorsite: str = "Latency"

    def __post_init__(self) -> None:
        validate_e164(self.number)
        match = _SIP_URI.fullmatch(self.livekit_sip_uri)
        if match is None:
            raise VoiceyError(
                "VY-TEL-002",
                detail="LiveKit SIP URI must be sip:host with no path or credentials.",
            )
        if match.group(2) is not None and not 1 <= int(match.group(2)) <= 65535:
            raise VoiceyError("VY-TEL-002", detail="LiveKit SIP port is invalid.")
        if match.group(2) is not None and int(match.group(2)) != 5060:
            raise VoiceyError(
                "VY-TEL-002",
                detail="Telnyx's certified LiveKit FQDN path requires TCP port 5060.",
            )
        if not _SAFE_NAME.fullmatch(self.agent_name) or not _SAFE_NAME.fullmatch(
            self.resource_prefix
        ):
            raise VoiceyError(
                "VY-TEL-002",
                detail="LiveKit agent and Telnyx SIP resource names must be lowercase slugs.",
            )
        if not self.auth_username or not self.auth_password:
            raise VoiceyError("VY-TEL-002", detail="Telnyx SIP credentials are required.")
        if len(self.auth_username) > 128 or len(self.auth_password) > 128:
            raise VoiceyError("VY-TEL-002", detail="Telnyx SIP credentials exceed 128 chars.")
        if not self.room_prefix or len(self.room_prefix) > 32:
            raise VoiceyError("VY-TEL-002", detail="SIP room prefix must contain 1-32 chars.")
        if self.anchorsite not in {
            "Latency",
            "Chicago, IL",
            "Ashburn, VA",
            "San Jose, CA",
            "Sydney, Australia",
            "Amsterdam, Netherlands",
            "London, UK",
            "Toronto, Canada",
            "Vancouver, Canada",
            "Frankfurt, Germany",
        }:
            raise VoiceyError("VY-TEL-002", detail="unsupported Telnyx anchor site.")

    @property
    def base_name(self) -> str:
        return f"{self.resource_prefix}-{self.agent_name}-{self.number.removeprefix('+')}"

    @property
    def livekit_sip_host(self) -> str:
        match = _SIP_URI.fullmatch(self.livekit_sip_uri)
        if match is None:  # protected by validation; keeps the property total for typing
            raise VoiceyError("VY-TEL-002", detail="LiveKit SIP URI is invalid.")
        return match.group(1)

    @property
    def config_fingerprint(self) -> str:
        wire = {
            "number": self.number,
            "agent_name": self.agent_name,
            "livekit_sip_uri": self.livekit_sip_uri,
            "auth_username": self.auth_username,
            "auth_password_sha256": hashlib.sha256(self.auth_password.encode()).hexdigest(),
            "room_prefix": self.room_prefix,
            "anchorsite": self.anchorsite,
            "transport": "TCP",
            "fqdn_port": 5060,
        }
        encoded = json.dumps(wire, sort_keys=True, separators=(",", ":")).encode()
        return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


@dataclass(frozen=True, slots=True)
class TelnyxSipProvisioningResult:
    operation_id: str
    livekit_inbound_trunk_id: str
    livekit_outbound_trunk_id: str
    livekit_dispatch_rule_id: str
    telnyx_connection_id: str
    telnyx_fqdn_id: str
    created_resources: int


class TelnyxLiveKitSipProvisioner:
    """Apply both control planes as one reverse-rollback ledger operation."""

    provider = "telnyx-livekit"

    def __init__(
        self,
        *,
        livekit: LiveKitSipAPI,
        telnyx: TelnyxSipBackend,
        ledger: TelephonyLedger,
    ) -> None:
        self.livekit = livekit
        self.telnyx = telnyx
        self.ledger = ledger

    async def provision(
        self,
        config: TelnyxLiveKitSipConfig,
    ) -> TelnyxSipProvisioningResult:
        snapshot = self.telnyx.snapshot_number(config.number)
        operation = self.ledger.prepare_provisioning(
            provider=self.provider,
            number=config.number,
            snapshot=snapshot,
            planned={
                "name": config.base_name,
                "config_fingerprint": config.config_fingerprint,
                "livekit_sip_host": config.livekit_sip_host,
                "telnyx_sip_address": "sip.telnyx.com",
                "transport": "TCP",
                "fqdn_port": 5060,
            },
        )
        try:
            inbound = await self._ensure_livekit_inbound(config)
            operation = self._record(operation, inbound)
            dispatch = await self._ensure_dispatch(config, inbound.resource_id)
            operation = self._record(operation, dispatch)
            outbound = await self._ensure_livekit_outbound(config)
            operation = self._record(operation, outbound)
            profile = self.telnyx.ensure_outbound_profile(name=f"{config.base_name}-outbound")
            operation = self._record(operation, profile)
            connection = self.telnyx.ensure_connection(
                name=config.base_name,
                username=config.auth_username,
                password=config.auth_password,
                outbound_profile_id=profile.resource_id,
                config_fingerprint=config.config_fingerprint,
                anchorsite=config.anchorsite,
            )
            operation = self._record(operation, connection)
            fqdn = self.telnyx.ensure_fqdn(
                connection_id=connection.resource_id,
                fqdn=config.livekit_sip_host,
                port=5060,
            )
            operation = self._record(operation, fqdn)
            binding = self.telnyx.attach_number(
                connection_id=connection.resource_id,
                number=config.number,
            )
            operation = self._record(operation, binding)
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
                        f"Telnyx SIP provisioning outcome is ambiguous for "
                        f"{operation.operation_id!r}; reconcile before retry."
                    ),
                ) from exc
            await self._rollback_after_failure(operation.operation_id)
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
                    f"Telnyx SIP provisioning outcome is ambiguous for "
                    f"{operation.operation_id!r}; reconcile before retry."
                ),
            ) from exc
        return TelnyxSipProvisioningResult(
            operation_id=operation.operation_id,
            livekit_inbound_trunk_id=inbound.resource_id,
            livekit_outbound_trunk_id=outbound.resource_id,
            livekit_dispatch_rule_id=dispatch.resource_id,
            telnyx_connection_id=connection.resource_id,
            telnyx_fqdn_id=fqdn.resource_id,
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
                elif resource.kind == "telnyx_number_binding":
                    self.telnyx.restore_number(operation.snapshot)
                else:
                    self.telnyx.delete_resource(resource)
        except Exception as exc:
            self.ledger.transition_provisioning(
                operation_id,
                expected=("rolling_back",),
                state="conflict",
            )
            raise VoiceyError(
                "VY-TEL-006",
                detail=f"Telnyx SIP rollback conflicted for {operation_id!r}.",
            ) from exc
        return self.ledger.transition_provisioning(
            operation_id,
            expected=("rolling_back",),
            state="rolled_back",
        )

    async def _rollback_after_failure(self, operation_id: str) -> None:
        await self.rollback(operation_id)

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

    async def _ensure_livekit_inbound(
        self,
        config: TelnyxLiveKitSipConfig,
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
                or item.media_encryption != lk_sip.SIP_MEDIA_ENCRYPT_DISABLE
            ):
                raise VoiceyError(
                    "VY-TEL-006",
                    detail="managed Telnyx LiveKit inbound trunk differs from desired config.",
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
                    media_encryption=lk_sip.SIP_MEDIA_ENCRYPT_DISABLE,
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
        config: TelnyxLiveKitSipConfig,
    ) -> ManagedSipResource:
        response = await self.livekit.list_outbound_trunk(
            lk_sip.ListSIPOutboundTrunkRequest(numbers=[config.number])
        )
        matching = [item for item in response.items if item.name == config.base_name]
        if len(matching) > 1:
            raise VoiceyError("VY-TEL-006", detail="duplicate managed LiveKit outbound trunks.")
        metadata = _managed_metadata(config)
        header = {"X-Telnyx-Username": config.auth_username}
        if matching:
            item = matching[0]
            if (
                list(item.numbers) != [config.number]
                or item.address != "sip.telnyx.com"
                or item.auth_username != config.auth_username
                or item.metadata != metadata
                or item.transport != lk_sip.SIP_TRANSPORT_TCP
                or item.media_encryption != lk_sip.SIP_MEDIA_ENCRYPT_DISABLE
                or dict(item.headers_to_attributes) != header
            ):
                raise VoiceyError(
                    "VY-TEL-006",
                    detail="managed Telnyx LiveKit outbound trunk differs from desired config.",
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
                    address="sip.telnyx.com",
                    numbers=[config.number],
                    auth_username=config.auth_username,
                    auth_password=config.auth_password,
                    headers_to_attributes=header,
                    transport=lk_sip.SIP_TRANSPORT_TCP,
                    media_encryption=lk_sip.SIP_MEDIA_ENCRYPT_DISABLE,
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
        config: TelnyxLiveKitSipConfig,
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
                    detail="managed Telnyx LiveKit dispatch rule differs from desired config.",
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
                                    "provider": "telnyx",
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


class TelnyxSipHTTPBackend:
    """Current Telnyx v2 FQDN/voice-profile API with strict ensure semantics."""

    def __init__(
        self,
        *,
        api_key: str,
        client: httpx.Client | None = None,
        base_url: str = "https://api.telnyx.com/v2",
    ) -> None:
        if not api_key:
            raise VoiceyError("VY-TEL-002", detail="TELNYX_API_KEY is required.")
        self._client = client or httpx.Client(
            base_url=base_url.rstrip("/"),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            timeout=30,
            follow_redirects=False,
        )

    def snapshot_number(self, number: str) -> dict[str, object]:
        item = self._number(number)
        return {
            "number": str(item["phone_number"]),
            "number_id": str(item["id"]),
            "connection_id": _optional(item.get("connection_id")),
        }

    def ensure_outbound_profile(self, *, name: str) -> ManagedSipResource:
        desired = {
            "name": name,
            "traffic_type": "conversational",
            "service_plan": "global",
        }
        matches = [
            item
            for item in self._list("/outbound_voice_profiles")
            if str(item.get("name", "")) == name
        ]
        if len(matches) > 1:
            raise VoiceyError("VY-TEL-006", detail="duplicate managed outbound profiles.")
        if matches:
            item = matches[0]
            if any(str(item.get(key, "")) != value for key, value in desired.items()):
                raise VoiceyError(
                    "VY-TEL-006",
                    detail="managed Telnyx outbound profile differs from desired config.",
                )
            return ManagedSipResource(
                "telnyx_outbound_profile",
                _provider_id(item.get("id"), "outbound profile id"),
                False,
            )
        created = self._data_object(
            self._request(
                "POST",
                "/outbound_voice_profiles",
                json_body=desired,
                expected=(200, 201),
                operation="create outbound profile",
            )
        )
        return ManagedSipResource(
            "telnyx_outbound_profile",
            _provider_id(created.get("id"), "outbound profile id"),
            True,
        )

    def ensure_connection(
        self,
        *,
        name: str,
        username: str,
        password: str,
        outbound_profile_id: str,
        config_fingerprint: str,
        anchorsite: str,
    ) -> ManagedSipResource:
        tag = f"voicey:{config_fingerprint.removeprefix('sha256:')}"
        inbound = {
            "ani_number_format": "+E.164",
            "dnis_number_format": "+e164",
        }
        outbound = {"outbound_voice_profile_id": outbound_profile_id}
        matches = [
            item
            for item in self._list("/fqdn_connections")
            if str(item.get("connection_name", "")) == name
        ]
        if len(matches) > 1:
            raise VoiceyError("VY-TEL-006", detail="duplicate managed FQDN connections.")
        if matches:
            item = matches[0]
            if (
                item.get("active") is not True
                or item.get("anchorsite_override") != anchorsite
                or item.get("user_name") != username
                or item.get("transport_protocol") != "TCP"
                or not _contains(_object(item.get("inbound")), inbound)
                or not _contains(_object(item.get("outbound")), outbound)
                or tag not in _strings(item.get("tags"))
            ):
                raise VoiceyError(
                    "VY-TEL-006",
                    detail="managed Telnyx FQDN connection differs from desired config.",
                )
            return ManagedSipResource(
                "telnyx_fqdn_connection",
                _provider_id(item.get("id"), "FQDN connection id"),
                False,
            )
        created = self._data_object(
            self._request(
                "POST",
                "/fqdn_connections",
                json_body={
                    "active": True,
                    "anchorsite_override": anchorsite,
                    "connection_name": name,
                    "user_name": username,
                    "password": password,
                    "inbound": inbound,
                    "outbound": outbound,
                    "transport_protocol": "TCP",
                    "dtmf_type": "RFC 2833",
                    "tags": [tag],
                },
                expected=(200, 201),
                operation="create FQDN connection",
            )
        )
        return ManagedSipResource(
            "telnyx_fqdn_connection",
            _provider_id(created.get("id"), "FQDN connection id"),
            True,
        )

    def ensure_fqdn(
        self,
        *,
        connection_id: str,
        fqdn: str,
        port: int,
    ) -> ManagedSipResource:
        matches = [
            item
            for item in self._list("/fqdns")
            if item.get("connection_id") == connection_id and item.get("fqdn") == fqdn
        ]
        if len(matches) > 1:
            raise VoiceyError("VY-TEL-006", detail="duplicate managed Telnyx FQDNs.")
        if matches:
            item = matches[0]
            try:
                observed_port = int(cast("int | str", item.get("port", 0)))
            except (TypeError, ValueError) as exc:
                raise VoiceyError(
                    "VY-TEL-006",
                    detail="managed Telnyx FQDN has an invalid port.",
                ) from exc
            if observed_port != port or str(item.get("dns_record_type", "")).casefold() != "a":
                raise VoiceyError(
                    "VY-TEL-006",
                    detail="managed Telnyx FQDN differs from desired config.",
                )
            return ManagedSipResource(
                "telnyx_fqdn",
                _provider_id(item.get("id"), "FQDN id"),
                False,
                connection_id,
            )
        created = self._data_object(
            self._request(
                "POST",
                "/fqdns",
                json_body={
                    "connection_id": connection_id,
                    "fqdn": fqdn,
                    "port": port,
                    "dns_record_type": "a",
                },
                expected=(200, 201),
                operation="create FQDN",
            )
        )
        return ManagedSipResource(
            "telnyx_fqdn",
            _provider_id(created.get("id"), "FQDN id"),
            True,
            connection_id,
        )

    def attach_number(self, *, connection_id: str, number: str) -> ManagedSipResource:
        item = self._number(number)
        number_id = _provider_id(item.get("id"), "phone number id")
        if _optional(item.get("connection_id")) == connection_id:
            return ManagedSipResource(
                "telnyx_number_binding",
                number_id,
                False,
                connection_id,
            )
        updated = self._data_object(
            self._request(
                "PATCH",
                f"/phone_numbers/{number_id}",
                json_body={"id": number_id, "connection_id": connection_id},
                expected=(200,),
                operation="attach number to FQDN connection",
            )
        )
        if _optional(updated.get("connection_id")) != connection_id:
            raise VoiceyError(
                "VY-TEL-006",
                detail="Telnyx did not confirm the number-to-FQDN binding.",
            )
        return ManagedSipResource(
            "telnyx_number_binding",
            number_id,
            True,
            connection_id,
        )

    def restore_number(self, snapshot: dict[str, object]) -> None:
        number_id = _provider_id(snapshot.get("number_id"), "phone number id")
        previous = _optional(snapshot.get("connection_id"))
        updated = self._data_object(
            self._request(
                "PATCH",
                f"/phone_numbers/{number_id}",
                json_body={"id": number_id, "connection_id": previous},
                expected=(200,),
                operation="restore number route",
            )
        )
        if _optional(updated.get("connection_id")) != previous:
            raise VoiceyError(
                "VY-TEL-006",
                detail="Telnyx number route rollback did not compare equal.",
            )

    def delete_resource(self, resource: ManagedSipResource) -> None:
        paths = {
            "telnyx_fqdn": "/fqdns",
            "telnyx_fqdn_connection": "/fqdn_connections",
            "telnyx_outbound_profile": "/outbound_voice_profiles",
        }
        base = paths.get(resource.kind)
        if base is None:
            raise VoiceyError(
                "VY-TEL-006",
                detail=f"unknown Telnyx SIP rollback resource {resource.kind!r}.",
            )
        self._request(
            "DELETE",
            f"{base}/{resource.resource_id}",
            expected=(200, 202, 204),
            operation=f"delete {resource.kind}",
        )

    def _number(self, number: str) -> dict[str, object]:
        normalized = validate_e164(number)
        matches = [
            item
            for item in self._list(
                "/phone_numbers",
                params={"filter[phone_number]": normalized, "page[size]": "2"},
            )
            if str(item.get("phone_number", "")) == normalized
        ]
        if len(matches) != 1:
            raise VoiceyError(
                "VY-TEL-003",
                detail=f"Telnyx number lookup returned {len(matches)} exact matches.",
            )
        return matches[0]

    def _list(
        self,
        path: str,
        *,
        params: Mapping[str, str] | None = None,
    ) -> list[dict[str, object]]:
        document = self._request(
            "GET",
            path,
            params={"page[size]": "250", **dict(params or {})},
            expected=(200,),
            operation=f"list {path.rsplit('/', maxsplit=1)[-1]}",
        )
        data = document.get("data")
        if not isinstance(data, list):
            raise VoiceyError("VY-TEL-011", detail="Telnyx list response is malformed.")
        raw = cast("list[object]", data)
        if any(not isinstance(item, dict) for item in raw):
            raise VoiceyError("VY-TEL-011", detail="Telnyx list item is malformed.")
        return cast("list[dict[str, object]]", raw)

    @staticmethod
    def _data_object(document: dict[str, object]) -> dict[str, object]:
        data = document.get("data")
        if not isinstance(data, dict):
            raise VoiceyError("VY-TEL-011", detail="Telnyx response data is malformed.")
        return cast("dict[str, object]", data)

    def _request(
        self,
        method: str,
        path: str,
        *,
        expected: tuple[int, ...],
        operation: str,
        params: Mapping[str, str] | None = None,
        json_body: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        try:
            response = self._client.request(
                method,
                path,
                params=params,
                json=json_body,
            )
        except httpx.HTTPError as exc:
            raise VoiceyError(
                "VY-TEL-011",
                detail=f"Telnyx {operation} did not return a definitive result.",
            ) from exc
        if response.status_code not in expected:
            if 400 <= response.status_code < 500:
                raise VoiceyError(
                    "VY-TEL-004",
                    detail=f"Telnyx {operation} http_{response.status_code}.",
                )
            raise VoiceyError(
                "VY-TEL-011",
                detail=f"Telnyx {operation} did not return a definitive result.",
            )
        if response.status_code == 204 or not response.content:
            return {}
        try:
            loaded: object = response.json()
        except ValueError as exc:
            raise VoiceyError(
                "VY-TEL-011",
                detail=f"Telnyx {operation} returned invalid JSON.",
            ) from exc
        if not isinstance(loaded, dict):
            raise VoiceyError(
                "VY-TEL-011",
                detail=f"Telnyx {operation} returned an invalid envelope.",
            )
        return cast("dict[str, object]", loaded)


def _managed_metadata(config: TelnyxLiveKitSipConfig) -> str:
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


def _provider_id(value: object, field_name: str) -> str:
    normalized = str(value or "")
    if not normalized or len(normalized) > 512 or any(char.isspace() for char in normalized):
        raise VoiceyError("VY-TEL-006", detail=f"Telnyx returned an invalid {field_name}.")
    return normalized


def _optional(value: object) -> str | None:
    return None if value in {None, ""} else str(value)


def _object(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        return {}
    return cast("dict[str, object]", value)


def _contains(observed: Mapping[str, object], required: Mapping[str, object]) -> bool:
    return all(observed.get(key) == value for key, value in required.items())


def _strings(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in cast("list[object]", value)]
