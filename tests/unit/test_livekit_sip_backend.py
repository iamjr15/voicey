from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import pytest

from voicey.errors import VoiceyError
from voicey.runtimes.livekit.sip import (
    ManagedSipResource,
    TwilioElasticSipBackend,
    TwilioLiveKitSipConfig,
    TwilioTrunkRecordingReconciler,
)


class FakeLeafCollection:
    def __init__(self, kind: str, parent: FakeTwilioClient, parent_id: str) -> None:
        self.kind = kind
        self.parent = parent
        self.parent_id = parent_id
        self.items: dict[str, Any] = {}

    def list(self) -> list[Any]:
        return list(self.items.values())

    def create(self, **values: object) -> Any:
        if self.kind == "binding":
            # Twilio's trunk CredentialList mapping resource uses the bound
            # CredentialList SID as its own `sid`; it does not expose a
            # separate `credential_list_sid` attribute.
            sid = str(values["credential_list_sid"])
            item = SimpleNamespace(sid=sid, trunk_sid=self.parent_id)
        else:
            sid = f"{self.kind}-{len(self.items) + 1}"
            item = SimpleNamespace(sid=sid, **values)
        self.items[sid] = item
        if self.kind == "phone":
            number = self.parent.numbers[str(values["phone_number_sid"])]
            number.trunk_sid = self.parent_id
        return item

    def __call__(self, sid: str) -> Any:
        collection = self

        class Context:
            def delete(self) -> bool:
                collection.items.pop(sid, None)
                return collection.parent.delete_confirmed

        return Context()


class FakeRecordingContext:
    def __init__(self) -> None:
        self.mode = "do-not-record"
        self.trim = "do-not-trim"
        self.confirm_updates = True

    def fetch(self) -> Any:
        return SimpleNamespace(mode=self.mode)

    def update(self, *, mode: str, trim: str) -> Any:
        self.trim = trim
        if self.confirm_updates:
            self.mode = mode
        return SimpleNamespace(mode=self.mode)


class FakeTrunkContext:
    def __init__(self, client: FakeTwilioClient, sid: str) -> None:
        self.client = client
        self.sid = sid
        self.origination_urls = FakeLeafCollection("orig", client, sid)
        self.credentials_lists = FakeLeafCollection("binding", client, sid)
        self.phone_numbers = FakeLeafCollection("phone", client, sid)
        self.recording = FakeRecordingContext()

    def recordings(self) -> FakeRecordingContext:
        return self.recording

    def delete(self) -> bool:
        self.client.trunks.items.pop(self.sid, None)
        return self.client.delete_confirmed


class FakeTrunks:
    def __init__(self, client: FakeTwilioClient) -> None:
        self.client = client
        self.items: dict[str, Any] = {}
        self.contexts: dict[str, FakeTrunkContext] = {}

    def list(self) -> list[Any]:
        return list(self.items.values())

    def create(self, **values: object) -> Any:
        sid = f"TK{len(self.items) + 1}"
        item = SimpleNamespace(sid=sid, **values)
        self.items[sid] = item
        self.contexts[sid] = FakeTrunkContext(self.client, sid)
        return item

    def __call__(self, sid: str) -> FakeTrunkContext:
        return self.contexts[sid]


class FakeCredentials:
    def __init__(self, client: FakeTwilioClient, list_sid: str) -> None:
        self.client = client
        self.list_sid = list_sid
        self.items: dict[str, Any] = {}

    def list(self) -> list[Any]:
        return list(self.items.values())

    def create(self, **values: object) -> Any:
        sid = f"CR{len(self.items) + 1}"
        item = SimpleNamespace(sid=sid, **values)
        self.items[sid] = item
        return item

    def __call__(self, sid: str) -> Any:
        collection = self

        class Context:
            def delete(self) -> bool:
                collection.items.pop(sid, None)
                return collection.client.delete_confirmed

        return Context()


