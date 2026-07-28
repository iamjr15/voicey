from __future__ import annotations

import hashlib
from collections.abc import Mapping
from io import BytesIO
from typing import Any, cast

import boto3
import pytest
from botocore.client import Config
from botocore.exceptions import EndpointConnectionError

from voicekit.errors import VoicekitError
from voicekit.storage.s3 import S3ArtifactStore, S3Client


class _Body(BytesIO):
    closed_by_store = False

    def close(self) -> None:
        self.closed_by_store = True
        super().close()


class _MemoryS3:
    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], tuple[bytes, dict[str, str]]] = {}
        self.deleted: list[tuple[str, str]] = []
        self.heads: list[str] = []
        self.last_body: _Body | None = None
        self.fail = False

    def put_object(self, **kwargs: object) -> Mapping[str, object]:
        self._check()
        bucket = cast("str", kwargs["Bucket"])
        key = cast("str", kwargs["Key"])
        body = cast("bytes", kwargs["Body"])
        metadata = cast("dict[str, str]", kwargs["Metadata"])
        assert kwargs["ContentLength"] == len(body)
        assert kwargs["ContentType"] == "application/octet-stream"
        self.objects[(bucket, key)] = (body, metadata)
        return {}

    def get_object(self, **kwargs: object) -> Mapping[str, object]:
        self._check()
        bucket = cast("str", kwargs["Bucket"])
        key = cast("str", kwargs["Key"])
        body, metadata = self.objects[(bucket, key)]
        self.last_body = _Body(body)
        return {"Body": self.last_body, "Metadata": metadata}

    def delete_object(self, **kwargs: object) -> Mapping[str, object]:
        self._check()
        item = (cast("str", kwargs["Bucket"]), cast("str", kwargs["Key"]))
        self.objects.pop(item, None)
        self.deleted.append(item)
        return {}

    def head_bucket(self, **kwargs: object) -> Mapping[str, object]:
        self._check()
        self.heads.append(cast("str", kwargs["Bucket"]))
        return {}

    def _check(self) -> None:
        if self.fail:
            raise EndpointConnectionError(endpoint_url="https://objects.example.test")


class _MissingBodyS3(_MemoryS3):
    def get_object(self, **kwargs: object) -> Mapping[str, object]:
        self._check()
        return {"Metadata": {"voicekit-sha256": "0" * 64}}


class _SessionCapture:
    def __init__(self, **kwargs: str) -> None:
        self.session_kwargs = kwargs
        self.client_kwargs: dict[str, object] = {}
        self.client_value = _MemoryS3()

    def client(
        self,
        _service: str,
        *,
        endpoint_url: str | None,
        region_name: str | None,
        config: Config,
    ) -> _MemoryS3:
        self.client_kwargs = {
            "endpoint_url": endpoint_url,
            "region_name": region_name,
            "config": config,
        }
        return self.client_value


@pytest.mark.asyncio
async def test_s3_artifacts_are_namespaced_checksummed_and_idempotently_deleted() -> None:
    client = _MemoryS3()
    store = S3ArtifactStore(
        "voicekit-artifacts",
        prefix="/production/relay/",
        client=cast("S3Client", client),
    )

    assert await store.ready()
    await store.put("recordings/call.wav", b"audio")
    stored = client.objects[("voicekit-artifacts", "production/relay/recordings/call.wav")]
    assert stored == (b"audio", {"voicekit-sha256": hashlib.sha256(b"audio").hexdigest()})
    assert await store.read("recordings/call.wav") == b"audio"
    assert client.last_body is not None
    assert client.last_body.closed_by_store

    await store.delete("recordings/call.wav")
    await store.delete("recordings/call.wav")
    assert client.deleted == [
        ("voicekit-artifacts", "production/relay/recordings/call.wav"),
        ("voicekit-artifacts", "production/relay/recordings/call.wav"),
    ]


@pytest.mark.asyncio
async def test_s3_round_trip_removes_probe_and_detects_tampering() -> None:
    client = _MemoryS3()
    store = S3ArtifactStore("voicekit-artifacts", client=cast("S3Client", client))

    await store.verify_round_trip("deployment-42")
    assert not client.objects
    assert len(client.deleted) == 1

    await store.put("recordings/call.wav", b"audio")
    body, _ = client.objects[("voicekit-artifacts", "recordings/call.wav")]
    client.objects[("voicekit-artifacts", "recordings/call.wav")] = (
        body,
        {"voicekit-sha256": "0" * 64},
    )
    with pytest.raises(VoicekitError) as caught:
        await store.read("recordings/call.wav")
    assert caught.value.code == "VK-ART-002"
    assert client.last_body is not None
    assert client.last_body.closed_by_store


