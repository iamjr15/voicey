"""Carrier recording ingestion and authenticated engine artifact access."""

from __future__ import annotations

import secrets
from typing import Protocol
from urllib.parse import urlsplit

from voicekit.errors import VoicekitError
from voicekit.storage.artifacts import ArtifactStore
from voicekit.storage.models import RecordingReady
from voicekit.storage.repository import StorageRepository
from voicekit.telephony.models import CallEvent


class TwilioRecordingAdapter(Protocol):
    async def download_recording(
        self,
        recording_sid: str,
        *,
        artifact_store: ArtifactStore,
        storage_key: str,
        max_bytes: int = 100 * 1024 * 1024,
    ) -> str: ...


class TelnyxRecordingAdapter(Protocol):
    async def download_recording(
        self,
        recording_url: str,
        *,
        artifact_store: ArtifactStore,
        storage_key: str,
        max_bytes: int = 100 * 1024 * 1024,
    ) -> str: ...


class VobizRecordingAdapter(Protocol):
    async def download_recording(
        self,
        recording_url: str,
        *,
        artifact_store: ArtifactStore,
        storage_key: str,
        max_bytes: int = 100 * 1024 * 1024,
    ) -> str: ...


class PipecatRecordingHandler:
    """Normalize verified carrier callbacks into one engine-owned artifact."""

    def __init__(
        self,
        *,
        repository: StorageRepository,
        artifact_store: ArtifactStore,
        access_base: str,
        current_secret: str,
        previous_secret: str | None = None,
        twilio: TwilioRecordingAdapter | None = None,
        telnyx: TelnyxRecordingAdapter | None = None,
        vobiz: VobizRecordingAdapter | None = None,
    ) -> None:
        parsed = urlsplit(access_base.rstrip("/"))
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise VoicekitError(
                "VK-TEL-009",
                detail="recording access base must be a normalized HTTPS base URL.",
            )
        if not current_secret:
            raise VoicekitError(
                "VK-TEL-009",
                detail="recording access requires the current webhook secret.",
            )
        self.repository = repository
        self.artifact_store = artifact_store
        self.access_base = access_base.rstrip("/")
        self.current_secret = current_secret
        self.previous_secret = previous_secret
        self.twilio = twilio
        self.telnyx = telnyx
        self.vobiz = vobiz

    async def handle_twilio(self, event: CallEvent) -> None:
        if event.type == "recording_failed":
            await self.repository.mark_recording_failed(event.provider_call_id)
            return
        if event.type != "recording_ready" or event.recording_sid is None:
            raise VoicekitError("VK-TEL-009", detail="invalid Twilio recording callback.")
        if self.twilio is None:
            raise VoicekitError("VK-TEL-009", detail="Twilio recording adapter is unavailable.")
        pending = await self._pending(event.provider_call_id)
        if pending is None:
            return
        recording_id, storage_key = pending
        await self.twilio.download_recording(
            event.recording_sid,
            artifact_store=self.artifact_store,
            storage_key=storage_key,
        )
        await self._mark_ready(recording_id, storage_key)

    async def handle_telnyx(self, event: CallEvent) -> None:
        if event.type == "recording_failed":
            await self.repository.mark_recording_failed(event.provider_call_id)
            return
        if (
            event.type != "recording_ready"
            or event.recording_sid is None
            or event.recording_url is None
        ):
            raise VoicekitError("VK-TEL-009", detail="invalid Telnyx recording callback.")
        if self.telnyx is None:
            raise VoicekitError("VK-TEL-009", detail="Telnyx recording adapter is unavailable.")
        pending = await self._pending(event.provider_call_id)
        if pending is None:
            return
        recording_id, storage_key = pending
        await self.telnyx.download_recording(
            event.recording_url,
            artifact_store=self.artifact_store,
            storage_key=storage_key,
        )
        await self._mark_ready(recording_id, storage_key)

    async def handle_vobiz(self, event: CallEvent) -> None:
        if event.type == "recording_failed":
            await self.repository.mark_recording_failed(event.provider_call_id)
            return
        if (
            event.type != "recording_ready"
            or event.recording_sid is None
            or event.recording_url is None
        ):
            raise VoicekitError("VK-TEL-009", detail="invalid Vobiz recording callback.")
        if self.vobiz is None:
            raise VoicekitError("VK-TEL-009", detail="Vobiz recording adapter is unavailable.")
        pending = await self._pending(event.provider_call_id)
        if pending is None:
            return
        recording_id, storage_key = pending
        await self.vobiz.download_recording(
            event.recording_url,
            artifact_store=self.artifact_store,
            storage_key=storage_key,
        )
        await self._mark_ready(recording_id, storage_key)

    async def read(self, recording_id: str, authorization: str | None) -> bytes:
        """Return protected bytes only to a current or previous webhook-secret bearer."""
        supplied = authorization or ""
        allowed = (f"Bearer {self.current_secret}",)
        if self.previous_secret:
            allowed += (f"Bearer {self.previous_secret}",)
        if not any(secrets.compare_digest(supplied, candidate) for candidate in allowed):
            raise VoicekitError("VK-WEB-004", detail="recording bearer authorization failed.")
        snapshot = await self.repository.get_recording(recording_id)
        if snapshot.status != "ready" or snapshot.storage_key is None:
            raise VoicekitError("VK-RES-010", detail=recording_id)
        return await self.artifact_store.read(snapshot.storage_key)

    async def _pending(self, call_id: str) -> tuple[str, str] | None:
        snapshot = await self.repository.get_recording_for_call(call_id)
        if snapshot is None:
            raise VoicekitError("VK-RES-010", detail=call_id)
        if snapshot.status == "ready":
            return None
        if snapshot.status == "failed":
            raise VoicekitError("VK-RES-010", detail=f"{call_id} recording already failed.")
        call = await self.repository.get_call(call_id)
        if call.status == "active":
            raise VoicekitError(
                "VK-RES-010",
                detail="recording-ready arrived before terminal persistence.",
            )
        return snapshot.recording_id, f"recordings/{snapshot.recording_id}.mp3"

    async def _mark_ready(self, recording_id: str, storage_key: str) -> None:
        await self.repository.mark_recording_ready(
            RecordingReady(
                recording_id=recording_id,
                access_url=f"{self.access_base}/recordings/{recording_id}",
                storage_key=storage_key,
            )
        )
