"""S3-compatible durable artifact storage for managed deployments."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import ipaddress
import re
from collections.abc import Mapping
from typing import Protocol, cast
from urllib.parse import urlsplit

import boto3
from botocore.client import Config
from botocore.exceptions import BotoCoreError, ClientError

from voicey.errors import VoiceyError
from voicey.storage.artifacts import validate_artifact_key

_BUCKET_PATTERN = re.compile(r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")
_DIGEST_METADATA_KEY = "voicey-sha256"


class _ReadableBody(Protocol):
    def read(self) -> bytes: ...

    def close(self) -> None: ...


class S3Client(Protocol):
    def put_object(self, **kwargs: object) -> Mapping[str, object]: ...

    def get_object(self, **kwargs: object) -> Mapping[str, object]: ...

    def delete_object(self, **kwargs: object) -> Mapping[str, object]: ...

    def head_bucket(self, **kwargs: object) -> Mapping[str, object]: ...


class S3ArtifactStore:
    """Async facade over a private S3-compatible bucket.

    Object keys are traversal-safe and namespaced under ``prefix``. Every write
    stores a SHA-256 digest in object metadata, and reads fail closed if bytes
    no longer match that digest.
    """

    def __init__(
        self,
        bucket: str,
        *,
        prefix: str = "",
        endpoint_url: str | None = None,
        region_name: str | None = None,
        access_key_id: str | None = None,
        secret_access_key: str | None = None,
        force_path_style: bool = False,
        client: S3Client | None = None,
    ) -> None:
        self.bucket = _validate_bucket(bucket)
        self.prefix = _validate_prefix(prefix)
        _validate_endpoint(endpoint_url)
        if (access_key_id is None) is not (secret_access_key is None):
            raise VoiceyError(
                "VY-ART-003",
                detail="Object-store access key id and secret must be configured together.",
            )
        if client is None:
            session_arguments: dict[str, str] = {}
            if access_key_id is not None and secret_access_key is not None:
                session_arguments["aws_access_key_id"] = access_key_id
                session_arguments["aws_secret_access_key"] = secret_access_key
            session = boto3.Session(**session_arguments)
            config = Config(
                retries={"mode": "standard", "max_attempts": 4},
                s3={
                    "addressing_style": "path" if force_path_style else "virtual",
                },
            )
            client = cast(
                "S3Client",
                session.client(
                    "s3",
                    endpoint_url=endpoint_url,
                    region_name=region_name,
                    config=config,
                ),
            )
        self._client = client

    async def ready(self) -> bool:
        """Verify that the configured credentials can reach the bucket."""
        try:
            await asyncio.to_thread(self._client.head_bucket, Bucket=self.bucket)
        except (BotoCoreError, ClientError, OSError) as exc:
            raise VoiceyError(
                "VY-ART-003",
                detail="Object-store bucket readiness check failed.",
            ) from exc
        return True

    async def put(self, storage_key: str, content: bytes) -> None:
        """Persist one checksummed private object."""
        key = self._key(storage_key)
        digest = hashlib.sha256(content).hexdigest()
        try:
            await asyncio.to_thread(
                self._client.put_object,
                Bucket=self.bucket,
                Key=key,
                Body=content,
                ContentLength=len(content),
                ContentType="application/octet-stream",
                Metadata={_DIGEST_METADATA_KEY: digest},
            )
        except (BotoCoreError, ClientError, OSError) as exc:
            raise VoiceyError(
                "VY-ART-002",
                detail=f"{storage_key}: object write failed.",
            ) from exc

    async def read(self, storage_key: str) -> bytes:
        """Read one object and verify its voicey-owned digest metadata."""
        key = self._key(storage_key)
        body: _ReadableBody | None = None
        try:
            response = await asyncio.to_thread(
                self._client.get_object,
                Bucket=self.bucket,
                Key=key,
            )
            raw_body = response.get("Body")
            if raw_body is None or not hasattr(raw_body, "read") or not hasattr(raw_body, "close"):
                raise VoiceyError(
                    "VY-ART-002",
                    detail=f"{storage_key}: object response has no readable body.",
                )
            body = cast("_ReadableBody", raw_body)
            content = await asyncio.to_thread(body.read)
            metadata = response.get("Metadata")
            digest = (
                cast("Mapping[object, object]", metadata).get(_DIGEST_METADATA_KEY)
                if isinstance(metadata, Mapping)
                else None
            )
            if not isinstance(digest, str) or not _digest_matches(content, digest):
                raise VoiceyError(
                    "VY-ART-002",
                    detail=f"{storage_key}: object checksum is missing or invalid.",
                )
            return content
        except VoiceyError:
            raise
        except (BotoCoreError, ClientError, OSError) as exc:
            raise VoiceyError("VY-ART-002", detail=f"{storage_key}: object read failed.") from exc
        finally:
            if body is not None:
                body.close()

    async def delete(self, storage_key: str) -> None:
        """Idempotently delete exactly one namespaced object."""
        key = self._key(storage_key)
        try:
            await asyncio.to_thread(
                self._client.delete_object,
                Bucket=self.bucket,
                Key=key,
            )
        except (BotoCoreError, ClientError, OSError) as exc:
            raise VoiceyError(
                "VY-ART-002",
                detail=f"{storage_key}: object delete failed.",
            ) from exc

    async def verify_round_trip(self, nonce: str) -> None:
        """Exercise write/read/delete during a deployment persistence preflight."""
        safe_nonce = base64.urlsafe_b64encode(hashlib.sha256(nonce.encode()).digest()).decode()
        key = f"preflight/{safe_nonce.rstrip('=')}.bin"
        expected = hashlib.sha256(f"voicey:{nonce}".encode()).digest()
        await self.put(key, expected)
        try:
            actual = await self.read(key)
            if actual != expected:
                raise VoiceyError(
                    "VY-ART-003",
                    detail="Object-store preflight returned different bytes.",
                )
        finally:
            await self.delete(key)

    def _key(self, storage_key: str) -> str:
        safe = validate_artifact_key(storage_key).as_posix()
        return f"{self.prefix}/{safe}" if self.prefix else safe


def _validate_bucket(bucket: str) -> str:
    if (
        _BUCKET_PATTERN.fullmatch(bucket) is None
        or ".." in bucket
        or ".-" in bucket
        or "-." in bucket
        or _is_ip_address(bucket)
    ):
        raise VoiceyError("VY-ART-003", detail=f"Invalid object-store bucket: {bucket!r}.")
    return bucket


def _validate_prefix(prefix: str) -> str:
    normalized = prefix.strip("/")
    if not normalized:
        return ""
    return validate_artifact_key(normalized).as_posix()


def _validate_endpoint(endpoint_url: str | None) -> None:
    if endpoint_url is None:
        return
    parsed = urlsplit(endpoint_url)
    loopback = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
    if (
        parsed.scheme not in ({"https", "http"} if loopback else {"https"})
        or parsed.username is not None
        or parsed.password is not None
        or parsed.hostname is None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise VoiceyError(
            "VY-ART-003",
            detail=(
                "Object-store endpoint must be HTTPS (HTTP is loopback-only) "
                "with no credentials or path."
            ),
        )


def _is_ip_address(value: str) -> bool:
    try:
        ipaddress.ip_address(value)
    except ValueError:
        return False
    return True


def _digest_matches(content: bytes, expected: str) -> bool:
    actual = hashlib.sha256(content).hexdigest()
    return len(expected) == len(actual) and hmac.compare_digest(actual, expected.lower())