@pytest.mark.asyncio
async def test_s3_transport_failures_are_catalogued() -> None:
    client = _MemoryS3()
    client.fail = True
    store = S3ArtifactStore("voicekit-artifacts", client=cast("S3Client", client))

    with pytest.raises(VoicekitError) as readiness:
        await store.ready()
    assert readiness.value.code == "VK-ART-003"

    for operation in (
        store.put("recordings/call.wav", b"audio"),
        store.read("recordings/call.wav"),
        store.delete("recordings/call.wav"),
    ):
        with pytest.raises(VoicekitError) as caught:
            await operation
        assert caught.value.code == "VK-ART-002"


@pytest.mark.parametrize(
    ("bucket", "endpoint", "prefix"),
    [
        ("Bad_Bucket", None, ""),
        ("192.168.1.1", None, ""),
        ("voicekit-artifacts", "http://objects.example.test", ""),
        (
            "voicekit-artifacts",
            "https://user:secret@objects.example.test",  # pragma: allowlist secret
            "",
        ),  # pragma: allowlist secret
        ("voicekit-artifacts", "https://objects.example.test/path", ""),
        ("voicekit-artifacts", None, "../escape"),
    ],
)
def test_s3_configuration_rejects_unsafe_values(
    bucket: str,
    endpoint: str | None,
    prefix: str,
) -> None:
    with pytest.raises(VoicekitError) as caught:
        S3ArtifactStore(
            bucket,
            endpoint_url=endpoint,
            prefix=prefix,
            client=cast("S3Client", _MemoryS3()),
        )
    assert caught.value.code in {"VK-ART-001", "VK-ART-003"}


def test_s3_configuration_accepts_loopback_http_and_rejects_partial_credentials() -> None:
    S3ArtifactStore(
        "voicekit-artifacts",
        endpoint_url="http://127.0.0.1:9000",
        client=cast("S3Client", _MemoryS3()),
    )

    with pytest.raises(VoicekitError) as caught:
        S3ArtifactStore(
            "voicekit-artifacts",
            access_key_id="only-one-half",
            client=cast("S3Client", _MemoryS3()),
        )
    assert caught.value.code == "VK-ART-003"


def test_s3_builds_the_installed_client_with_explicit_security_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[_SessionCapture] = []

    def session_factory(**kwargs: str) -> _SessionCapture:
        session = _SessionCapture(**kwargs)
        captured.append(session)
        return session

    monkeypatch.setattr(boto3, "Session", session_factory)
    store = S3ArtifactStore(
        "voicekit-artifacts",
        endpoint_url="https://objects.example.test",
        region_name="auto",
        access_key_id="access-id",
        secret_access_key="secret-value",  # pragma: allowlist secret
        force_path_style=True,
    )

    assert store.bucket == "voicekit-artifacts"
    assert captured[0].session_kwargs == {
        "aws_access_key_id": "access-id",
        "aws_secret_access_key": "secret-value",  # pragma: allowlist secret
    }
    assert captured[0].client_kwargs["endpoint_url"] == "https://objects.example.test"
    assert captured[0].client_kwargs["region_name"] == "auto"
    config = cast("Config", captured[0].client_kwargs["config"])
    assert cast("dict[str, str]", vars(config)["s3"]) == {"addressing_style": "path"}


@pytest.mark.asyncio
async def test_s3_rejects_missing_body_and_round_trip_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    missing_body = S3ArtifactStore(
        "voicekit-artifacts",
        client=cast("S3Client", _MissingBodyS3()),
    )
    with pytest.raises(VoicekitError) as missing:
        await missing_body.read("recordings/call.wav")
    assert missing.value.code == "VK-ART-002"

    client = _MemoryS3()
    store = S3ArtifactStore("voicekit-artifacts", client=cast("S3Client", client))

    async def mismatched_read(_storage_key: str) -> bytes:
        return b"different"

    monkeypatch.setattr(store, "read", cast("Any", mismatched_read))
    with pytest.raises(VoicekitError) as mismatch:
        await store.verify_round_trip("deployment-mismatch")
    assert mismatch.value.code == "VK-ART-003"
    assert not client.objects