class FakeCredentialListContext:
    def __init__(self, client: FakeTwilioClient, sid: str) -> None:
        self.client = client
        self.sid = sid
        self.credentials = FakeCredentials(client, sid)

    def delete(self) -> bool:
        self.client.credential_lists.items.pop(self.sid, None)
        return self.client.delete_confirmed


class FakeCredentialLists:
    def __init__(self, client: FakeTwilioClient) -> None:
        self.client = client
        self.items: dict[str, Any] = {}
        self.contexts: dict[str, FakeCredentialListContext] = {}

    def list(self) -> list[Any]:
        return list(self.items.values())

    def create(self, **values: object) -> Any:
        sid = f"CL{len(self.items) + 1}"
        item = SimpleNamespace(sid=sid, **values)
        self.items[sid] = item
        self.contexts[sid] = FakeCredentialListContext(self.client, sid)
        return item

    def __call__(self, sid: str) -> FakeCredentialListContext:
        return self.contexts[sid]


class FakeNumberContext:
    def __init__(self, number: Any) -> None:
        self.number = number

    def fetch(self) -> Any:
        return self.number

    def update(self, **values: object) -> Any:
        for key, value in values.items():
            setattr(self.number, key, value or None)
        return self.number


class FakeIncomingNumbers:
    def __init__(self, client: FakeTwilioClient) -> None:
        self.client = client

    def list(self, *, phone_number: str, limit: int) -> list[Any]:
        del limit
        return [
            value for value in self.client.numbers.values() if value.phone_number == phone_number
        ]

    def __call__(self, sid: str) -> FakeNumberContext:
        return FakeNumberContext(self.client.numbers[sid])


class FakeCoreRecordings:
    def __init__(self) -> None:
        self.items: list[Any] = []
        self.queries: list[tuple[str, int]] = []

    def list(self, *, call_sid: str, limit: int) -> list[Any]:
        self.queries.append((call_sid, limit))
        return list(self.items)


class FakeTwilioClient:
    def __init__(self) -> None:
        self.delete_confirmed = True
        self.numbers = {
            "PN1": SimpleNamespace(
                sid="PN1",
                phone_number="+14155550100",
                voice_url="https://old.example.test",
                voice_method="POST",
                voice_fallback_url=None,
                voice_fallback_method=None,
                status_callback=None,
                status_callback_method=None,
                voice_application_sid=None,
                trunk_sid=None,
            )
        }
        self.trunks = FakeTrunks(self)
        self.credential_lists = FakeCredentialLists(self)
        self.trunking = SimpleNamespace(v1=SimpleNamespace(trunks=self.trunks))
        self.sip = SimpleNamespace(credential_lists=self.credential_lists)
        self.incoming_phone_numbers = FakeIncomingNumbers(self)
        self.recordings = FakeCoreRecordings()


