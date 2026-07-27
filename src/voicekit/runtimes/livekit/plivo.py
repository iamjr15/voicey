"""Crash-safe Plivo Zentrunk provisioning for LiveKit."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Protocol, cast
from urllib.parse import quote, urlsplit

import httpx
from livekit.protocol import agent_dispatch as lk_dispatch
from livekit.protocol import room as lk_room
from livekit.protocol import sip as lk_sip

from voicekit.errors import VoicekitError
from voicekit.runtimes.livekit.sip import LiveKitSipAPI, ManagedSipResource
from voicekit.telephony.ledger import ProvisioningRecord, TelephonyLedger
from voicekit.telephony.models import validate_e164, validate_identifier

_AUTH_ID = re.compile(r"^(?:MA|SA)[A-Za-z0-9]{18}$")
_SAFE_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
_SIP_URI = re.compile(r"^sip:([a-z0-9](?:[a-z0-9.-]*[a-z0-9])?)(?::([0-9]{1,5}))?$")
_SIP_DOMAIN = re.compile(r"^[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?(?::[0-9]{1,5})?$")
_SIP_USERNAME = re.compile(r"^[A-Za-z0-9]{5,20}$")


@dataclass(frozen=True, slots=True)
class PlivoManagedTrunk:
    resource: ManagedSipResource
    sip_domain: str


class PlivoSipBackend(Protocol):
    """Verifiable Plivo Zentrunk resources needed by the LiveKit route."""

    def snapshot_number(self, number: str) -> dict[str, object]: ...

    def ensure_uri(self, *, name: str, uri: str) -> ManagedSipResource: ...

    def ensure_credential(
        self,
        *,
        name: str,
        username: str,
        password: str,
    ) -> ManagedSipResource: ...

    def ensure_trunk(
        self,
        *,
        name: str,
        direction: str,
        primary_uri_uuid: str | None = None,
        credential_uuid: str | None = None,
        secure: bool,
    ) -> PlivoManagedTrunk: ...

    def attach_number(self, *, trunk_id: str, number: str) -> ManagedSipResource: ...

    def restore_number(self, snapshot: dict[str, object]) -> None: ...

    def delete_resource(self, resource: ManagedSipResource) -> None: ...


@dataclass(frozen=True, slots=True)
class PlivoLiveKitSipConfig:
    """Official Plivo↔LiveKit SIP desired state."""

    number: str
    agent_name: str
    livekit_sip_uri: str
    auth_username: str
    auth_password: str = field(repr=False)
    resource_prefix: str = "voicekit"
    room_prefix: str = "call-"

    def __post_init__(self) -> None:
        validate_e164(self.number)
        match = _SIP_URI.fullmatch(self.livekit_sip_uri.casefold())
        if match is None:
            raise VoicekitError(
                "VK-TEL-002",
                detail="Plivo LiveKit SIP URI must be sip:host with optional port.",
            )
        if match.group(2) is not None and int(match.group(2)) != 5060:
            raise VoicekitError(
                "VK-TEL-002",
                detail="Plivo's documented LiveKit origination path requires TCP port 5060.",
            )
        if not _SAFE_NAME.fullmatch(self.agent_name) or not _SAFE_NAME.fullmatch(
            self.resource_prefix
        ):
            raise VoicekitError(
                "VK-TEL-002",
                detail="LiveKit agent and Plivo SIP resource names must be lowercase slugs.",
            )
        if not _SIP_USERNAME.fullmatch(self.auth_username):
            raise VoicekitError(
                "VK-TEL-002",
                detail="Plivo SIP username must contain 5-20 alphanumeric characters.",
            )
        if not 5 <= len(self.auth_password) <= 20 or not any(
            not value.isalnum() for value in self.auth_password
        ):
            raise VoicekitError(
                "VK-TEL-002",
                detail="Plivo SIP password must contain 5-20 chars and a special character.",
            )
        if not self.room_prefix or len(self.room_prefix) > 32:
            raise VoicekitError("VK-TEL-002", detail="SIP room prefix must contain 1-32 chars.")

    @property
    def base_name(self) -> str:
        return f"{self.resource_prefix}-{self.agent_name}-{self.number.removeprefix('+')}"

    @property
    def livekit_sip_host(self) -> str:
        match = _SIP_URI.fullmatch(self.livekit_sip_uri.casefold())
        if match is None:
            raise VoicekitError("VK-TEL-002", detail="LiveKit SIP URI is invalid.")
        return match.group(1)

    @property
    def origination_uri(self) -> str:
        return f"{self.livekit_sip_host};transport=tcp"

    @property
    def config_fingerprint(self) -> str:
        wire = {
            "number": self.number,
            "agent_name": self.agent_name,
            "livekit_sip_host": self.livekit_sip_host,
            "auth_username": self.auth_username,
            "auth_password_sha256": hashlib.sha256(self.auth_password.encode()).hexdigest(),
            "room_prefix": self.room_prefix,
            "inbound_transport": "TCP",
            "outbound_transport": "TLS",
            "outbound_media_encryption": "REQUIRE",
        }
        encoded = json.dumps(wire, sort_keys=True, separators=(",", ":")).encode()
        return f"sha256:{hashlib.sha256(encoded).hexdigest()}"

    @property
    def credential_name(self) -> str:
        """Bind adoption to the write-only password without exposing it."""
        password_hash = hashlib.sha256(self.auth_password.encode()).hexdigest()[:12]
        return f"{self.base_name}-credential-{password_hash}"


@dataclass(frozen=True, slots=True)
class PlivoSipProvisioningResult:
    operation_id: str
    livekit_inbound_trunk_id: str
    livekit_outbound_trunk_id: str
    livekit_dispatch_rule_id: str
    plivo_inbound_trunk_id: str
    plivo_outbound_trunk_id: str
    created_resources: int


class PlivoLiveKitSipProvisioner:
    """Apply the Plivo and LiveKit control planes as one ledgered operation."""

    provider = "plivo-livekit"

    def __init__(
        self,
        *,
        livekit: LiveKitSipAPI,
        plivo: PlivoSipBackend,
        ledger: TelephonyLedger,
    ) -> None:
        self.livekit = livekit
        self.plivo = plivo
        self.ledger = ledger

    async def provision(self, config: PlivoLiveKitSipConfig) -> PlivoSipProvisioningResult:
        snapshot = self.plivo.snapshot_number(config.number)
        operation = self.ledger.prepare_provisioning(
            provider=self.provider,
            number=config.number,
            snapshot=snapshot,
            planned={
                "name": config.base_name,
                "config_fingerprint": config.config_fingerprint,
                "livekit_sip_host": config.livekit_sip_host,
                "inbound_transport": "TCP",
                "outbound_transport": "TLS",
                "secure_outbound": True,
            },
        )
        try:
            uri = self.plivo.ensure_uri(
                name=f"{config.base_name}-uri",
                uri=config.origination_uri,
            )
            operation = self._record(operation, uri)
            inbound = self.plivo.ensure_trunk(
                name=f"{config.base_name}-in",
                direction="inbound",
                primary_uri_uuid=uri.resource_id,
                secure=False,
            )
            operation = self._record(operation, inbound.resource)
            credential = self.plivo.ensure_credential(
                name=config.credential_name,
                username=config.auth_username,
                password=config.auth_password,
            )
            operation = self._record(operation, credential)
            outbound = self.plivo.ensure_trunk(
                name=f"{config.base_name}-out",
                direction="outbound",
                credential_uuid=credential.resource_id,
                secure=True,
            )
            operation = self._record(operation, outbound.resource)
            binding = self.plivo.attach_number(
                trunk_id=inbound.resource.resource_id,
                number=config.number,
            )
            operation = self._record(operation, binding)
            livekit_inbound = await self._ensure_livekit_inbound(config)
            operation = self._record(operation, livekit_inbound)
            dispatch = await self._ensure_dispatch(config, livekit_inbound.resource_id)
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
                        f"Plivo SIP provisioning outcome is ambiguous for "
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
                    f"Plivo SIP provisioning outcome is ambiguous for "
                    f"{operation.operation_id!r}; reconcile before retry."
                ),
            ) from exc
        return PlivoSipProvisioningResult(
            operation_id=operation.operation_id,
            livekit_inbound_trunk_id=livekit_inbound.resource_id,
            livekit_outbound_trunk_id=livekit_outbound.resource_id,
            livekit_dispatch_rule_id=dispatch.resource_id,
            plivo_inbound_trunk_id=inbound.resource.resource_id,
            plivo_outbound_trunk_id=outbound.resource.resource_id,
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
                elif resource.kind == "plivo_number_binding":
                    self.plivo.restore_number(operation.snapshot)
                else:
                    self.plivo.delete_resource(resource)
        except Exception as exc:
            self.ledger.transition_provisioning(
                operation_id,
                expected=("rolling_back",),
                state="conflict",
            )
            raise VoicekitError(
                "VK-TEL-006",
                detail=f"Plivo SIP rollback conflicted for {operation_id!r}.",
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
        config: PlivoLiveKitSipConfig,
    ) -> ManagedSipResource:
        response = await self.livekit.list_inbound_trunk(
            lk_sip.ListSIPInboundTrunkRequest(numbers=[config.number])
        )
        name = f"{config.base_name}-in"
        matching = [item for item in response.items if item.name == name]
        if len(matching) > 1:
            raise VoicekitError("VK-TEL-006", detail="duplicate managed LiveKit SIP trunks.")
        metadata = _metadata(config)
        if matching:
            item = matching[0]
            if (
                list(item.numbers) != [config.number]
                or item.auth_username
                or item.auth_password
                or item.metadata != metadata
                or item.media_encryption != lk_sip.SIP_MEDIA_ENCRYPT_DISABLE
            ):
                raise VoicekitError(
                    "VK-TEL-006",
                    detail="managed Plivo LiveKit inbound trunk differs from desired config.",
                )
            return ManagedSipResource("livekit_inbound_trunk", item.sip_trunk_id, False)
        created = await self.livekit.create_inbound_trunk(
            lk_sip.CreateSIPInboundTrunkRequest(
                trunk=lk_sip.SIPInboundTrunkInfo(
                    name=name,
                    metadata=metadata,
                    numbers=[config.number],
                    media_encryption=lk_sip.SIP_MEDIA_ENCRYPT_DISABLE,
                )
            )
        )
        return ManagedSipResource("livekit_inbound_trunk", created.sip_trunk_id, True)

    async def _ensure_livekit_outbound(
        self,
        config: PlivoLiveKitSipConfig,
        sip_domain: str,
    ) -> ManagedSipResource:
        response = await self.livekit.list_outbound_trunk(
            lk_sip.ListSIPOutboundTrunkRequest(numbers=[config.number])
        )
        name = f"{config.base_name}-out"
        matching = [item for item in response.items if item.name == name]
        if len(matching) > 1:
            raise VoicekitError(
                "VK-TEL-006",
                detail="duplicate managed LiveKit outbound trunks.",
            )
        metadata = _metadata(config)
        if matching:
            item = matching[0]
            if (
                list(item.numbers) != [config.number]
                or item.address != sip_domain
                or item.auth_username != config.auth_username
                or item.metadata != metadata
                or item.transport != lk_sip.SIP_TRANSPORT_TLS
                or item.media_encryption != lk_sip.SIP_MEDIA_ENCRYPT_REQUIRE
            ):
                raise VoicekitError(
                    "VK-TEL-006",
                    detail="managed Plivo LiveKit outbound trunk differs from desired config.",
                )
            return ManagedSipResource("livekit_outbound_trunk", item.sip_trunk_id, False)
        created = await self.livekit.create_outbound_trunk(
            lk_sip.CreateSIPOutboundTrunkRequest(
                trunk=lk_sip.SIPOutboundTrunkInfo(
                    name=name,
                    metadata=metadata,
                    address=sip_domain,
                    numbers=[config.number],
                    auth_username=config.auth_username,
                    auth_password=config.auth_password,
                    transport=lk_sip.SIP_TRANSPORT_TLS,
                    media_encryption=lk_sip.SIP_MEDIA_ENCRYPT_REQUIRE,
                )
            )
        )
        return ManagedSipResource("livekit_outbound_trunk", created.sip_trunk_id, True)

    async def _ensure_dispatch(
        self,
        config: PlivoLiveKitSipConfig,
        trunk_id: str,
    ) -> ManagedSipResource:
        response = await self.livekit.list_dispatch_rule(
            lk_sip.ListSIPDispatchRuleRequest(trunk_ids=[trunk_id])
        )
        matching = [item for item in response.items if item.name == config.base_name]
        if len(matching) > 1:
            raise VoicekitError("VK-TEL-006", detail="duplicate managed LiveKit dispatch rules.")
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
                raise VoicekitError(
                    "VK-TEL-006",
                    detail="managed Plivo LiveKit dispatch differs from desired config.",
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
                                    "provider": "plivo",
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


class PlivoSipHTTPBackend:
    """Current Plivo Zentrunk API with strict ensure and rollback semantics."""

    def __init__(
        self,
        *,
        auth_id: str,
        auth_token: str,
        client: httpx.Client | None = None,
        base_url: str = "https://api.plivo.com",
    ) -> None:
        if not _AUTH_ID.fullmatch(auth_id):
            raise VoicekitError("VK-TEL-002", detail="PLIVO_AUTH_ID is missing or invalid.")
        if not auth_token:
            raise VoicekitError("VK-TEL-002", detail="PLIVO_AUTH_TOKEN is required.")
        parsed = urlsplit(base_url)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise VoicekitError(
                "VK-TEL-002",
                detail="Plivo SIP API base URL must be normalized HTTPS.",
            )
        self.auth_id = auth_id
        self._client = client or httpx.Client(
            base_url=base_url.rstrip("/"),
            auth=httpx.BasicAuth(auth_id, auth_token),
            headers={"Accept": "application/json", "Content-Type": "application/json"},
            timeout=30,
            follow_redirects=False,
        )

    @property
    def account_path(self) -> str:
        return f"/v1/Account/{quote(self.auth_id, safe='')}"

    def snapshot_number(self, number: str) -> dict[str, object]:
        item = self._number(number)
        return {"number": number, "app_id": _optional(item.get("app_id"))}

    def ensure_uri(self, *, name: str, uri: str) -> ManagedSipResource:
        matches = [item for item in self._list("URI") if str(item.get("name", "")) == name]
        if len(matches) > 1:
            raise VoicekitError("VK-TEL-006", detail="duplicate managed Plivo SIP URIs.")
        if matches:
            item = matches[0]
            if str(item.get("uri", "")) != uri:
                raise VoicekitError(
                    "VK-TEL-006",
                    detail="managed Plivo SIP URI differs from desired config.",
                )
            return ManagedSipResource(
                "plivo_uri",
                _identifier(item.get("uri_uuid"), "Plivo URI id"),
                False,
            )
        created = self._request(
            "POST",
            f"{self.account_path}/Zentrunk/URI/",
            json_body={"name": name, "uri": uri},
            expected=(200, 201, 202),
            operation="create origination URI",
        )
        return ManagedSipResource(
            "plivo_uri",
            _identifier(created.get("uri_uuid"), "Plivo URI id"),
            True,
        )

    def ensure_credential(
        self,
        *,
        name: str,
        username: str,
        password: str,
    ) -> ManagedSipResource:
        matches = [item for item in self._list("Credential") if str(item.get("name", "")) == name]
        if len(matches) > 1:
            raise VoicekitError("VK-TEL-006", detail="duplicate managed Plivo credentials.")
        if matches:
            item = matches[0]
            if str(item.get("username", "")) != username:
                raise VoicekitError(
                    "VK-TEL-006",
                    detail="managed Plivo credential differs from desired config.",
                )
            return ManagedSipResource(
                "plivo_credential",
                _identifier(item.get("credential_uuid"), "Plivo credential id"),
                False,
            )
        created = self._request(
            "POST",
            f"{self.account_path}/Zentrunk/Credential/",
            json_body={"name": name, "username": username, "password": password},
            expected=(200, 201, 202),
            operation="create credential",
        )
        return ManagedSipResource(
            "plivo_credential",
            _identifier(created.get("credential_uuid"), "Plivo credential id"),
            True,
        )

    def ensure_trunk(
        self,
        *,
        name: str,
        direction: str,
        primary_uri_uuid: str | None = None,
        credential_uuid: str | None = None,
        secure: bool,
    ) -> PlivoManagedTrunk:
        if direction not in {"inbound", "outbound"}:
            raise VoicekitError("VK-TEL-002", detail="Plivo trunk direction is invalid.")
        matches = [item for item in self._list("Trunk") if str(item.get("name", "")) == name]
        if len(matches) > 1:
            raise VoicekitError("VK-TEL-006", detail="duplicate managed Plivo trunks.")
        desired = {
            "trunk_direction": direction,
            "primary_uri_uuid": primary_uri_uuid,
            "credential_uuid": credential_uuid,
            "secure": secure,
        }
        if matches:
            trunk_id = _identifier(matches[0].get("trunk_id"), "Plivo trunk id")
            item = self._trunk(trunk_id, fallback=matches[0])
            if (
                str(item.get("trunk_direction", "")) != direction
                or bool(item.get("secure", False)) != secure
                or any(
                    _optional(item.get(field)) != expected
                    for field, expected in desired.items()
                    if field not in {"trunk_direction", "secure"} and expected is not None
                )
            ):
                raise VoicekitError(
                    "VK-TEL-006",
                    detail=f"managed Plivo {direction} trunk differs from desired config.",
                )
            return PlivoManagedTrunk(
                ManagedSipResource(f"plivo_{direction}_trunk", trunk_id, False),
                _sip_domain(item.get("trunk_domain")),
            )
        body: dict[str, object] = {
            "name": name,
            "trunk_direction": direction,
            "secure": secure,
        }
        if primary_uri_uuid is not None:
            body["primary_uri_uuid"] = primary_uri_uuid
        if credential_uuid is not None:
            body["credential_uuid"] = credential_uuid
        created = self._request(
            "POST",
            f"{self.account_path}/Zentrunk/Trunk/",
            json_body=body,
            expected=(200, 201, 202),
            operation=f"create {direction} trunk",
        )
        trunk_id = _identifier(created.get("trunk_id"), "Plivo trunk id")
        item = self._trunk(trunk_id, fallback=created)
        return PlivoManagedTrunk(
            ManagedSipResource(f"plivo_{direction}_trunk", trunk_id, True),
            _sip_domain(item.get("trunk_domain")),
        )

    def attach_number(self, *, trunk_id: str, number: str) -> ManagedSipResource:
        normalized = validate_e164(number)
        desired = validate_identifier(trunk_id, field_name="Plivo trunk id")
        current = _optional(self._number(normalized).get("app_id"))
        if current == desired:
            return ManagedSipResource("plivo_number_binding", normalized, False)
        self._update_number(normalized, desired)
        if _optional(self._number(normalized).get("app_id")) != desired:
            raise VoicekitError(
                "VK-TEL-011",
                detail="Plivo did not confirm the number-to-trunk binding.",
            )
        return ManagedSipResource("plivo_number_binding", normalized, True)

    def restore_number(self, snapshot: dict[str, object]) -> None:
        number = validate_e164(str(snapshot.get("number", "")))
        previous = _optional(snapshot.get("app_id"))
        self._update_number(number, previous)
        if _optional(self._number(number).get("app_id")) != previous:
            raise VoicekitError(
                "VK-TEL-006",
                detail="Plivo number route rollback did not compare equal.",
            )

    def delete_resource(self, resource: ManagedSipResource) -> None:
        mapping = {
            "plivo_uri": "URI",
            "plivo_credential": "Credential",
            "plivo_inbound_trunk": "Trunk",
            "plivo_outbound_trunk": "Trunk",
        }
        kind = mapping.get(resource.kind)
        if kind is None:
            raise VoicekitError(
                "VK-TEL-006",
                detail=f"unknown Plivo SIP rollback resource {resource.kind!r}.",
            )
        self._request(
            "DELETE",
            f"{self.account_path}/Zentrunk/{kind}/{quote(resource.resource_id, safe='')}/",
            expected=(200, 202, 204, 404),
            operation=f"delete {kind.casefold()}",
        )

    def _list(self, resource: str) -> list[dict[str, object]]:
        value = self._request(
            "GET",
            f"{self.account_path}/Zentrunk/{resource}/",
            expected=(200,),
            operation=f"list {resource.casefold()}",
        )
        candidate = value.get(
            "objects",
            value.get(f"{resource.casefold()}s", value.get("data", [])),
        )
        if not isinstance(candidate, list):
            raise VoicekitError(
                "VK-TEL-011",
                detail=f"Plivo {resource.casefold()} list is malformed.",
            )
        raw = cast("list[object]", candidate)
        if any(not isinstance(item, dict) for item in raw):
            raise VoicekitError(
                "VK-TEL-011",
                detail=f"Plivo {resource.casefold()} item is malformed.",
            )
        return cast("list[dict[str, object]]", raw)

    def _trunk(
        self,
        trunk_id: str,
        *,
        fallback: dict[str, object],
    ) -> dict[str, object]:
        if fallback.get("trunk_domain") is not None:
            return fallback
        return self._request(
            "GET",
            f"{self.account_path}/Zentrunk/Trunk/{quote(trunk_id, safe='')}/",
            expected=(200,),
            operation="retrieve trunk",
        )

    def _number(self, number: str) -> dict[str, object]:
        normalized = validate_e164(number)
        return self._request(
            "GET",
            f"{self.account_path}/Number/{quote(normalized.removeprefix('+'), safe='')}/",
            expected=(200,),
            operation="retrieve number",
        )

    def _update_number(self, number: str, app_id: str | None) -> None:
        normalized = validate_e164(number)
        self._request(
            "POST",
            f"{self.account_path}/Number/{quote(normalized.removeprefix('+'), safe='')}/",
            json_body={"app_id": app_id or ""},
            expected=(200, 202),
            operation="assign number to trunk",
        )

    def _request(
        self,
        method: str,
        path: str,
        *,
        expected: tuple[int, ...],
        operation: str,
        json_body: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        try:
            response = self._client.request(method, path, json=json_body)
        except httpx.HTTPError as exc:
            raise VoicekitError(
                "VK-TEL-011",
                detail=f"Plivo {operation} did not return a definitive result.",
            ) from exc
        if response.status_code not in expected:
            code = "VK-TEL-011" if response.status_code >= 500 else "VK-TEL-006"
            raise VoicekitError(
                code,
                detail=f"Plivo {operation} http_{response.status_code}.",
            )
        if response.status_code == 204 or not response.content:
            return {}
        try:
            document = response.json()
        except ValueError as exc:
            raise VoicekitError(
                "VK-TEL-011",
                detail=f"Plivo {operation} returned invalid JSON.",
            ) from exc
        if not isinstance(document, dict):
            raise VoicekitError(
                "VK-TEL-011",
                detail=f"Plivo {operation} returned an invalid envelope.",
            )
        return cast("dict[str, object]", document)


def _metadata(config: PlivoLiveKitSipConfig) -> str:
    return json.dumps(
        {
            "managed_by": "voicekit",
            "provider": "plivo",
            "tier": "beta",
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
        raise VoicekitError("VK-TEL-006", detail="Plivo returned an invalid SIP domain.")
    return domain
