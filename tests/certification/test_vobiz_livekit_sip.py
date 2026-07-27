"""Offline certification for Vobiz UDP SIP provisioning into LiveKit."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from livekit.protocol import sip as lk_sip

from voicekit.errors import VoicekitError
from voicekit.runtimes.livekit.sip import LiveKitSipAPI, ManagedSipResource
from voicekit.runtimes.livekit.vobiz import (
    VobizLiveKitSipConfig,
    VobizLiveKitSipProvisioner,
    VobizManagedTrunk,
    VobizSipBackend,
    VobizSipHTTPBackend,
)
from voicekit.telephony.ledger import TelephonyLedger

NUMBER = "+918071234567"


class FakeLiveKit:
    def __init__(self) -> None:
        self.inbound: list[lk_sip.SIPInboundTrunkInfo] = []
        self.outbound: list[lk_sip.SIPOutboundTrunkInfo] = []
        self.dispatch: list[lk_sip.SIPDispatchRuleInfo] = []
        self.deleted: list[tuple[str, str]] = []
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

    async def delete_sip_trunk(
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


class FakeVobiz:
    def __init__(self) -> None:
        self.number_trunk = "old-trunk"
        self.inbound: VobizManagedTrunk | None = None
        self.outbound: VobizManagedTrunk | None = None
        self.restored: list[dict[str, object]] = []
        self.deleted: list[str] = []
        self.fail_at: str | None = None
        self.fail_error: Exception = VoicekitError(
            "VK-TEL-004",
            detail="definitive failure.",
        )

    def snapshot_number(self, number: str) -> dict[str, object]:
        assert number == NUMBER
        return {"number": number, "trunk_group_id": self.number_trunk}

    def require_credential(
        self,
        *,
        credential_id: str,
        username: str,
    ) -> ManagedSipResource:
        self._fail("credential")
        assert credential_id == "credential-1"
        assert username == "voicekituser"
        return ManagedSipResource("vobiz_credential", credential_id, False)

    def ensure_trunk(
        self,
        *,
        name: str,
        direction: str,
        max_concurrent_calls: int,
        inbound_destination: str | None = None,
        credential_id: str | None = None,
    ) -> VobizManagedTrunk:
        self._fail(direction)
        assert max_concurrent_calls == 10
        if direction == "inbound":
            assert name.endswith("-in")
            assert inbound_destination == "project.sip.livekit.cloud"
            if self.inbound is None:
                self.inbound = VobizManagedTrunk(
                    ManagedSipResource("vobiz_inbound_trunk", "vobiz-in-1", True),
                    "inbound.sip.vobiz.ai",
                )
                return self.inbound
            return VobizManagedTrunk(
                ManagedSipResource("vobiz_inbound_trunk", "vobiz-in-1", False),
                self.inbound.sip_domain,
            )
        assert name.endswith("-out")
        assert credential_id == "credential-1"
        if self.outbound is None:
            self.outbound = VobizManagedTrunk(
                ManagedSipResource("vobiz_outbound_trunk", "vobiz-out-1", True),
                "outbound.sip.vobiz.ai",
            )
            return self.outbound
        return VobizManagedTrunk(
            ManagedSipResource("vobiz_outbound_trunk", "vobiz-out-1", False),
            self.outbound.sip_domain,
        )

    def attach_number(self, *, trunk_id: str, number: str) -> ManagedSipResource:
        self._fail("binding")
        assert number == NUMBER
        if self.number_trunk == trunk_id:
            return ManagedSipResource("vobiz_number_binding", number, False)
        self.number_trunk = trunk_id
        return ManagedSipResource("vobiz_number_binding", number, True)

    def restore_number(self, snapshot: dict[str, object]) -> None:
        self.restored.append(snapshot)
        self.number_trunk = cast("str", snapshot["trunk_group_id"])

    def delete_resource(self, resource: ManagedSipResource) -> None:
        self.deleted.append(resource.kind)
        if resource.kind == "vobiz_outbound_trunk":
            self.outbound = None
        if resource.kind == "vobiz_inbound_trunk":
            self.inbound = None

    def _fail(self, point: str) -> None:
        if self.fail_at == point:
            raise self.fail_error


def _config(**values: object) -> VobizLiveKitSipConfig:
    defaults: dict[str, object] = {
        "number": NUMBER,
        "agent_name": "booking",
        "livekit_sip_uri": "sip:project.sip.livekit.cloud",
        "credential_id": "credential-1",
        "auth_username": "voicekituser",
        "auth_password": "credential-secret",  # pragma: allowlist secret
    }
    return VobizLiveKitSipConfig(**cast("Any", {**defaults, **values}))


async def test_provisions_official_vobiz_livekit_udp_contract(tmp_path: Path) -> None:
    livekit = FakeLiveKit()
    vobiz = FakeVobiz()
    ledger = TelephonyLedger(tmp_path / "vobiz-livekit.sqlite3")
    provisioner = VobizLiveKitSipProvisioner(
        livekit=cast("LiveKitSipAPI", livekit),
        vobiz=cast("VobizSipBackend", vobiz),
        ledger=ledger,
    )
    try:
        result = await provisioner.provision(_config())
        operation = ledger.get_provisioning(result.operation_id)

        assert operation.state == "applied"
        assert result.created_resources == 6
        assert result.vobiz_inbound_trunk_id == "vobiz-in-1"
        assert result.vobiz_outbound_trunk_id == "vobiz-out-1"
        inbound = livekit.inbound[0]
        outbound = livekit.outbound[0]
        dispatch = livekit.dispatch[0]
        assert inbound.numbers == [NUMBER]
        assert inbound.allowed_addresses == ["13.233.44.61/32"]
        assert inbound.media_encryption == lk_sip.SIP_MEDIA_ENCRYPT_DISABLE
        assert outbound.address == "outbound.sip.vobiz.ai"
        assert outbound.transport == lk_sip.SIP_TRANSPORT_UDP
        assert outbound.auth_username == "voicekituser"
        assert outbound.media_encryption == lk_sip.SIP_MEDIA_ENCRYPT_DISABLE
        assert dispatch.rule.dispatch_rule_individual.room_prefix == "call-"
        assert dispatch.room_config.agents[0].agent_name == "booking"
        metadata = json.loads(dispatch.room_config.agents[0].metadata)
        assert metadata["provider"] == "vobiz"
    finally:
        ledger.close()


async def test_provisioning_is_idempotent_and_rollback_restores_route(
    tmp_path: Path,
) -> None:
    livekit = FakeLiveKit()
    vobiz = FakeVobiz()
    ledger = TelephonyLedger(tmp_path / "vobiz-idempotent.sqlite3")
    provisioner = VobizLiveKitSipProvisioner(
        livekit=cast("LiveKitSipAPI", livekit),
        vobiz=cast("VobizSipBackend", vobiz),
        ledger=ledger,
    )
    try:
        first = await provisioner.provision(_config())
        second = await provisioner.provision(_config())
        assert first.created_resources == 6
        assert second.created_resources == 0
        assert len(livekit.inbound) == len(livekit.outbound) == len(livekit.dispatch) == 1

        rolled_back = await provisioner.rollback(first.operation_id)
        again = await provisioner.rollback(first.operation_id)
        assert rolled_back.state == again.state == "rolled_back"
        assert vobiz.number_trunk == "old-trunk"
        assert len(vobiz.restored) == 1
        assert livekit.deleted == [
            ("trunk", "outbound-1"),
            ("dispatch", "dispatch-1"),
            ("trunk", "inbound-1"),
        ]
        assert vobiz.deleted == [
            "vobiz_outbound_trunk",
            "vobiz_inbound_trunk",
        ]
    finally:
        ledger.close()


async def test_definitive_failure_rolls_back_and_ambiguous_failure_stops(
    tmp_path: Path,
) -> None:
    livekit = FakeLiveKit()
    vobiz = FakeVobiz()
    ledger = TelephonyLedger(tmp_path / "vobiz-failure.sqlite3")
    provisioner = VobizLiveKitSipProvisioner(
        livekit=cast("LiveKitSipAPI", livekit),
        vobiz=cast("VobizSipBackend", vobiz),
        ledger=ledger,
    )
    try:
        vobiz.fail_at = "binding"
        with pytest.raises(VoicekitError) as definitive:
            await provisioner.provision(_config())
        assert definitive.value.code == "VK-TEL-004"
        assert ledger.open_provisioning(provider="vobiz-livekit") == ()
        assert vobiz.inbound is None
        assert vobiz.outbound is None

        vobiz.fail_error = VoicekitError("VK-TEL-011", detail="ambiguous")
        with pytest.raises(VoicekitError) as ambiguous_error:
            await provisioner.provision(_config())
        assert ambiguous_error.value.code == "VK-TEL-006"
        ambiguous = ledger.open_provisioning(provider="vobiz-livekit")
        assert len(ambiguous) == 1
        assert ambiguous[0].state == "ambiguous"
    finally:
        ledger.close()


class FakeVobizHTTP:
    def __init__(self) -> None:
        self.credentials = [
            {
                "id": "credential-1",
                "username": "voicekituser",
                "enabled": True,
            }
        ]
        self.trunks: list[dict[str, object]] = []
        self.number: dict[str, object] = {
            "e164": NUMBER,
            "trunk_group_id": "old-trunk",
        }
        self.requests: list[tuple[str, str, dict[str, object] | None]] = []
        self.apply_assignment = True

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        body: dict[str, object] | None = None
        if request.content:
            loaded: object = json.loads(request.content)
            body = cast("dict[str, object]", loaded)
        self.requests.append((request.method, path, body))
        account = "/api/v1/Account/MA_VOBIZ_TEST"
        if request.method == "GET" and path == f"{account}/credentials":
            return httpx.Response(200, json={"credentials": self.credentials})
        if request.method == "GET" and path == f"{account}/trunks":
            return httpx.Response(200, json={"trunks": self.trunks})
        if request.method == "GET" and path.startswith(f"{account}/trunks/"):
            trunk_id = path.rsplit("/", maxsplit=1)[-1]
            matching = [item for item in self.trunks if item["trunk_id"] == trunk_id]
            if len(matching) == 1:
                return httpx.Response(200, json=matching[0])
            return httpx.Response(404, json={"error": "not found"})
        if request.method == "POST" and path == f"{account}/trunks":
            assert body is not None
            direction = str(body["trunk_direction"])
            trunk_id = f"{direction}-trunk"
            item = {
                **body,
                "trunk_id": trunk_id,
                "trunk_domain": f"{direction}.sip.vobiz.ai",
                "concurrent_calls_limit": body["max_concurrent_calls"],
            }
            self.trunks.append(item)
            return httpx.Response(201, json=item)
        if request.method == "GET" and path == f"{account}/numbers":
            return httpx.Response(200, json={"numbers": [self.number]})
        number_path = f"{account}/numbers/{NUMBER}/assign"
        if request.method == "POST" and path == number_path:
            assert body is not None
            if self.apply_assignment:
                self.number["trunk_group_id"] = body["trunk_group_id"]
            return httpx.Response(200, json=self.number)
        if request.method == "DELETE" and path == number_path:
            if self.apply_assignment:
                self.number["trunk_group_id"] = None
            return httpx.Response(204)
        if request.method == "DELETE" and path.startswith(f"{account}/trunks/"):
            trunk_id = path.rsplit("/", maxsplit=1)[-1]
            self.trunks = [item for item in self.trunks if item["trunk_id"] != trunk_id]
            return httpx.Response(204)
        return httpx.Response(404, json={"error": "not found"})


def test_vobiz_http_backend_uses_current_trunk_and_number_contract() -> None:
    fake = FakeVobizHTTP()
    client = httpx.Client(
        transport=httpx.MockTransport(fake.handler),
        base_url="https://api.vobiz.ai",
    )
    backend = VobizSipHTTPBackend(
        auth_id="MA_VOBIZ_TEST",
        auth_token="not-a-real-token",  # pragma: allowlist secret
        client=client,
    )
    snapshot = backend.snapshot_number(NUMBER)
    credential = backend.require_credential(
        credential_id="credential-1",
        username="voicekituser",
    )
    inbound = backend.ensure_trunk(
        name="voicekit-booking-in",
        direction="inbound",
        max_concurrent_calls=10,
        inbound_destination="project.sip.livekit.cloud",
    )
    outbound = backend.ensure_trunk(
        name="voicekit-booking-out",
        direction="outbound",
        max_concurrent_calls=10,
        credential_id=credential.resource_id,
    )
    assert not backend.ensure_trunk(
        name="voicekit-booking-in",
        direction="inbound",
        max_concurrent_calls=10,
        inbound_destination="project.sip.livekit.cloud",
    ).resource.created
    binding = backend.attach_number(
        trunk_id=inbound.resource.resource_id,
        number=NUMBER,
    )
    assert binding.created
    assert outbound.sip_domain == "outbound.sip.vobiz.ai"
    create_bodies = [
        body
        for method, path, body in fake.requests
        if method == "POST" and path.endswith("/trunks")
    ]
    assert create_bodies[0] == {
        "name": "voicekit-booking-in",
        "trunk_direction": "inbound",
        "trunk_type": "INBOUND",
        "trunk_status": "enabled",
        "transport": "udp",
        "secure": False,
        "max_concurrent_calls": 10,
        "concurrent_calls_limit": 10,
        "inbound_destination": "project.sip.livekit.cloud",
    }
    backend.restore_number(snapshot)
    assert fake.number["trunk_group_id"] == "old-trunk"
    backend.delete_resource(outbound.resource)
    backend.delete_resource(inbound.resource)
    assert fake.trunks == []


@pytest.mark.parametrize(
    "values",
    [
        {"livekit_sip_uri": "https://project.sip.livekit.cloud"},
        {"livekit_sip_uri": "sip:project.sip.livekit.cloud:5070"},
        {"agent_name": "Booking"},
        {"resource_prefix": "Voicekit"},
        {"credential_id": "bad credential"},
        {"auth_username": "not-valid"},
        {"auth_password": "short"},  # pragma: allowlist secret
        {"auth_password": "x" * 129},  # pragma: allowlist secret
        {"room_prefix": ""},
        {"room_prefix": "x" * 33},
        {"max_concurrent_calls": 0},
        {"max_concurrent_calls": 1001},
    ],
)
def test_vobiz_livekit_config_rejects_unsafe_values(values: dict[str, object]) -> None:
    with pytest.raises(VoicekitError) as caught:
        _config(**values)
    assert caught.value.code == "VK-TEL-002"


def test_vobiz_livekit_config_fingerprint_is_secret_safe_and_uri_is_revalidated() -> None:
    first = _config()
    second = _config(auth_password="another-credential-secret")
    assert first.config_fingerprint != second.config_fingerprint
    assert "credential-secret" not in first.config_fingerprint

    object.__setattr__(first, "livekit_sip_uri", "not-sip")
    with pytest.raises(VoicekitError, match="invalid"):
        _ = first.livekit_sip_host


@pytest.mark.parametrize(
    "mutation",
    [
        "inbound-drift",
        "inbound-duplicate",
        "outbound-drift",
        "outbound-duplicate",
        "dispatch-drift",
        "dispatch-duplicate",
    ],
)
async def test_provisioner_rejects_livekit_drift_and_duplicate_managed_resources(
    tmp_path: Path,
    mutation: str,
) -> None:
    livekit = FakeLiveKit()
    vobiz = FakeVobiz()
    ledger = TelephonyLedger(tmp_path / f"{mutation}.sqlite3")
    provisioner = VobizLiveKitSipProvisioner(
        livekit=cast("LiveKitSipAPI", livekit),
        vobiz=cast("VobizSipBackend", vobiz),
        ledger=ledger,
    )
    try:
        await provisioner.provision(_config())
        if mutation == "inbound-drift":
            livekit.inbound[0].allowed_addresses.clear()
        elif mutation == "inbound-duplicate":
            duplicate_inbound = lk_sip.SIPInboundTrunkInfo()
            duplicate_inbound.CopyFrom(livekit.inbound[0])
            livekit.inbound.append(duplicate_inbound)
        elif mutation == "outbound-drift":
            livekit.outbound[0].address = "human-change.sip.vobiz.ai"
        elif mutation == "outbound-duplicate":
            duplicate_outbound = lk_sip.SIPOutboundTrunkInfo()
            duplicate_outbound.CopyFrom(livekit.outbound[0])
            livekit.outbound.append(duplicate_outbound)
        elif mutation == "dispatch-drift":
            livekit.dispatch[0].rule.dispatch_rule_individual.room_prefix = "human-"
        else:
            duplicate_dispatch = lk_sip.SIPDispatchRuleInfo()
            duplicate_dispatch.CopyFrom(livekit.dispatch[0])
            livekit.dispatch.append(duplicate_dispatch)

        with pytest.raises(VoicekitError) as caught:
            await provisioner.provision(_config())
        assert caught.value.code == "VK-TEL-006"
    finally:
        ledger.close()


async def test_provisioner_catalogs_unexpected_failure_and_rollback_conflict(
    tmp_path: Path,
) -> None:
    livekit = FakeLiveKit()
    vobiz = FakeVobiz()
    ledger = TelephonyLedger(tmp_path / "unexpected.sqlite3")
    provisioner = VobizLiveKitSipProvisioner(
        livekit=cast("LiveKitSipAPI", livekit),
        vobiz=cast("VobizSipBackend", vobiz),
        ledger=ledger,
    )
    try:
        vobiz.fail_at = "credential"
        vobiz.fail_error = RuntimeError("unexpected")
        with pytest.raises(VoicekitError) as unexpected:
            await provisioner.provision(_config())
        assert unexpected.value.code == "VK-TEL-006"
        assert ledger.open_provisioning(provider="vobiz-livekit")[0].state == "ambiguous"

        vobiz.fail_at = None
        result = await provisioner.provision(_config())
        livekit.fail_delete = True
        with pytest.raises(VoicekitError, match="rollback conflicted"):
            await provisioner.rollback(result.operation_id)
        assert ledger.get_provisioning(result.operation_id).state == "conflict"

        foreign = ledger.prepare_provisioning(
            provider="twilio-livekit",
            number=NUMBER,
            snapshot={},
            planned={},
        )
        with pytest.raises(VoicekitError, match="another provider"):
            await provisioner.rollback(foreign.operation_id)
    finally:
        ledger.close()


async def test_rollback_ignores_adopted_resources(tmp_path: Path) -> None:
    livekit = FakeLiveKit()
    vobiz = FakeVobiz()
    ledger = TelephonyLedger(tmp_path / "adopted.sqlite3")
    provisioner = VobizLiveKitSipProvisioner(
        livekit=cast("LiveKitSipAPI", livekit),
        vobiz=cast("VobizSipBackend", vobiz),
        ledger=ledger,
    )
    try:
        operation = ledger.prepare_provisioning(
            provider="vobiz-livekit",
            number=NUMBER,
            snapshot={"number": NUMBER, "trunk_group_id": "old"},
            planned={},
        )
        ledger.append_provisioned_resource(
            operation.operation_id,
            resource=ManagedSipResource(
                "vobiz_inbound_trunk",
                "adopted-trunk",
                False,
            ).wire(),
        )
        rolled_back = await provisioner.rollback(operation.operation_id)
        assert rolled_back.state == "rolled_back"
        assert vobiz.deleted == []
    finally:
        ledger.close()


def _http_backend(fake: FakeVobizHTTP) -> tuple[VobizSipHTTPBackend, httpx.Client]:
    client = httpx.Client(
        transport=httpx.MockTransport(fake.handler),
        base_url="https://api.vobiz.ai",
    )
    return (
        VobizSipHTTPBackend(
            auth_id="MA_VOBIZ_TEST",
            auth_token="not-a-real-token",  # pragma: allowlist secret
            client=client,
        ),
        client,
    )


def test_vobiz_http_backend_rejects_credential_trunk_and_number_drift() -> None:
    fake = FakeVobizHTTP()
    backend, client = _http_backend(fake)
    try:
        fake.credentials.clear()
        with pytest.raises(VoicekitError, match="not found uniquely"):
            backend.require_credential(
                credential_id="credential-1",
                username="voicekituser",
            )
        fake.credentials = [{"id": "credential-1", "username": "human", "enabled": True}]
        with pytest.raises(VoicekitError, match="differs"):
            backend.require_credential(
                credential_id="credential-1",
                username="voicekituser",
            )

        with pytest.raises(VoicekitError, match="direction"):
            backend.ensure_trunk(
                name="bad",
                direction="sideways",
                max_concurrent_calls=10,
            )
        fake.trunks = [
            {
                "name": "duplicate",
                "trunk_id": "one",
                "trunk_domain": "one.sip.vobiz.ai",
            },
            {
                "name": "duplicate",
                "trunk_id": "two",
                "trunk_domain": "two.sip.vobiz.ai",
            },
        ]
        with pytest.raises(VoicekitError, match="duplicate"):
            backend.ensure_trunk(
                name="duplicate",
                direction="inbound",
                max_concurrent_calls=10,
                inbound_destination="project.sip.livekit.cloud",
            )

        fake.trunks = [
            {
                "name": "drift",
                "trunk_id": "drift-id",
                "trunk_domain": "drift.sip.vobiz.ai",
                "trunk_direction": "inbound",
                "transport": "tcp",
                "inbound_destination": "project.sip.livekit.cloud",
                "concurrent_calls_limit": 10,
            }
        ]
        with pytest.raises(VoicekitError, match="differs"):
            backend.ensure_trunk(
                name="drift",
                direction="inbound",
                max_concurrent_calls=10,
                inbound_destination="project.sip.livekit.cloud",
            )

        fake.trunks.clear()
        fake.number["trunk_group_id"] = "already"
        assert not backend.attach_number(trunk_id="already", number=NUMBER).created
        fake.number["trunk_group_id"] = "before"
        fake.apply_assignment = False
        with pytest.raises(VoicekitError) as unconfirmed:
            backend.attach_number(trunk_id="new", number=NUMBER)
        assert unconfirmed.value.code == "VK-TEL-011"

        fake.number["trunk_group_id"] = None
        backend.restore_number({"number": NUMBER, "trunk_group_id": None})
        fake.number["trunk_group_id"] = "before"
        with pytest.raises(VoicekitError, match="did not compare equal"):
            backend.restore_number({"number": NUMBER, "trunk_group_id": "old"})
        with pytest.raises(VoicekitError, match="unknown"):
            backend.delete_resource(ManagedSipResource("vobiz_credential", "credential-1", True))

        fake.number["e164"] = "+918071234500"
        with pytest.raises(VoicekitError, match="0 exact"):
            backend.snapshot_number(NUMBER)
    finally:
        client.close()


@pytest.mark.parametrize(
    ("document", "expected_code"),
    [
        ({"credentials": "bad"}, "VK-TEL-011"),
        ({"credentials": [1]}, "VK-TEL-011"),
        ("bad", "VK-TEL-011"),
        (b"{", "VK-TEL-011"),
    ],
)
def test_vobiz_http_backend_rejects_malformed_envelopes(
    document: object,
    expected_code: str,
) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        if isinstance(document, bytes):
            return httpx.Response(200, content=document)
        return httpx.Response(200, json=document)

    client = httpx.Client(
        transport=httpx.MockTransport(handler),
        base_url="https://api.vobiz.ai",
    )
    backend = VobizSipHTTPBackend(
        auth_id="MA_VOBIZ_TEST",
        auth_token="not-a-real-token",  # pragma: allowlist secret
        client=client,
    )
    try:
        with pytest.raises(VoicekitError) as caught:
            backend.require_credential(
                credential_id="credential-1",
                username="voicekituser",
            )
        assert caught.value.code == expected_code
    finally:
        client.close()


@pytest.mark.parametrize(
    ("status", "expected_code"),
    [(400, "VK-TEL-006"), (503, "VK-TEL-011")],
)
def test_vobiz_http_backend_distinguishes_definitive_and_ambiguous_http(
    status: int,
    expected_code: str,
) -> None:
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(status, json={"error": "failure"})
        ),
        base_url="https://api.vobiz.ai",
    )
    backend = VobizSipHTTPBackend(
        auth_id="MA_VOBIZ_TEST",
        auth_token="not-a-real-token",  # pragma: allowlist secret
        client=client,
    )
    try:
        with pytest.raises(VoicekitError) as caught:
            backend.snapshot_number(NUMBER)
        assert caught.value.code == expected_code
    finally:
        client.close()


def test_vobiz_http_backend_network_and_object_shape_failures_are_ambiguous() -> None:
    def network_failure(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline", request=request)

    network_client = httpx.Client(
        transport=httpx.MockTransport(network_failure),
        base_url="https://api.vobiz.ai",
    )
    backend = VobizSipHTTPBackend(
        auth_id="MA_VOBIZ_TEST",
        auth_token="not-a-real-token",  # pragma: allowlist secret
        client=network_client,
    )
    try:
        with pytest.raises(VoicekitError) as caught:
            backend.snapshot_number(NUMBER)
        assert caught.value.code == "VK-TEL-011"
    finally:
        network_client.close()

    fake = FakeVobizHTTP()

    def malformed_create(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return fake.handler(request)
        return httpx.Response(201, json=[])

    client = httpx.Client(
        transport=httpx.MockTransport(malformed_create),
        base_url="https://api.vobiz.ai",
    )
    malformed = VobizSipHTTPBackend(
        auth_id="MA_VOBIZ_TEST",
        auth_token="not-a-real-token",  # pragma: allowlist secret
        client=client,
    )
    try:
        with pytest.raises(VoicekitError) as caught:
            malformed.ensure_trunk(
                name="new",
                direction="outbound",
                max_concurrent_calls=10,
                credential_id="credential-1",
            )
        assert caught.value.code == "VK-TEL-011"
    finally:
        client.close()


def test_vobiz_http_backend_accepts_a_direct_list_envelope() -> None:
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200,
                json=[
                    {
                        "id": "credential-1",
                        "username": "voicekituser",
                        "enabled": True,
                    }
                ],
            )
        ),
        base_url="https://api.vobiz.ai",
    )
    backend = VobizSipHTTPBackend(
        auth_id="MA_VOBIZ_TEST",
        auth_token="not-a-real-token",  # pragma: allowlist secret
        client=client,
    )
    try:
        credential = backend.require_credential(
            credential_id="credential-1",
            username="voicekituser",
        )
        assert credential.resource_id == "credential-1"
    finally:
        client.close()


def test_vobiz_http_backend_requires_token_and_valid_sip_domain() -> None:
    with pytest.raises(VoicekitError, match="AUTH_TOKEN"):
        VobizSipHTTPBackend(auth_id="MA_VOBIZ_TEST", auth_token="")

    fake = FakeVobizHTTP()

    def invalid_domain(request: httpx.Request) -> httpx.Response:
        response = fake.handler(request)
        if request.method == "POST" and request.url.path.endswith("/trunks"):
            payload = cast("dict[str, object]", response.json())
            payload["trunk_domain"] = "attacker.example"
            return httpx.Response(201, json=payload)
        return response

    client = httpx.Client(
        transport=httpx.MockTransport(invalid_domain),
        base_url="https://api.vobiz.ai",
    )
    backend = VobizSipHTTPBackend(
        auth_id="MA_VOBIZ_TEST",
        auth_token="not-a-real-token",  # pragma: allowlist secret
        client=client,
    )
    try:
        with pytest.raises(VoicekitError, match="invalid SIP domain"):
            backend.ensure_trunk(
                name="new",
                direction="outbound",
                max_concurrent_calls=10,
                credential_id="credential-1",
            )
    finally:
        client.close()