def test_twilio_elastic_backend_full_create_reuse_restore_and_delete() -> None:
    client = FakeTwilioClient()
    backend = TwilioElasticSipBackend(client)
    snapshot = backend.snapshot_number("+14155550100")

    trunk = backend.ensure_trunk(
        name="voicey-agent-14155550100",
        domain_name="voicey-test.pstn.twilio.com",
    )
    assert trunk.created is True
    assert client.trunks.items[trunk.resource_id].secure is True
    assert (
        backend.ensure_trunk(
            name="voicey-agent-14155550100",
            domain_name="voicey-test.pstn.twilio.com",
        ).created
        is False
    )

    origination = backend.ensure_origination(
        trunk_sid=trunk.resource_id,
        name="voicey-agent-14155550100",
        sip_uri="sip:example.sip.livekit.cloud;transport=tls",
    )
    assert origination.created is True
    assert (
        backend.ensure_origination(
            trunk_sid=trunk.resource_id,
            name="voicey-agent-14155550100",
            sip_uri="sip:example.sip.livekit.cloud;transport=tls",
        ).created
        is False
    )

    backend.ensure_recording(
        trunk_sid=trunk.resource_id,
        enabled=True,
        allow_update=True,
    )
    recording = client.trunks(trunk.resource_id).recording
    assert recording.mode == "record-from-answer-dual"
    assert recording.trim == "trim-silence"
    backend.ensure_recording(
        trunk_sid=trunk.resource_id,
        enabled=True,
        allow_update=False,
    )

    credential_list = backend.ensure_credential_list(name="voicey-agent-14155550100")
    assert credential_list.created is True
    assert backend.ensure_credential_list(name="voicey-agent-14155550100").created is False
    credential = backend.ensure_credential(
        credential_list_sid=credential_list.resource_id,
        username="voicey-user",
        password="random-password",  # pragma: allowlist secret
    )
    assert credential.created is True
    assert (
        backend.ensure_credential(
            credential_list_sid=credential_list.resource_id,
            username="voicey-user",
            password="ignored-existing",  # pragma: allowlist secret
        ).created
        is False
    )
    binding = backend.ensure_credential_binding(
        trunk_sid=trunk.resource_id,
        credential_list_sid=credential_list.resource_id,
    )
    assert binding.created is True
    assert (
        backend.ensure_credential_binding(
            trunk_sid=trunk.resource_id,
            credential_list_sid=credential_list.resource_id,
        ).created
        is False
    )

    number = backend.attach_number(
        trunk_sid=trunk.resource_id,
        number="+14155550100",
    )
    assert number.created is True
    assert (
        backend.attach_number(
            trunk_sid=trunk.resource_id,
            number="+14155550100",
        ).created
        is False
    )
    backend.restore_number(snapshot)
    assert client.numbers["PN1"].trunk_sid is None
    assert client.numbers["PN1"].voice_url == "https://old.example.test"

    for resource in (binding, credential, credential_list, origination, trunk):
        backend.delete_resource(resource)


@pytest.mark.parametrize(
    ("operation", "detail"),
    [
        ("record_existing", "recording mode"),
        ("record_unconfirmed", "did not confirm"),
        ("number_unconfirmed", "number-to-trunk"),
        ("unknown_delete", "unknown"),
        ("delete_unconfirmed", "did not confirm deletion"),
    ],
)
def test_twilio_elastic_backend_rejects_unconfirmed_mutations(
    operation: str,
    detail: str,
) -> None:
    client = FakeTwilioClient()
    backend = TwilioElasticSipBackend(client)
    trunk = backend.ensure_trunk(
        name="voicey-agent-14155550100",
        domain_name="voicey-test.pstn.twilio.com",
    )

    def run() -> None:
        if operation == "record_existing":
            backend.ensure_recording(
                trunk_sid=trunk.resource_id,
                enabled=True,
                allow_update=False,
            )
        elif operation == "record_unconfirmed":
            client.trunks(trunk.resource_id).recording.confirm_updates = False
            backend.ensure_recording(
                trunk_sid=trunk.resource_id,
                enabled=True,
                allow_update=True,
            )
        elif operation == "number_unconfirmed":
            original_create = client.trunks(trunk.resource_id).phone_numbers.create

            def create_without_binding(**values: object) -> Any:
                item = original_create(**values)
                client.numbers["PN1"].trunk_sid = None
                return item

            client.trunks(trunk.resource_id).phone_numbers.create = create_without_binding
            backend.attach_number(
                trunk_sid=trunk.resource_id,
                number="+14155550100",
            )
        elif operation == "unknown_delete":
            backend.delete_resource(ManagedSipResource("unknown", "X", True))
        else:
            client.delete_confirmed = False
            backend.delete_resource(trunk)

    with pytest.raises(VoiceyError) as caught:
        run()

    assert caught.value.code == "VY-TEL-006"
    assert detail in (caught.value.detail or "")


