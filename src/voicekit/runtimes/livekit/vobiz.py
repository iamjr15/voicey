"""Crash-safe Vobiz UDP SIP provisioning for LiveKit."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Protocol, cast
from urllib.parse import quote

import httpx
from livekit.protocol import agent_dispatch as lk_dispatch
from livekit.protocol import room as lk_room
from livekit.protocol import sip as lk_sip

from voicekit.errors import VoicekitError
from voicekit.runtimes.livekit.sip import LiveKitSipAPI, ManagedSipResource
from voicekit.telephony.ledger import ProvisioningRecord, TelephonyLedger
from voicekit.telephony.models import validate_e164, validate_identifier

_SAFE_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
_SIP_URI = re.compile(r"^sip:([a-z0-9](?:[a-z0-9.-]*[a-z0-9])?)(?::([0-9]{1,5}))?$")
_SIP_DOMAIN = re.compile(r"^[a-z0-9][a-z0-9.-]{1,253}\.sip\.vobiz\.ai$")
_SIP_USERNAME = re.compile(r"^[A-Za-z0-9]{1,100}$")
_VOBIZ_GATEWAY = "13.233.44.61/32"


@dataclass(frozen=True, slots=True)
class VobizManagedTrunk:
    resource: ManagedSipResource
    sip_domain: str


class VobizSipBackend(Protocol):
    """Verifiable Vobiz resources needed by a LiveKit SIP route."""

    def snapshot_number(self, number: str) -> dict[str, object]: ...

    def require_credential(
        self,
        *,
        credential_id: str,
        username: str,
    ) -> ManagedSipResource: ...

    def ensure_trunk(
        self,
        *,
        name: str,
        direction: str,
        max_concurrent_calls: int,
        inbound_destination: str | None = None,
        credential_id: str | None = None,
    ) -> VobizManagedTrunk: ...

    def attach_number(self, *, trunk_id: str, number: str) -> ManagedSipResource: ...

    def restore_number(self, snapshot: dict[str, object]) -> None: ...

    def delete_resource(self, resource: ManagedSipResource) -> None: ...


@dataclass(frozen=True, slots=True)
class VobizLiveKitSipConfig:
    """Deterministic desired state for a Vobiz↔LiveKit UDP SIP route."""

    number: str
    agent_name: str
    livekit_sip_uri: str
    credential_id: str
    auth_username: str
    auth_password: str = field(repr=False)
    resource_prefix: str = "voicekit"
    room_prefix: str = "call-"
    max_concurrent_calls: int = 10

    def __post_init__(self) -> None:
        validate_e164(self.number)
        match = _SIP_URI.fullmatch(self.livekit_sip_uri)
        if match is None or (match.group(2) is not None and int(match.group(2)) != 5060):
            raise VoicekitError(
                "VK-TEL-002",
                detail="Vobiz LiveKit SIP URI must be sip:host with optional UDP port 5060.",
            )
        if not _SAFE_NAME.fullmatch(self.agent_name) or not _SAFE_NAME.fullmatch(
            self.resource_prefix
        ):
            raise VoicekitError(
                "VK-TEL-002",
                detail="LiveKit agent and Vobiz SIP resource names must be lowercase slugs.",
            )
        validate_identifier(self.credential_id, field_name="Vobiz credential id")
        if not _SIP_USERNAME.fullmatch(self.auth_username):
            raise VoicekitError(
                "VK-TEL-002",
                detail="Vobiz SIP username must contain 1-100 alphanumeric characters.",
            )
        if len(self.auth_password) < 8 or len(self.auth_password) > 128:
            raise VoicekitError(
                "VK-TEL-002",
                detail="Vobiz SIP password must contain 8-128 characters.",
            )
        if not self.room_prefix or len(self.room_prefix) > 32:
            raise VoicekitError("VK-TEL-002", detail="SIP room prefix must contain 1-32 chars.")
        if not 1 <= self.max_concurrent_calls <= 1000:
            raise VoicekitError(
                "VK-TEL-002",
                detail="Vobiz max concurrent calls must be between 1 and 1000.",
            )

    @property
    def base_name(self) -> str:
        return f"{self.resource_prefix}-{self.agent_name}-{self.number.removeprefix('+')}"

    @property
    def inbound_name(self) -> str:
        return f"{self.base_name}-in"

    @property
    def outbound_name(self) -> str:
        return f"{self.base_name}-out"

    @property
    def livekit_sip_host(self) -> str:
        match = _SIP_URI.fullmatch(self.livekit_sip_uri)
        if match is None:
            raise VoicekitError("VK-TEL-002", detail="LiveKit SIP URI is invalid.")
        return match.group(1)

    @property
    def config_fingerprint(self) -> str:
        wire = {
            "number": self.number,
            "agent_name": self.agent_name,
            "livekit_sip_host": self.livekit_sip_host,
            "credential_id": self.credential_id,
            "auth_username": self.auth_username,
            "auth_password_sha256": hashlib.sha256(self.auth_password.encode()).hexdigest(),
            "room_prefix": self.room_prefix,
            "max_concurrent_calls": self.max_concurrent_calls,
            "transport": "UDP",
            "vobiz_gateway": _VOBIZ_GATEWAY,
        }
        encoded = json.dumps(wire, sort_keys=True, separators=(",", ":")).encode()
        return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


@dataclass(frozen=True, slots=True)
class VobizSipProvisioningResult:
    operation_id: str
    livekit_inbound_trunk_id: str
    livekit_outbound_trunk_id: str
    livekit_dispatch_rule_id: str
    vobiz_inbound_trunk_id: str
    vobiz_outbound_trunk_id: str
    created_resources: int


class VobizLiveKitSipProvisioner:
    """Apply Vobiz and LiveKit control planes as one reverse-rollback operation."""

    provider = "vobiz-livekit"

    def __init__(
        self,
        *,
        livekit: LiveKitSipAPI,
        vobiz: VobizSipBackend,
        ledger: TelephonyLedger,
    ) -> None:
        self.livekit = livekit
        self.vobiz = vobiz
        self.ledger = ledger

    async def provision(
        self,
        config: VobizLiveKitSipConfig,
    ) -> VobizSipProvisioningResult:
        snapshot = self.vobiz.snapshot_number(config.number)
        operation = self.ledger.prepare_provisioning(
            provider=self.provider,
            number=config.number,
            snapshot=snapshot,
            planned={
                "name": config.base_name,
                "config_fingerprint": config.config_fingerprint,
                "livekit_sip_host": config.livekit_sip_host,
                "transport": "UDP",
                "gateway_allowlist": _VOBIZ_GATEWAY,
            },
        )
        try:
            credential = self.vobiz.require_credential(
                credential_id=config.credential_id,
                username=config.auth_username,
            )
            inbound = self.vobiz.ensure_trunk(
                name=config.inbound_name,
                direction="inbound",
                max_concurrent_calls=config.max_concurrent_calls,
                inbound_destination=config.livekit_sip_host,
            )
            operation = self._record(operation, inbound.resource)
            outbound = self.vobiz.ensure_trunk(
                name=config.outbound_name,
                direction="outbound",
                max_concurrent_calls=config.max_concurrent_calls,
                credential_id=credential.resource_id,
            )
            operation = self._record(operation, outbound.resource)
            binding = self.vobiz.attach_number(
                trunk_id=inbound.resource.resource_id,
                number=config.number,
            )
            operation = self._record(operation, binding)
            livekit_inbound = await self._ensure_livekit_inbound(config)
            operation = self._record(operation, livekit_inbound)
            dispatch = await self._ensure_dispatch(
                config,
                livekit_inbound.resource_id,
            )
            operation = self._record(operation, dispatch)
            livekit_outbound = await self._ensure_livekit_outbound(
                config,
                outbound.sip_domain,
            )
            operation = self._record(operation, livekit_outbound)
            operation = self.ledger.transition_provisioning(
                operation.operation_id,
                expected=("prepared", "applying"),
                state="applied",
            )
        except VoicekitError as exc:
            if exc.code == "VK-TEL-011":
                self.ledger.transition_provisioning(
                    operation.operation_id,
                    expected=("prepared", "applying"),
                    state="ambiguous",
                )
                raise VoicekitError(
                    "VK-TEL-006",
                    detail=(
                        f"Vobiz SIP provisioning outcome is ambiguous for "
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
            raise VoicekitError(
                "VK-TEL-006",
                detail=(
                    f"Vobiz SIP provisioning outcome is ambiguous for "
                    f"{operation.operation_id!r}; reconcile before retry."
                ),
            ) from exc
        return VobizSipProvisioningResult(
            operation_id=operation.operation_id,
            livekit_inbound_trunk_id=livekit_inbound.resource_id,
            livekit_outbound_trunk_id=livekit_outbound.resource_id,
            livekit_dispatch_rule_id=dispatch.resource_id,
            vobiz_inbound_trunk_id=inbound.resource.resource_id,
            vobiz_outbound_trunk_id=outbound.resource.resource_id,
            created_resources=sum(
                bool(resource.get("created")) for resource in operation.resources
            ),
        )

    async def rollback(self, operation_id: str) -> ProvisioningRecord:
        operation = self.ledger.get_provisioning(operation_id)
        if operation.provider != self.provider:
            raise VoicekitError("VK-TEL-006", detail="provisioning token has another provider.")
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
                    await self.livekit.delete_sip_trunk(
                        lk_sip.DeleteSIPTrunkRequest(sip_trunk_id=resource.resource_id)
                    )
                elif resource.kind == "vobiz_number_binding":
                    self.vobiz.restore_number(operation.snapshot)
                else:
                    self.vobiz.delete_resource(resource)
        except Exception as exc:
            self.ledger.transition_provisioning(
                operation_id,
                expected=("rolling_back",),
                state="conflict",
            )
            raise VoicekitError(
                "VK-TEL-006",
                detail=f"Vobiz SIP rollback conflicted for {operation_id!r}.",
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

    async def _ensure_livekit_inbound(
        self,
        config: VobizLiveKitSipConfig,
    ) -> ManagedSipResource:
        response = await self.livekit.list_inbound_trunk(
            lk_sip.ListSIPInboundTrunkRequest(numbers=[config.number])
        )
        matching = [item for item in response.items if item.name == config.inbound_name]
        if len(matching) > 1:
            raise VoicekitError("VK-TEL-006", detail="duplicate managed LiveKit SIP trunks.")
        metadata = _managed_metadata(config)
        if matching:
            item = matching[0]
            if (
                list(item.numbers) != [config.number]
                or list(item.allowed_addresses) != [_VOBIZ_GATEWAY]
                or item.metadata != metadata
                or item.auth_username
                or item.auth_password
                or item.media_encryption != lk_sip.SIP_MEDIA_ENCRYPT_DISABLE
            ):
                raise VoicekitError(
                    "VK-TEL-006",
                    detail="managed Vobiz LiveKit inbound trunk differs from desired config.",
                )
            return ManagedSipResource("livekit_inbound_trunk", item.sip_trunk_id, False)
        created = await self.livekit.create_inbound_trunk(
            lk_sip.CreateSIPInboundTrunkRequest(
                trunk=lk_sip.SIPInboundTrunkInfo(
                    name=config.inbound_name,
                    metadata=metadata,
                    numbers=[config.number],
                    allowed_addresses=[_VOBIZ_GATEWAY],
                    media_encryption=lk_sip.SIP_MEDIA_ENCRYPT_DISABLE,
                )
            )
        )
        return ManagedSipResource("livekit_inbound_trunk", created.sip_trunk_id, True)

    async def _ensure_livekit_outbound(
        self,
        config: VobizLiveKitSipConfig,
        sip_domain: str,
    ) -> ManagedSipResource:
        response = await self.livekit.list_outbound_trunk(
            lk_sip.ListSIPOutboundTrunkRequest(numbers=[config.number])
        )
        matching = [item for item in response.items if item.name == config.outbound_name]
        if len(matching) > 1:
            raise VoicekitError(
                "VK-TEL-006",
                detail="duplicate managed LiveKit outbound trunks.",
            )
        metadata = _managed_metadata(config)
        if matching:
            item = matching[0]
            if (
                list(item.numbers) != [config.number]
                or item.address != sip_domain
                or item.auth_username != config.auth_username
                or item.metadata != metadata
                or item.transport != lk_sip.SIP_TRANSPORT_UDP
                or item.media_encryption != lk_sip.SIP_MEDIA_ENCRYPT_DISABLE
            ):
                raise VoicekitError(
                    "VK-TEL-006",
                    detail="managed Vobiz LiveKit outbound trunk differs from desired config.",
                )
            return ManagedSipResource("livekit_outbound_trunk", item.sip_trunk_id, False)
        created = await self.livekit.create_outbound_trunk(
            lk_sip.CreateSIPOutboundTrunkRequest(
                trunk=lk_sip.SIPOutboundTrunkInfo(
                    name=config.outbound_name,
                    metadata=metadata,
                    address=sip_domain,
                    numbers=[config.number],
                    auth_username=config.auth_username,
                    auth_password=config.auth_password,
                    transport=lk_sip.SIP_TRANSPORT_UDP,
                    media_encryption=lk_sip.SIP_MEDIA_ENCRYPT_DISABLE,
                )
            )
        )
        return ManagedSipResource("livekit_outbound_trunk", created.sip_trunk_id, True)

    async def _ensure_dispatch(
        self,
        config: VobizLiveKitSipConfig,
        trunk_id: str,
    ) -> ManagedSipResource:
        response = await self.livekit.list_dispatch_rule(
            lk_sip.ListSIPDispatchRuleRequest(trunk_ids=[trunk_id])
        )
        matching = [item for item in response.items if item.name == config.base_name]
        if len(matching) > 1:
            raise VoicekitError("VK-TEL-006", detail="duplicate managed LiveKit dispatch rules.")
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
                raise VoicekitError(
                    "VK-TEL-006",
                    detail="managed Vobiz LiveKit dispatch rule differs from desired config.",
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
                                    "provider": "vobiz",
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


class VobizSipHTTPBackend:
    """Current Vobiz trunk/credential/number API with strict ensure semantics."""

    def __init__(
        self,
        *,
        auth_id: str,
        auth_token: str,
        client: httpx.Client | None = None,
        base_url: str = "https://api.vobiz.ai",
    ) -> None:
        self.auth_id = validate_identifier(auth_id, field_name="Vobiz auth id")
        if not auth_token:
            raise VoicekitError("VK-TEL-002", detail="VOBIZ_AUTH_TOKEN is required.")
        self._client = client or httpx.Client(
            base_url=base_url.rstrip("/"),
            headers={
                "X-Auth-ID": self.auth_id,
                "X-Auth-Token": auth_token,
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            timeout=30,
            follow_redirects=False,
        )

    @property
    def account_path(self) -> str:
        return f"/api/v1/Account/{quote(self.auth_id, safe='')}"

    def snapshot_number(self, number: str) -> dict[str, object]:
        item = self._number(number)
        return {
            "number": number,
            "trunk_group_id": _optional(item.get("trunk_group_id")),
        }

    def require_credential(
        self,
        *,
        credential_id: str,
        username: str,
    ) -> ManagedSipResource:
        wanted_id = validate_identifier(credential_id, field_name="Vobiz credential id")
        matches = [
            item
            for item in self._list(f"{self.account_path}/credentials", "credentials")
            if str(item.get("id", "")) == wanted_id
        ]
        if len(matches) != 1:
            raise VoicekitError(
                "VK-TEL-006",
                detail="configured Vobiz SIP credential was not found uniquely.",
            )
        item = matches[0]
        if str(item.get("username", "")) != username or item.get("enabled") is False:
            raise VoicekitError(
                "VK-TEL-006",
                detail="configured Vobiz SIP credential differs from desired state.",
            )
        return ManagedSipResource("vobiz_credential", wanted_id, False)

    def ensure_trunk(
        self,
        *,
        name: str,
        direction: str,
        max_concurrent_calls: int,
        inbound_destination: str | None = None,
        credential_id: str | None = None,
    ) -> VobizManagedTrunk:
        if direction not in {"inbound", "outbound"}:
            raise VoicekitError("VK-TEL-002", detail="Vobiz trunk direction is invalid.")
        matches = [
            item
            for item in self._list(f"{self.account_path}/trunks", "trunks")
            if str(item.get("name", "")) == name
        ]
        if len(matches) > 1:
            raise VoicekitError("VK-TEL-006", detail="duplicate managed Vobiz trunks.")
        desired = {
            "trunk_direction": direction,
            "transport": "udp",
            "inbound_destination": inbound_destination,
            "credential_uuid": credential_id,
        }
        if matches:
            trunk_id = _identifier(matches[0].get("trunk_id"), "Vobiz trunk id")
            item = self._object(
                self._request(
                    "GET",
                    f"{self.account_path}/trunks/{quote(trunk_id, safe='')}",
                    expected=(200,),
                    operation=f"retrieve {direction} trunk",
                )
            )
            if (
                any(
                    _optional(item.get(field)) != expected
                    for field, expected in desired.items()
                    if expected is not None
                )
                or int(str(item.get("concurrent_calls_limit", -1))) != max_concurrent_calls
            ):
                raise VoicekitError(
                    "VK-TEL-006",
                    detail=f"managed Vobiz {direction} trunk differs from desired config.",
                )
            return VobizManagedTrunk(
                ManagedSipResource(
                    f"vobiz_{direction}_trunk",
                    trunk_id,
                    False,
                ),
                _sip_domain(item.get("trunk_domain")),
            )
        body: dict[str, object] = {
            "name": name,
            "trunk_direction": direction,
            "trunk_type": direction.upper(),
            "trunk_status": "enabled",
            "transport": "udp",
            "secure": False,
            "max_concurrent_calls": max_concurrent_calls,
            "concurrent_calls_limit": max_concurrent_calls,
        }
        if inbound_destination is not None:
            body["inbound_destination"] = inbound_destination
        if credential_id is not None:
            body["credential_uuid"] = credential_id
        item = self._object(
            self._request(
                "POST",
                f"{self.account_path}/trunks",
                json_body=body,
                expected=(200, 201),
                operation=f"create {direction} trunk",
            )
        )
        return VobizManagedTrunk(
            ManagedSipResource(
                f"vobiz_{direction}_trunk",
                _identifier(item.get("trunk_id"), "Vobiz trunk id"),
                True,
            ),
            _sip_domain(item.get("trunk_domain")),
        )

    def attach_number(self, *, trunk_id: str, number: str) -> ManagedSipResource:
        validated = validate_e164(number)
        desired = validate_identifier(trunk_id, field_name="Vobiz trunk id")
        current = _optional(self._number(validated).get("trunk_group_id"))
        if current == desired:
            return ManagedSipResource("vobiz_number_binding", validated, False)
        self._request(
            "POST",
            f"{self.account_path}/numbers/{quote(validated, safe='')}/assign",
            json_body={"trunk_group_id": desired},
            expected=(200,),
            operation="assign number to trunk",
        )
        if _optional(self._number(validated).get("trunk_group_id")) != desired:
            raise VoicekitError(
                "VK-TEL-011",
                detail="Vobiz did not confirm the number-to-trunk binding.",
            )
        return ManagedSipResource("vobiz_number_binding", validated, True)

    def restore_number(self, snapshot: dict[str, object]) -> None:
        number = validate_e164(str(snapshot.get("number", "")))
        previous = _optional(snapshot.get("trunk_group_id"))
        path = f"{self.account_path}/numbers/{quote(number, safe='')}/assign"
        if previous is None:
            self._request(
                "DELETE",
                path,
                expected=(200, 204, 404),
                operation="unassign number from trunk",
            )
        else:
            self._request(
                "POST",
                path,
                json_body={"trunk_group_id": previous},
                expected=(200,),
                operation="restore number trunk",
            )
        if _optional(self._number(number).get("trunk_group_id")) != previous:
            raise VoicekitError(
                "VK-TEL-006",
                detail="Vobiz number route rollback did not compare equal.",
            )

    def delete_resource(self, resource: ManagedSipResource) -> None:
        if resource.kind not in {"vobiz_inbound_trunk", "vobiz_outbound_trunk"}:
            raise VoicekitError(
                "VK-TEL-006",
                detail=f"unknown Vobiz SIP rollback resource {resource.kind!r}.",
            )
        self._request(
            "DELETE",
            f"{self.account_path}/trunks/{quote(resource.resource_id, safe='')}",
            expected=(200, 204, 404),
            operation="delete trunk",
        )

    def _number(self, number: str) -> dict[str, object]:
        validated = validate_e164(number)
        matches = [
            item
            for item in self._list(f"{self.account_path}/numbers", "numbers")
            if str(
                item.get(
                    "e164",
                    item.get("number", item.get("phone_number", "")),
                )
            )
            == validated
        ]
        if len(matches) != 1:
            raise VoicekitError(
                "VK-TEL-006",
                detail=f"Vobiz number lookup returned {len(matches)} exact matches.",
            )
        return matches[0]

    def _list(self, path: str, key: str) -> list[dict[str, object]]:
        value = self._request("GET", path, expected=(200,), operation=f"list {key}")
        if isinstance(value, list):
            raw = cast("list[object]", value)
        elif isinstance(value, dict):
            document = cast("dict[str, object]", value)
            candidate = document.get(key, document.get("objects", document.get("data", [])))
            if not isinstance(candidate, list):
                raise VoicekitError("VK-TEL-011", detail=f"Vobiz {key} list is malformed.")
            raw = cast("list[object]", candidate)
        else:
            raise VoicekitError("VK-TEL-011", detail=f"Vobiz {key} list is malformed.")
        if not all(isinstance(item, dict) for item in raw):
            raise VoicekitError("VK-TEL-011", detail=f"Vobiz {key} item is malformed.")
        return [cast("dict[str, object]", item) for item in raw]

    def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, object] | None = None,
        expected: tuple[int, ...],
        operation: str,
    ) -> object:
        try:
            response = self._client.request(method, path, json=json_body)
        except httpx.HTTPError as exc:
            raise VoicekitError(
                "VK-TEL-011",
                detail=f"Vobiz {operation} did not return a definitive result.",
            ) from exc
        if response.status_code not in expected:
            code = "VK-TEL-011" if response.status_code >= 500 else "VK-TEL-006"
            raise VoicekitError(
                code,
                detail=f"Vobiz {operation} http_{response.status_code}.",
            )
        if response.status_code == 204 or not response.content:
            return {}
        try:
            return cast("object", response.json())
        except ValueError as exc:
            raise VoicekitError(
                "VK-TEL-011",
                detail=f"Vobiz {operation} returned invalid JSON.",
            ) from exc

    @staticmethod
    def _object(value: object) -> dict[str, object]:
        if not isinstance(value, dict):
            raise VoicekitError("VK-TEL-011", detail="Vobiz response object is malformed.")
        return cast("dict[str, object]", value)


def _managed_metadata(config: VobizLiveKitSipConfig) -> str:
    return json.dumps(
        {
            "managed_by": "voicekit",
            "provider": "vobiz",
            "config_fingerprint": config.config_fingerprint,
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def _resource(wire: Mapping[str, object]) -> ManagedSipResource:
    return ManagedSipResource(
        kind=str(wire["kind"]),
        resource_id=str(wire["resource_id"]),
        created=bool(wire["created"]),
        parent_id=_optional(wire.get("parent_id")),
    )


def _optional(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _identifier(value: object, field_name: str) -> str:
    return validate_identifier(str(value or ""), field_name=field_name)


def _sip_domain(value: object) -> str:
    domain = str(value or "").casefold()
    if not _SIP_DOMAIN.fullmatch(domain):
        raise VoicekitError("VK-TEL-006", detail="Vobiz returned an invalid SIP domain.")
    return domain
