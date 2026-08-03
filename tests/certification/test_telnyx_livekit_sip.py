"""Offline certification for Telnyx FQDN SIP provisioning into LiveKit."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from livekit.protocol import sip as lk_sip

from voicey.errors import VoiceyError
from voicey.runtimes.livekit.sip import (
    LiveKitSipAPI,
    ManagedSipResource,
)
from voicey.runtimes.livekit.telnyx import (
    TelnyxLiveKitSipConfig,
    TelnyxLiveKitSipProvisioner,
    TelnyxSipBackend,
    TelnyxSipHTTPBackend,
)
from voicey.telephony.ledger import TelephonyLedger

NUMBER = "+14155550100"
BASE_NAME = "voicey-booking-14155550100"


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
        match = next(
            item
            for item in self.dispatch
            if item.sip_dispatch_rule_id == request.sip_dispatch_rule_id
        )
        self.dispatch.remove(match)
        return match

    async def delete_trunk(
        self,
        request: lk_sip.DeleteSIPTrunkRequest,
    ) -> lk_sip.SIPTrunkInfo:
        self.deleted.append(("trunk", request.sip_trunk_id))
        for item in tuple(self.inbound):
            if item.sip_trunk_id == request.sip_trunk_id:
                self.inbound.remove(item)
                return lk_sip.SIPTrunkInfo(sip_trunk_id=request.sip_trunk_id)
        for item in tuple(self.outbound):
            if item.sip_trunk_id == request.sip_trunk_id:
                self.outbound.remove(item)
                return lk_sip.SIPTrunkInfo(sip_trunk_id=request.sip_trunk_id)
        return lk_sip.SIPTrunkInfo(sip_trunk_id=request.sip_trunk_id)


class FakeTelnyxBackend:
    def __init__(self) -> None:
        self.number_connection = "old-connection"
        self.profile: ManagedSipResource | None = None
        self.connection: ManagedSipResource | None = None
        self.fqdn: ManagedSipResource | None = None
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.deleted: list[str] = []
        self.restored: list[dict[str, object]] = []
        self.fail_at: str | None = None
        self.fail_error = VoiceyError("VY-TEL-004", detail="definitive test failure.")

    def snapshot_number(self, number: str) -> dict[str, object]:
        assert number == NUMBER
        return {
            "number": number,
            "number_id": "number-1",
            "connection_id": self.number_connection,
        }

    def ensure_outbound_profile(self, *, name: str) -> ManagedSipResource:
        self._fail("profile")
        self.calls.append(("profile", {"name": name}))
        if self.profile is None:
            self.profile = ManagedSipResource("telnyx_outbound_profile", "profile-1", True)
            return self.profile
        return ManagedSipResource("telnyx_outbound_profile", "profile-1", False)

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
        self._fail("connection")
        self.calls.append(
            (
                "connection",
                {
                    "name": name,
                    "username": username,
                    "password": password,
                    "outbound_profile_id": outbound_profile_id,
                    "config_fingerprint": config_fingerprint,
                    "anchorsite": anchorsite,
                },
            )
        )
        if self.connection is None:
            self.connection = ManagedSipResource(
                "telnyx_fqdn_connection",
                "connection-1",
                True,
            )
            return self.connection
        return ManagedSipResource("telnyx_fqdn_connection", "connection-1", False)

    def ensure_fqdn(
        self,
        *,
        connection_id: str,
        fqdn: str,
        port: int,
    ) -> ManagedSipResource:
        self._fail("fqdn")
        self.calls.append(
            (
                "fqdn",
                {"connection_id": connection_id, "fqdn": fqdn, "port": port},
            )
        )
        if self.fqdn is None:
            self.fqdn = ManagedSipResource(
                "telnyx_fqdn",
                "fqdn-1",
                True,
                connection_id,
            )
            return self.fqdn
        return ManagedSipResource("telnyx_fqdn", "fqdn-1", False, connection_id)

    def attach_number(self, *, connection_id: str, number: str) -> ManagedSipResource:
        self._fail("binding")
        self.calls.append(("binding", {"connection_id": connection_id, "number": number}))
        if self.number_connection == connection_id:
            return ManagedSipResource(
                "telnyx_number_binding",
                "number-1",
                False,
                connection_id,
            )
        self.number_connection = connection_id
        return ManagedSipResource(
            "telnyx_number_binding",
            "number-1",
            True,
            connection_id,
        )

    def restore_number(self, snapshot: dict[str, object]) -> None:
        self.restored.append(snapshot)
        self.number_connection = cast("str", snapshot["connection_id"])

    def delete_resource(self, resource: ManagedSipResource) -> None:
        self.deleted.append(resource.kind)
        if resource.kind == "telnyx_fqdn":
            self.fqdn = None
        elif resource.kind == "telnyx_fqdn_connection":
            self.connection = None
        elif resource.kind == "telnyx_outbound_profile":
            self.profile = None

    def _fail(self, point: str) -> None:
        if self.fail_at == point:
            raise self.fail_error


def _config(**values: object) -> TelnyxLiveKitSipConfig:
    defaults: dict[str, object] = {
        "number": NUMBER,
        "agent_name": "booking",
        "livekit_sip_uri": "sip:project.sip.livekit.cloud",
        "auth_username": "voicey-user",
        "auth_password": "credential-secret",  # pragma: allowlist secret
    }
    return TelnyxLiveKitSipConfig(**cast("Any", {**defaults, **values}))


async def test_provision_both_sides_with_current_official_contract(tmp_path: Path) -> None:
    livekit = FakeLiveKit()
    telnyx = FakeTelnyxBackend()
    ledger = TelephonyLedger(tmp_path / "sip.sqlite3")
    provisioner = TelnyxLiveKitSipProvisioner(
        livekit=cast("LiveKitSipAPI", livekit),
        telnyx=cast("TelnyxSipBackend", telnyx),
        ledger=ledger,
    )
    try:
        result = await provisioner.provision(_config())
        operation = ledger.get_provisioning(result.operation_id)

        assert operation.state == "applied"
        assert result.created_resources == 7
        assert result.telnyx_connection_id == "connection-1"
        assert result.telnyx_fqdn_id == "fqdn-1"
        assert [name for name, _ in telnyx.calls] == [
            "profile",
            "connection",
            "fqdn",
            "binding",
        ]
        fqdn = telnyx.calls[2][1]
        assert fqdn == {
            "connection_id": "connection-1",
            "fqdn": "project.sip.livekit.cloud",
            "port": 5060,
        }

        inbound = livekit.inbound[0]
        outbound = livekit.outbound[0]
        dispatch = livekit.dispatch[0]
        assert inbound.numbers == [NUMBER]
        assert inbound.media_encryption == lk_sip.SIP_MEDIA_ENCRYPT_DISABLE
        assert outbound.address == "sip.telnyx.com"
        assert outbound.transport == lk_sip.SIP_TRANSPORT_TCP
        assert outbound.auth_username == "voicey-user"
        assert dict(outbound.headers_to_attributes) == {"X-Telnyx-Username": "voicey-user"}
        assert outbound.media_encryption == lk_sip.SIP_MEDIA_ENCRYPT_DISABLE
        assert dispatch.rule.dispatch_rule_individual.room_prefix == "call-"
        assert dispatch.room_config.agents[0].agent_name == "booking"
        dispatch_metadata = json.loads(dispatch.room_config.agents[0].metadata)
        assert dispatch_metadata["provider"] == "telnyx"
    finally:
        ledger.close()


async def test_provision_is_idempotent_without_duplicate_resources(tmp_path: Path) -> None:
    livekit = FakeLiveKit()
    telnyx = FakeTelnyxBackend()
    ledger = TelephonyLedger(tmp_path / "idempotent.sqlite3")
    provisioner = TelnyxLiveKitSipProvisioner(
        livekit=cast("LiveKitSipAPI", livekit),
        telnyx=cast("TelnyxSipBackend", telnyx),
        ledger=ledger,
    )
    try:
        first = await provisioner.provision(_config())
        second = await provisioner.provision(_config())

        assert first.created_resources == 7
        assert second.created_resources == 0
        assert len(livekit.inbound) == 1
        assert len(livekit.outbound) == 1
        assert len(livekit.dispatch) == 1
        assert len(telnyx.calls) == 8
    finally:
        ledger.close()


async def test_rollback_is_reverse_order_idempotent_and_restores_number(
    tmp_path: Path,
) -> None:
    livekit = FakeLiveKit()
    telnyx = FakeTelnyxBackend()
    ledger = TelephonyLedger(tmp_path / "rollback.sqlite3")
    provisioner = TelnyxLiveKitSipProvisioner(
        livekit=cast("LiveKitSipAPI", livekit),
        telnyx=cast("TelnyxSipBackend", telnyx),
        ledger=ledger,
    )
    try:
        result = await provisioner.provision(_config())
        rolled_back = await provisioner.rollback(result.operation_id)
        again = await provisioner.rollback(result.operation_id)

        assert rolled_back.state == "rolled_back"
        assert again.state == "rolled_back"
        assert telnyx.number_connection == "old-connection"
        assert len(telnyx.restored) == 1
        assert telnyx.deleted == [
            "telnyx_fqdn",
            "telnyx_fqdn_connection",
            "telnyx_outbound_profile",
        ]
        assert livekit.deleted == [
            ("trunk", "outbound-1"),
            ("dispatch", "dispatch-1"),
            ("trunk", "inbound-1"),
        ]
    finally:
        ledger.close()


async def test_definitive_failure_rolls_back_but_ambiguous_failure_does_not(
    tmp_path: Path,
) -> None:
    livekit = FakeLiveKit()
    telnyx = FakeTelnyxBackend()
    ledger = TelephonyLedger(tmp_path / "failure.sqlite3")
    provisioner = TelnyxLiveKitSipProvisioner(
        livekit=cast("LiveKitSipAPI", livekit),
        telnyx=cast("TelnyxSipBackend", telnyx),
        ledger=ledger,
    )
    telnyx.fail_at = "fqdn"
    try:
        with pytest.raises(VoiceyError, match="VY-TEL-004"):
            await provisioner.provision(_config())
        first = tuple(ledger.open_provisioning(provider="telnyx-livekit"))
        assert first == ()
        assert telnyx.deleted == [
            "telnyx_fqdn_connection",
            "telnyx_outbound_profile",
        ]
        assert livekit.inbound == []
        assert livekit.outbound == []

        telnyx.fail_error = VoiceyError("VY-TEL-011", detail="unknown outcome.")
        telnyx.deleted.clear()
        with pytest.raises(VoiceyError, match="VY-TEL-006"):
            await provisioner.provision(_config())
        open_operations = ledger.open_provisioning(provider="telnyx-livekit")
        assert len(open_operations) == 1
        assert open_operations[0].state == "ambiguous"
        assert telnyx.deleted == []
    finally:
        ledger.close()


@pytest.mark.parametrize(
    "values",
    [
        {"number": "invalid"},
        {"agent_name": "Invalid"},
        {"livekit_sip_uri": "https://project.sip.livekit.cloud"},
        {"livekit_sip_uri": "sip:user:password@host"},
        {"livekit_sip_uri": "sip:host:70000"},
        {"livekit_sip_uri": "sip:host:5061"},
        {"auth_username": ""},
        {"auth_password": ""},
        {"room_prefix": ""},
        {"anchorsite": "Moon"},
    ],
)
def test_config_rejects_unsafe_or_unsupported_values(values: dict[str, object]) -> None:
    with pytest.raises(VoiceyError, match="VY-TEL-002"):
        _config(**values)


class FakeTelnyxHTTP:
    def __init__(self) -> None:
        self.profile: dict[str, object] | None = None
        self.connection: dict[str, object] | None = None
        self.fqdn: dict[str, object] | None = None
        self.number: dict[str, object] = {
            "id": "number-1",
            "phone_number": NUMBER,
            "connection_id": "old-connection",
        }
        self.requests: list[tuple[str, str, dict[str, object] | None]] = []
        self.failures: dict[tuple[str, str], int] = {}

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/v2")
        body: dict[str, object] | None = None
        if request.content:
            loaded: object = json.loads(request.content)
            body = cast("dict[str, object]", loaded)
        self.requests.append((request.method, path, body))
        failure = self.failures.get((request.method, path))
        if failure is not None:
            return httpx.Response(failure, json={"errors": [{"detail": "private"}]})
        collections = {
            "/outbound_voice_profiles": self.profile,
            "/fqdn_connections": self.connection,
            "/fqdns": self.fqdn,
        }
        if request.method == "GET" and path in collections:
            item = collections[path]
            return httpx.Response(200, json={"data": [] if item is None else [item]})
        if request.method == "GET" and path == "/phone_numbers":
            return httpx.Response(200, json={"data": [self.number]})
        if request.method == "POST" and path == "/outbound_voice_profiles":
            self.profile = {"id": "profile-1", **(body or {})}
            return httpx.Response(201, json={"data": self.profile})
        if request.method == "POST" and path == "/fqdn_connections":
            sanitized = dict(body or {})
            sanitized.pop("password", None)
            self.connection = {"id": "connection-1", **sanitized}
            return httpx.Response(201, json={"data": self.connection})
        if request.method == "POST" and path == "/fqdns":
            self.fqdn = {"id": "fqdn-1", **(body or {})}
            return httpx.Response(201, json={"data": self.fqdn})
        if request.method == "PATCH" and path == "/phone_numbers/number-1":
            self.number.update(body or {})
            return httpx.Response(200, json={"data": self.number})
        if request.method == "DELETE":
            if path == "/fqdns/fqdn-1":
                self.fqdn = None
            elif path == "/fqdn_connections/connection-1":
                self.connection = None
            elif path == "/outbound_voice_profiles/profile-1":
                self.profile = None
            return httpx.Response(204)
        return httpx.Response(404, json={"errors": [{"detail": "missing"}]})


@pytest.fixture
def http_backend() -> Iterator[tuple[TelnyxSipHTTPBackend, FakeTelnyxHTTP, httpx.Client]]:
    fake = FakeTelnyxHTTP()
    client = httpx.Client(
        base_url="https://api.telnyx.com/v2",
        transport=httpx.MockTransport(fake.handler),
    )
    backend = TelnyxSipHTTPBackend(
        api_key="KEY-not-real",  # pragma: allowlist secret
        client=client,
    )
    try:
        yield backend, fake, client
    finally:
        client.close()


def test_http_backend_uses_exact_official_fqdn_requests_and_idempotency(
    http_backend: tuple[TelnyxSipHTTPBackend, FakeTelnyxHTTP, httpx.Client],
) -> None:
    backend, fake, _ = http_backend
    config = _config()
    snapshot = backend.snapshot_number(NUMBER)
    profile = backend.ensure_outbound_profile(name=f"{BASE_NAME}-outbound")
    connection = backend.ensure_connection(
        name=BASE_NAME,
        username=config.auth_username,
        password=config.auth_password,
        outbound_profile_id=profile.resource_id,
        config_fingerprint=config.config_fingerprint,
        anchorsite=config.anchorsite,
    )
    fqdn = backend.ensure_fqdn(
        connection_id=connection.resource_id,
        fqdn=config.livekit_sip_host,
        port=5060,
    )
    binding = backend.attach_number(connection_id=connection.resource_id, number=NUMBER)

    assert snapshot["connection_id"] == "old-connection"
    assert all(resource.created for resource in (profile, connection, fqdn, binding))
    connection_request = next(
        body
        for method, path, body in fake.requests
        if method == "POST" and path == "/fqdn_connections"
    )
    assert connection_request is not None
    assert connection_request["transport_protocol"] == "TCP"
    assert "encrypted_media" not in connection_request
    assert connection_request["inbound"] == {
        "ani_number_format": "+E.164",
        "dnis_number_format": "+e164",
    }
    assert connection_request["outbound"] == {"outbound_voice_profile_id": "profile-1"}
    fqdn_request = next(
        body for method, path, body in fake.requests if method == "POST" and path == "/fqdns"
    )
    assert fqdn_request == {
        "connection_id": "connection-1",
        "fqdn": "project.sip.livekit.cloud",
        "port": 5060,
        "dns_record_type": "a",
    }

    assert not backend.ensure_outbound_profile(name=f"{BASE_NAME}-outbound").created
    assert not backend.ensure_connection(
        name=BASE_NAME,
        username=config.auth_username,
        password=config.auth_password,
        outbound_profile_id=profile.resource_id,
        config_fingerprint=config.config_fingerprint,
        anchorsite=config.anchorsite,
    ).created
    assert not backend.ensure_fqdn(
        connection_id=connection.resource_id,
        fqdn=config.livekit_sip_host,
        port=5060,
    ).created
    assert not backend.attach_number(connection_id=connection.resource_id, number=NUMBER).created


def test_http_backend_restore_delete_conflicts_and_safe_errors(
    http_backend: tuple[TelnyxSipHTTPBackend, FakeTelnyxHTTP, httpx.Client],
) -> None:
    backend, fake, _ = http_backend
    config = _config()
    snapshot = backend.snapshot_number(NUMBER)
    profile = backend.ensure_outbound_profile(name=f"{BASE_NAME}-outbound")
    connection = backend.ensure_connection(
        name=BASE_NAME,
        username=config.auth_username,
        password=config.auth_password,
        outbound_profile_id=profile.resource_id,
        config_fingerprint=config.config_fingerprint,
        anchorsite=config.anchorsite,
    )
    fqdn = backend.ensure_fqdn(
        connection_id=connection.resource_id,
        fqdn=config.livekit_sip_host,
        port=5060,
    )
    backend.attach_number(connection_id=connection.resource_id, number=NUMBER)
    backend.restore_number(snapshot)
    assert fake.number["connection_id"] == "old-connection"
    for resource in (fqdn, connection, profile):
        backend.delete_resource(resource)
    assert fake.fqdn is None
    assert fake.connection is None
    assert fake.profile is None

    fake.failures[("GET", "/phone_numbers")] = 401
    with pytest.raises(VoiceyError) as rejected:
        backend.snapshot_number(NUMBER)
    assert rejected.value.code == "VY-TEL-004"
    assert "private" not in str(rejected.value)
    fake.failures[("GET", "/phone_numbers")] = 503
    with pytest.raises(VoiceyError, match="VY-TEL-011"):
        backend.snapshot_number(NUMBER)
    with pytest.raises(VoiceyError, match="VY-TEL-006"):
        backend.delete_resource(ManagedSipResource("unknown", "id", True))


def test_http_backend_detects_managed_drift(
    http_backend: tuple[TelnyxSipHTTPBackend, FakeTelnyxHTTP, httpx.Client],
) -> None:
    backend, fake, _ = http_backend
    fake.profile = {
        "id": "profile-1",
        "name": f"{BASE_NAME}-outbound",
        "traffic_type": "fax",
        "service_plan": "global",
    }
    with pytest.raises(VoiceyError, match="VY-TEL-006"):
        backend.ensure_outbound_profile(name=f"{BASE_NAME}-outbound")

    fake.profile["traffic_type"] = "conversational"
    config = _config()
    fake.connection = {
        "id": "connection-1",
        "connection_name": BASE_NAME,
        "active": True,
        "anchorsite_override": "Latency",
        "user_name": "someone-else",
        "transport_protocol": "TCP",
    }
    with pytest.raises(VoiceyError, match="VY-TEL-006"):
        backend.ensure_connection(
            name=BASE_NAME,
            username=config.auth_username,
            password=config.auth_password,
            outbound_profile_id="profile-1",
            config_fingerprint=config.config_fingerprint,
            anchorsite=config.anchorsite,
        )


def test_http_backend_requires_credentials() -> None:
    with pytest.raises(VoiceyError, match="VY-TEL-002"):
        TelnyxSipHTTPBackend(api_key="")  # pragma: allowlist secret