def test_twilio_elastic_backend_rejects_duplicates_and_drift() -> None:
    client = FakeTwilioClient()
    backend = TwilioElasticSipBackend(client)
    backend.ensure_trunk(
        name="managed",
        domain_name="managed.pstn.twilio.com",
    )
    client.trunks.items["TK2"] = SimpleNamespace(
        sid="TK2",
        friendly_name="managed",
        domain_name="managed.pstn.twilio.com",
    )
    with pytest.raises(VoiceyError):
        backend.ensure_trunk(
            name="managed",
            domain_name="managed.pstn.twilio.com",
        )
    client.trunks.items.pop("TK2")
    client.trunks.items["TK1"].domain_name = "drift.pstn.twilio.com"
    with pytest.raises(VoiceyError):
        backend.ensure_trunk(
            name="managed",
            domain_name="managed.pstn.twilio.com",
        )

    client.numbers["PN2"] = SimpleNamespace(**{**vars(client.numbers["PN1"]), "sid": "PN2"})
    with pytest.raises(VoiceyError) as caught:
        backend.snapshot_number("+14155550100")
    assert caught.value.code == "VY-TEL-003"


def test_twilio_elastic_backend_correlates_only_one_completed_trunk_recording() -> None:
    client = FakeTwilioClient()
    backend = TwilioElasticSipBackend(client)
    call_sid = f"CA{'a' * 32}"
    recording_sid = f"RE{'b' * 32}"
    client.recordings.items = [
        SimpleNamespace(
            sid=f"RE{'c' * 32}",
            source="DialVerb",
            status="completed",
            duration="2",
        ),
        SimpleNamespace(
            sid=recording_sid,
            source="Trunking",
            status="completed",
            duration="12",
        ),
    ]
    recording = backend.completed_trunk_recording(call_sid)
    assert recording is not None
    assert recording.recording_sid == recording_sid
    assert recording.duration_s == 12
    assert client.recordings.queries == [(call_sid, 20)]

    client.recordings.items = []
    assert backend.completed_trunk_recording(call_sid) is None
    with pytest.raises(VoiceyError):
        backend.completed_trunk_recording("not-a-call-sid")


@pytest.mark.parametrize(
    ("items", "detail"),
    [
        (
            [
                SimpleNamespace(
                    sid=f"RE{'b' * 32}",
                    source="Trunking",
                    status="completed",
                    duration="1",
                ),
                SimpleNamespace(
                    sid=f"RE{'c' * 32}",
                    source="Trunking",
                    status="completed",
                    duration="1",
                ),
            ],
            "multiple completed",
        ),
        (
            [
                SimpleNamespace(
                    sid="invalid",
                    source="Trunking",
                    status="completed",
                    duration="1",
                )
            ],
            "invalid trunk RecordingSid",
        ),
        (
            [
                SimpleNamespace(
                    sid=f"RE{'b' * 32}",
                    source="Trunking",
                    status="completed",
                    duration="not-an-integer",
                )
            ],
            "invalid trunk recording duration",
        ),
    ],
)
def test_twilio_elastic_backend_rejects_ambiguous_recording_results(
    items: list[Any],
    detail: str,
) -> None:
    client = FakeTwilioClient()
    client.recordings.items = items
    with pytest.raises(VoiceyError) as caught:
        TwilioElasticSipBackend(client).completed_trunk_recording(f"CA{'a' * 32}")
    assert detail in (caught.value.detail or "")


@pytest.mark.asyncio
async def test_trunk_recording_reconciler_downloads_and_emits_ready() -> None:
    client = FakeTwilioClient()
    call_sid = f"CA{'a' * 32}"
    recording_sid = f"RE{'b' * 32}"
    client.recordings.items = [
        SimpleNamespace(
            sid=recording_sid,
            source="Trunking",
            status="completed",
            duration=None,
        )
    ]

    class Repository:
        def __init__(self) -> None:
            self.ready: list[Any] = []

        async def get_recording_for_call(self, call_id: str) -> Any:
            assert call_id == "call-recording"
            return SimpleNamespace(recording_id="rec-engine", status="pending")

        async def mark_recording_ready(self, update: object) -> object:
            self.ready.append(update)
            return SimpleNamespace()

    class Downloader:
        def __init__(self) -> None:
            self.items: list[tuple[str, str]] = []

        async def download_recording(
            self,
            sid: str,
            *,
            artifact_store: object,
            storage_key: str,
            max_bytes: int = 100 * 1024 * 1024,
        ) -> str:
            del artifact_store, max_bytes
            self.items.append((sid, storage_key))
            return storage_key

    repository = Repository()
    downloader = Downloader()
    reconciler = TwilioTrunkRecordingReconciler(
        backend=TwilioElasticSipBackend(client),
        downloader=cast(Any, downloader),
        repository=cast(Any, repository),
        artifact_store=cast(Any, object()),
        access_base="https://records.example.test",
    )
    assert await reconciler.wait_until_ready(
        call_id="call-recording",
        twilio_call_sid=call_sid,
        timeout_s=1,
        poll_interval_s=0.01,
    )
    assert downloader.items == [
        (recording_sid, "recordings/rec-engine.mp3"),
    ]
    assert repository.ready[0].access_url == ("https://records.example.test/recordings/rec-engine")


