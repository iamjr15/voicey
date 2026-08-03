# pyright: reportPrivateUsage=false

"""Offline beta certification for Plivo Zentrunk into LiveKit."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from livekit.protocol import sip as lk_sip

from voicey.errors import VoiceyError
from voicey.runtimes.livekit.plivo import (
    PlivoLiveKitSipConfig,
    PlivoLiveKitSipProvisioner,
    PlivoManagedTrunk,
    PlivoSipBackend,
    PlivoSipHTTPBackend,
)
from voicey.runtimes.livekit.sip import LiveKitSipAPI, ManagedSipResource
from voicey.telephony.ledger import TelephonyLedger

AUTH_ID = "MA000000000000000000"
AUTH_TOKEN = "not-a-real-plivo-token"  # pragma: allowlist secret
NUMBER = "+14155550100"


class FakeLiveKit:
    def __init__(self) -> None:
        self.inbound: list[lk_sip.SIPInboundTrunkInfo] = []
        self.outbound: list[lk_sip.SIPOutboundTrunkInfo] = []
        self.dispatch: list[lk_sip.SIPDispatchRuleInfo] = []
        self.deleted: list[tuple[str, str]] = []

    async def list_inbound_trunk(
        self,
        _request: lk_sip.ListSIPInboundTrunkRequest,
    ) -> lk_sip.ListSIPInboundTrunkResponse:
        return lk_sip.ListSIPInboundTrunkResponse(items=self.inbound)

    async def create_inbound_trunk(
        self,
        request: lk_sip.CreateSIPInboundTrunkRequest,
    ) -> lk_sip.SIPInboundTrunkInfo:
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


class FakePlivo:
    def __init__(self) -> None:
        self.app_id = "old-app"
        self.uri: ManagedSipResource | None = None
        self.credential: ManagedSipResource | None = None
        self.inbound: PlivoManagedTrunk | None = None
        self.outbound: PlivoManagedTrunk | None = None
        self.deleted: list[str] = []
        self.restored: list[dict[str, object]] = []
        self.fail_at: str | None = None
        self.fail_error: Exception = VoiceyError("VY-TEL-004", detail="definitive")

    def snapshot_number(self, number: str) -> dict[str, object]:
        assert number == NUMBER
        return {"number": number, "app_id": self.app_id}

    def ensure_uri(self, *, name: str, uri: str) -> ManagedSipResource:
        self._fail("uri")
        assert name.endswith("-uri")
        assert uri == "project.sip.livekit.cloud;transport=tcp"
        if self.uri is None:
            self.uri = ManagedSipResource("plivo_uri", "uri-1", True)
            return self.uri
        return ManagedSipResource("plivo_uri", "uri-1", False)

    def ensure_credential(
        self,
        *,
        name: str,
        username: str,
        password: str,
    ) -> ManagedSipResource:
        self._fail("credential")
        assert name.endswith("-credential-e32111f11142")
        assert username == "voiceyuser"
        assert password == "secret!"  # pragma: allowlist secret
        if self.credential is None:
            self.credential = ManagedSipResource("plivo_credential", "credential-1", True)
            return self.credential
        return ManagedSipResource("plivo_credential", "credential-1", False)

    def ensure_trunk(
        self,
        *,
        name: str,
        direction: str,
        primary_uri_uuid: str | None = None,
        credential_uuid: str | None = None,
        secure: bool,
    ) -> PlivoManagedTrunk:
        self._fail(direction)
        if direction == "inbound":
            assert name.endswith("-in")
            assert primary_uri_uuid == "uri-1"
            assert credential_uuid is None
            assert not secure
            if self.inbound is None:
                self.inbound = PlivoManagedTrunk(
                    ManagedSipResource("plivo_inbound_trunk", "plivo-in-1", True),
                    "inbound.sip.plivo.com",
                )
                return self.inbound
            return PlivoManagedTrunk(
                ManagedSipResource("plivo_inbound_trunk", "plivo-in-1", False),
                self.inbound.sip_domain,
            )
        assert name.endswith("-out")
        assert credential_uuid == "credential-1"
        assert primary_uri_uuid is None
        assert secure
        if self.outbound is None:
            self.outbound = PlivoManagedTrunk(
                ManagedSipResource("plivo_outbound_trunk", "plivo-out-1", True),
                "outbound.sip.plivo.com",
            )
            return self.outbound
        return PlivoManagedTrunk(
            ManagedSipResource("plivo_outbound_trunk", "plivo-out-1", False),
            self.outbound.sip_domain,
        )

    def attach_number(self, *, trunk_id: str, number: str) -> ManagedSipResource:
        self._fail("binding")
        assert number == NUMBER
        if self.app_id == trunk_id:
            return ManagedSipResource("plivo_number_binding", number, False)
        self.app_id = trunk_id
        return ManagedSipResource("plivo_number_binding", number, True)

    def restore_number(self, snapshot: dict[str, object]) -> None:
        self.restored.append(snapshot)
        self.app_id = str(snapshot["app_id"])

    def delete_resource(self, resource: ManagedSipResource) -> None:
        self.deleted.append(resource.kind)
        if resource.kind == "plivo_outbound_trunk":
            self.outbound = None
        elif resource.kind == "plivo_inbound_trunk":
            self.inbound = None
        elif resource.kind == "plivo_credential":
            self.credential = None
        elif resource.kind == "plivo_uri":
            self.uri = None

    def _fail(self, point: str) -> None:
        if self.fail_at == point:
            raise self.fail_error


def _config(**values: object) -> PlivoLiveKitSipConfig:
    defaults: dict[str, object] = {
        "number": NUMBER,
        "agent_name": "booking",
        "livekit_sip_uri": "sip:project.sip.livekit.cloud",
        "auth_username": "voiceyuser",
        "auth_password": "secret!",  # pragma: allowlist secret
    }
    return PlivoLiveKitSipConfig(**cast("Any", {**defaults, **values}))


async def test_provisions_official_plivo_livekit_transport_and_security(tmp_path: Path) -> None:
    livekit = FakeLiveKit()
    plivo = FakePlivo()
    ledger = TelephonyLedger(tmp_path / "plivo-livekit.sqlite3")
    provisioner = PlivoLiveKitSipProvisioner(
        livekit=cast("LiveKitSipAPI", livekit),
        plivo=cast("PlivoSipBackend", plivo),
        ledger=ledger,
    )
    try:
        result = await provisioner.provision(_config())
        assert result.created_resources == 8
        assert ledger.get_provisioning(result.operation_id).state == "applied"
        inbound = livekit.inbound[0]
        outbound = livekit.outbound[0]
        dispatch = livekit.dispatch[0]
        assert inbound.numbers == [NUMBER]
        assert inbound.media_encryption == lk_sip.SIP_MEDIA_ENCRYPT_DISABLE
        assert outbound.address == "outbound.sip.plivo.com"
        assert outbound.transport == lk_sip.SIP_TRANSPORT_TLS
        assert outbound.media_encryption == lk_sip.SIP_MEDIA_ENCRYPT_REQUIRE
        assert outbound.auth_username == "voiceyuser"
        assert json.loads(dispatch.room_config.agents[0].metadata)["tier"] == "beta"
    finally:
        ledger.close()


async def test_idempotent_provision_and_reverse_rollback(tmp_path: Path) -> None:
    livekit = FakeLiveKit()
    plivo = FakePlivo()
    ledger = TelephonyLedger(tmp_path / "plivo-idempotent.sqlite3")
    provisioner = PlivoLiveKitSipProvisioner(
        livekit=cast("LiveKitSipAPI", livekit),
        plivo=cast("PlivoSipBackend", plivo),
        ledger=ledger,
    )
    try:
        first = await provisioner.provision(_config())
        second = await provisioner.provision(_config())
        assert first.created_resources == 8
        assert second.created_resources == 0
        await provisioner.rollback(first.operation_id)
        await provisioner.rollback(first.operation_id)
        assert plivo.app_id == "old-app"
        assert len(plivo.restored) == 1
        assert livekit.deleted == [
            ("trunk", "outbound-1"),
            ("dispatch", "dispatch-1"),
            ("trunk", "inbound-1"),
        ]
        assert plivo.deleted == [
            "plivo_outbound_trunk",
            "plivo_credential",
            "plivo_inbound_trunk",
            "plivo_uri",
        ]
    finally:
        ledger.close()


async def test_definitive_failure_rolls_back_but_ambiguous_failure_stops(
    tmp_path: Path,
) -> None:
    livekit = FakeLiveKit()
    plivo = FakePlivo()
    ledger = TelephonyLedger(tmp_path / "plivo-failure.sqlite3")
    provisioner = PlivoLiveKitSipProvisioner(
        livekit=cast("LiveKitSipAPI", livekit),
        plivo=cast("PlivoSipBackend", plivo),
        ledger=ledger,
    )
    try:
        plivo.fail_at = "binding"
        with pytest.raises(VoiceyError) as definitive:
            await provisioner.provision(_config())
        assert definitive.value.code == "VY-TEL-004"
        assert ledger.open_provisioning(provider="plivo-livekit") == ()
        assert plivo.uri is plivo.credential is plivo.inbound is plivo.outbound is None

        plivo.fail_error = VoiceyError("VY-TEL-011", detail="ambiguous")
        with pytest.raises(VoiceyError) as ambiguous:
            await provisioner.provision(_config())
        assert ambiguous.value.code == "VY-TEL-006"
        assert ledger.open_provisioning(provider="plivo-livekit")[0].state == "ambiguous"
    finally:
        ledger.close()


class FakePlivoHTTP:
    def __init__(self) -> None:
        self.number: dict[str, object] = {"number": NUMBER[1:], "app_id": "old-app"}
        self.uris: list[dict[str, object]] = []
        self.credentials: list[dict[str, object]] = []
        self.trunks: list[dict[str, object]] = []
        self.requests: list[tuple[str, str, dict[str, object] | None]] = []
        self.apply_number = True

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        body: dict[str, object] | None = None
        if request.content:
            body = cast("dict[str, object]", json.loads(request.content))
        self.requests.append((request.method, path, body))
        prefix = f"/v1/Account/{AUTH_ID}"
        if request.method == "GET" and path == f"{prefix}/Number/{NUMBER[1:]}/":
            return httpx.Response(200, json=self.number)
        if request.method == "POST" and path == f"{prefix}/Number/{NUMBER[1:]}/":
            assert body is not None
            if self.apply_number:
                self.number["app_id"] = body["app_id"]
            return httpx.Response(202, json={"message": "changed"})
        for resource, collection, id_field in (
            ("URI", self.uris, "uri_uuid"),
            ("Credential", self.credentials, "credential_uuid"),
            ("Trunk", self.trunks, "trunk_id"),
        ):
            collection_path = f"{prefix}/Zentrunk/{resource}/"
            if request.method == "GET" and path == collection_path:
                return httpx.Response(200, json={"objects": collection})
            if request.method == "POST" and path == collection_path:
                assert body is not None
                identifier = f"{resource.casefold()}-{len(collection) + 1}"
                item = {**body, id_field: identifier}
                if resource == "Trunk":
                    item["trunk_domain"] = f"{body['trunk_direction']}.sip.plivo.com"
                collection.append(item)
                return httpx.Response(201, json=item)
            resource_prefix = collection_path
            if path.startswith(resource_prefix) and path != collection_path:
                identifier = path.removeprefix(resource_prefix).rstrip("/")
                matching = [item for item in collection if item[id_field] == identifier]
                if request.method == "GET" and matching:
                    return httpx.Response(200, json=matching[0])
                if request.method == "DELETE":
                    collection[:] = [item for item in collection if item[id_field] != identifier]
                    return httpx.Response(204)
        return httpx.Response(404, json={"error": "not_found"})


def _http_backend(fake: FakePlivoHTTP) -> tuple[PlivoSipHTTPBackend, httpx.Client]:
    client = httpx.Client(
        base_url="https://api.plivo.com",
        transport=httpx.MockTransport(fake.handler),
    )
    return (
        PlivoSipHTTPBackend(
            auth_id=AUTH_ID,
            auth_token=AUTH_TOKEN,
            client=client,
        ),
        client,
    )


def test_http_backend_create_adopt_bind_restore_and_delete() -> None:
    fake = FakePlivoHTTP()
    backend, client = _http_backend(fake)
    try:
        snapshot = backend.snapshot_number(NUMBER)
        uri = backend.ensure_uri(name="voicey-uri", uri="project.example;transport=tcp")
        credential = backend.ensure_credential(
            name="voicey-credential",
            username="voiceyuser",
            password="secret!",  # pragma: allowlist secret
        )
        inbound = backend.ensure_trunk(
            name="voicey-in",
            direction="inbound",
            primary_uri_uuid=uri.resource_id,
            secure=False,
        )
        outbound = backend.ensure_trunk(
            name="voicey-out",
            direction="outbound",
            credential_uuid=credential.resource_id,
            secure=True,
        )
        assert inbound.sip_domain == "inbound.sip.plivo.com"
        assert outbound.sip_domain == "outbound.sip.plivo.com"
        binding = backend.attach_number(trunk_id=inbound.resource.resource_id, number=NUMBER)
        assert binding.created
        assert not backend.ensure_uri(
            name="voicey-uri", uri="project.example;transport=tcp"
        ).created
        assert not backend.ensure_credential(
            name="voicey-credential",
            username="voiceyuser",
            password="secret!",  # pragma: allowlist secret
        ).created
        backend.restore_number(snapshot)
        assert fake.number["app_id"] == "old-app"
        for resource in (
            outbound.resource,
            credential,
            inbound.resource,
            uri,
        ):
            backend.delete_resource(resource)
        assert fake.trunks == fake.credentials == fake.uris == []
    finally:
        client.close()


def test_http_backend_drift_nonconfirmation_and_ambiguous_response_fail_closed() -> None:
    fake = FakePlivoHTTP()
    backend, client = _http_backend(fake)
    try:
        backend.ensure_uri(name="voicey-uri", uri="project.example;transport=tcp")
        fake.uris[0]["uri"] = "human.example;transport=tcp"
        with pytest.raises(VoiceyError, match="differs"):
            backend.ensure_uri(name="voicey-uri", uri="project.example;transport=tcp")

        fake.apply_number = False
        with pytest.raises(VoiceyError) as unconfirmed:
            backend.attach_number(trunk_id="trunk-1", number=NUMBER)
        assert unconfirmed.value.code == "VY-TEL-011"
    finally:
        client.close()


@pytest.mark.parametrize(
    "values",
    [
        {"number": "bad"},
        {"agent_name": "Bad Name"},
        {"livekit_sip_uri": "http://project.example"},
        {"livekit_sip_uri": "sip:project.example:5070"},
        {"auth_username": "bad!"},
        {"auth_password": "nosecret"},  # pragma: allowlist secret
        {"auth_password": "x!" * 11},  # pragma: allowlist secret
        {"room_prefix": ""},
    ],
)
def test_config_rejects_unsafe_values(values: dict[str, object]) -> None:
    with pytest.raises(VoiceyError, match="VY-TEL-002"):
        _config(**values)


def test_http_backend_rejects_bad_auth_and_unknown_resource() -> None:
    with pytest.raises(VoiceyError, match="VY-TEL-002"):
        PlivoSipHTTPBackend(auth_id="bad", auth_token=AUTH_TOKEN)
    with pytest.raises(VoiceyError, match="VY-TEL-002"):
        PlivoSipHTTPBackend(auth_id=AUTH_ID, auth_token="")
    with pytest.raises(VoiceyError, match="normalized HTTPS"):
        PlivoSipHTTPBackend(
            auth_id=AUTH_ID,
            auth_token=AUTH_TOKEN,
            base_url="http://api.plivo.com",
        )
    backend = PlivoSipHTTPBackend(
        auth_id=AUTH_ID,
        auth_token=AUTH_TOKEN,
        client=cast("httpx.Client", object()),
    )
    with pytest.raises(VoiceyError, match="unknown"):
        backend.delete_resource(ManagedSipResource("other", "id", True))


async def test_livekit_resource_drift_duplicates_and_unexpected_failure_are_fenced(
    tmp_path: Path,
) -> None:
    livekit = FakeLiveKit()
    plivo = FakePlivo()
    ledger = TelephonyLedger(tmp_path / "plivo-livekit-edges.sqlite3")
    provisioner = PlivoLiveKitSipProvisioner(
        livekit=cast("LiveKitSipAPI", livekit),
        plivo=cast("PlivoSipBackend", plivo),
        ledger=ledger,
    )
    config = _config()
    try:
        await provisioner.provision(config)

        duplicate_inbound = lk_sip.SIPInboundTrunkInfo()
        duplicate_inbound.CopyFrom(livekit.inbound[0])
        duplicate_inbound.sip_trunk_id = "inbound-duplicate"
        livekit.inbound.append(duplicate_inbound)
        with pytest.raises(VoiceyError, match="duplicate managed LiveKit SIP"):
            await provisioner._ensure_livekit_inbound(config)
        livekit.inbound.pop()
        livekit.inbound[0].numbers[:] = ["+14155550199"]
        with pytest.raises(VoiceyError, match="inbound trunk differs"):
            await provisioner._ensure_livekit_inbound(config)
        livekit.inbound[0].numbers[:] = [NUMBER]

        duplicate_outbound = lk_sip.SIPOutboundTrunkInfo()
        duplicate_outbound.CopyFrom(livekit.outbound[0])
        duplicate_outbound.sip_trunk_id = "outbound-duplicate"
        livekit.outbound.append(duplicate_outbound)
        with pytest.raises(VoiceyError, match="duplicate managed LiveKit outbound"):
            await provisioner._ensure_livekit_outbound(config, "outbound.sip.plivo.com")
        livekit.outbound.pop()
        livekit.outbound[0].address = "human-change.sip.plivo.com"
        with pytest.raises(VoiceyError, match="outbound trunk differs"):
            await provisioner._ensure_livekit_outbound(config, "outbound.sip.plivo.com")
        livekit.outbound[0].address = "outbound.sip.plivo.com"

        duplicate_dispatch = lk_sip.SIPDispatchRuleInfo()
        duplicate_dispatch.CopyFrom(livekit.dispatch[0])
        duplicate_dispatch.sip_dispatch_rule_id = "dispatch-duplicate"
        livekit.dispatch.append(duplicate_dispatch)
        with pytest.raises(VoiceyError, match="duplicate managed LiveKit dispatch"):
            await provisioner._ensure_dispatch(config, "inbound-1")
        livekit.dispatch.pop()
        livekit.dispatch[0].rule.dispatch_rule_individual.room_prefix = "human-"
        with pytest.raises(VoiceyError, match="dispatch differs"):
            await provisioner._ensure_dispatch(config, "inbound-1")

        plivo.fail_at = "credential"
        plivo.fail_error = RuntimeError("unexpected")
        with pytest.raises(VoiceyError, match="ambiguous"):
            await provisioner.provision(config)
        assert ledger.open_provisioning(provider="plivo-livekit")[-1].state == "ambiguous"
    finally:
        ledger.close()


async def test_rollback_rejects_foreign_token_and_catalogs_cleanup_conflict(
    tmp_path: Path,
) -> None:
    class CleanupFails(FakePlivo):
        def delete_resource(self, resource: ManagedSipResource) -> None:
            raise RuntimeError(resource.kind)

    livekit = FakeLiveKit()
    plivo = CleanupFails()
    ledger = TelephonyLedger(tmp_path / "plivo-rollback-edges.sqlite3")
    provisioner = PlivoLiveKitSipProvisioner(
        livekit=cast("LiveKitSipAPI", livekit),
        plivo=cast("PlivoSipBackend", plivo),
        ledger=ledger,
    )
    try:
        foreign = ledger.prepare_provisioning(
            provider="other",
            number=NUMBER,
            snapshot={},
            planned={},
        )
        with pytest.raises(VoiceyError, match="another provider"):
            await provisioner.rollback(foreign.operation_id)

        applied = await provisioner.provision(_config())
        with pytest.raises(VoiceyError, match="rollback conflicted"):
            await provisioner.rollback(applied.operation_id)
        assert ledger.get_provisioning(applied.operation_id).state == "conflict"
    finally:
        ledger.close()


def test_config_secret_fingerprint_and_property_revalidation_are_safe() -> None:
    config = _config()
    assert config.credential_name.endswith("-e32111f11142")
    assert "secret!" not in config.config_fingerprint
    object.__setattr__(config, "livekit_sip_uri", "invalid")
    with pytest.raises(VoiceyError, match="LiveKit SIP URI is invalid"):
        _ = config.livekit_sip_host


def test_http_backend_detects_uri_credential_and_trunk_drift_or_duplicates() -> None:
    fake = FakePlivoHTTP()
    backend, client = _http_backend(fake)
    try:
        uri = backend.ensure_uri(name="voicey-uri", uri="project.example;transport=tcp")
        fake.uris.append(dict(fake.uris[0]))
        with pytest.raises(VoiceyError, match="duplicate managed Plivo SIP URIs"):
            backend.ensure_uri(name="voicey-uri", uri="project.example;transport=tcp")
        fake.uris.pop()
        fake.uris[0]["uri"] = "human.example;transport=tcp"
        with pytest.raises(VoiceyError, match="URI differs"):
            backend.ensure_uri(name="voicey-uri", uri="project.example;transport=tcp")
        fake.uris[0]["uri"] = "project.example;transport=tcp"

        credential = backend.ensure_credential(
            name="voicey-credential-hash",
            username="voiceyuser",
            password="secret!",  # pragma: allowlist secret
        )
        fake.credentials.append(dict(fake.credentials[0]))
        with pytest.raises(VoiceyError, match="duplicate managed Plivo credentials"):
            backend.ensure_credential(
                name="voicey-credential-hash",
                username="voiceyuser",
                password="secret!",  # pragma: allowlist secret
            )
        fake.credentials.pop()
        fake.credentials[0]["username"] = "humanuser"
        with pytest.raises(VoiceyError, match="credential differs"):
            backend.ensure_credential(
                name="voicey-credential-hash",
                username="voiceyuser",
                password="secret!",  # pragma: allowlist secret
            )
        fake.credentials[0]["username"] = "voiceyuser"

        inbound = backend.ensure_trunk(
            name="voicey-in",
            direction="inbound",
            primary_uri_uuid=uri.resource_id,
            secure=False,
        )
        adopted = backend.ensure_trunk(
            name="voicey-in",
            direction="inbound",
            primary_uri_uuid=uri.resource_id,
            secure=False,
        )
        assert adopted.resource.resource_id == inbound.resource.resource_id
        assert not adopted.resource.created
        fake.trunks.append(dict(fake.trunks[0]))
        with pytest.raises(VoiceyError, match="duplicate managed Plivo trunks"):
            backend.ensure_trunk(
                name="voicey-in",
                direction="inbound",
                primary_uri_uuid=uri.resource_id,
                secure=False,
            )
        fake.trunks.pop()
        fake.trunks[0]["secure"] = True
        with pytest.raises(VoiceyError, match="inbound trunk differs"):
            backend.ensure_trunk(
                name="voicey-in",
                direction="inbound",
                primary_uri_uuid=uri.resource_id,
                secure=False,
            )
        with pytest.raises(VoiceyError, match="direction"):
            backend.ensure_trunk(name="bad", direction="sideways", secure=False)

        assert credential.resource_id == "credential-1"
    finally:
        client.close()


def test_http_backend_binding_restore_and_malformed_responses_fail_closed() -> None:
    fake = FakePlivoHTTP()
    backend, client = _http_backend(fake)
    try:
        fake.number["app_id"] = "trunk-1"
        assert not backend.attach_number(trunk_id="trunk-1", number=NUMBER).created
        fake.apply_number = False
        with pytest.raises(VoiceyError, match="rollback did not compare equal"):
            backend.restore_number({"number": NUMBER, "app_id": "old-app"})
    finally:
        client.close()

    cases = (
        (httpx.Response(200, json={"objects": "bad"}), "list is malformed"),
        (httpx.Response(200, json={"objects": ["bad"]}), "item is malformed"),
        (httpx.Response(200, content=b"{"), "invalid JSON"),
        (httpx.Response(200, json=[]), "invalid envelope"),
        (httpx.Response(400, json={"error": "bad"}), "http_400"),
        (httpx.Response(503, json={"error": "down"}), "http_503"),
    )
    for response, message in cases:
        malformed_client = httpx.Client(
            base_url="https://api.plivo.com",
            transport=httpx.MockTransport(lambda _request, response=response: response),
        )
        malformed = PlivoSipHTTPBackend(
            auth_id=AUTH_ID,
            auth_token=AUTH_TOKEN,
            client=malformed_client,
        )
        try:
            with pytest.raises(VoiceyError, match=message):
                malformed.ensure_uri(name="voicey-uri", uri="project.example;transport=tcp")
        finally:
            malformed_client.close()


def test_http_backend_network_and_invalid_trunk_domain_are_uncertain() -> None:
    def network(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline", request=request)

    network_client = httpx.Client(
        base_url="https://api.plivo.com",
        transport=httpx.MockTransport(network),
    )
    backend = PlivoSipHTTPBackend(
        auth_id=AUTH_ID,
        auth_token=AUTH_TOKEN,
        client=network_client,
    )
    try:
        with pytest.raises(VoiceyError, match="definitive result"):
            backend.snapshot_number(NUMBER)
    finally:
        network_client.close()

    fake = FakePlivoHTTP()
    backend, client = _http_backend(fake)
    try:
        fake.trunks.append(
            {
                "name": "voicey-in",
                "trunk_id": "trunk-1",
                "trunk_direction": "inbound",
                "primary_uri_uuid": "uri-1",
                "secure": False,
                "trunk_domain": "not a domain",
            }
        )
        with pytest.raises(VoiceyError, match="invalid SIP domain"):
            backend.ensure_trunk(
                name="voicey-in",
                direction="inbound",
                primary_uri_uuid="uri-1",
                secure=False,
            )
    finally:
        client.close()