@pytest.mark.asyncio
async def test_trunk_recording_reconciler_pending_ready_and_validation_edges() -> None:
    client = FakeTwilioClient()

    class Repository:
        def __init__(self, snapshot: object) -> None:
            self.snapshot = snapshot

        async def get_recording_for_call(self, _call_id: str) -> object:
            return self.snapshot

    def reconciler(snapshot: object) -> TwilioTrunkRecordingReconciler:
        return TwilioTrunkRecordingReconciler(
            backend=TwilioElasticSipBackend(client),
            downloader=cast(Any, object()),
            repository=cast(Any, Repository(snapshot)),
            artifact_store=cast(Any, object()),
            access_base="https://records.example.test/",
        )

    with pytest.raises(VoiceyError, match="VY-TEL-009"):
        TwilioTrunkRecordingReconciler(
            backend=TwilioElasticSipBackend(client),
            downloader=cast(Any, object()),
            repository=cast(Any, Repository(None)),
            artifact_store=cast(Any, object()),
            access_base="http://insecure.example.test",
        )
    with pytest.raises(VoiceyError, match="VY-TEL-009"):
        await reconciler(None).reconcile(
            call_id="call-no-recording",
            twilio_call_sid=f"CA{'a' * 32}",
        )

    assert await reconciler(SimpleNamespace(recording_id="rec-ready", status="ready")).reconcile(
        call_id="call-ready",
        twilio_call_sid=f"CA{'a' * 32}",
    )
    pending = reconciler(SimpleNamespace(recording_id="rec-pending", status="pending"))
    assert (
        await pending.wait_until_ready(
            call_id="call-pending",
            twilio_call_sid=f"CA{'a' * 32}",
            timeout_s=0.001,
            poll_interval_s=0.001,
        )
        is False
    )
    with pytest.raises(VoiceyError, match="VY-TEL-009"):
        await pending.wait_until_ready(
            call_id="call-pending",
            twilio_call_sid=f"CA{'a' * 32}",
            timeout_s=0,
        )


@pytest.mark.parametrize(
    "values",
    [
        {"number": "invalid"},
        {"livekit_sip_uri": "https://not-sip.example.test"},
        {"twilio_domain_name": "not-twilio.example.test"},
        {"agent_name": "UPPER"},
        {"auth_password": ""},
        {"auth_password": "lowercasepassword1"},
        {"auth_password": "UPPERCASEPASSWORD1"},
        {"auth_password": "NoDigitsPassword"},
        {"room_prefix": ""},
    ],
)
def test_twilio_livekit_sip_config_rejects_invalid_values(
    values: dict[str, object],
) -> None:
    defaults: dict[str, object] = {
        "number": "+14155550100",
        "agent_name": "agent",
        "livekit_sip_uri": "sip:example.sip.livekit.cloud",
        "twilio_domain_name": "managed.pstn.twilio.com",
        "auth_username": "user",
        "auth_password": "VoiceyPassword1",  # pragma: allowlist secret
    }
    with pytest.raises(VoiceyError):
        TwilioLiveKitSipConfig(**{**defaults, **values})  # type: ignore[arg-type]
