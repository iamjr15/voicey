from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from voicey.errors import VoiceyError
from voicey.obs.records import NewCall
from voicey.runtimes.pipecat.recording import PipecatRecordingHandler
from voicey.storage.artifacts import ArtifactStore, LocalArtifactStore
from voicey.storage.models import ResultDeliveryConfig, TerminalRequest
from voicey.storage.sqlite import SQLiteRepository
from voicey.telephony.models import CallEvent


class TwilioDownloader:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def download_recording(
        self,
        recording_sid: str,
        *,
        artifact_store: ArtifactStore,
        storage_key: str,
        max_bytes: int = 100 * 1024 * 1024,
    ) -> str:
        assert max_bytes > 0
        self.calls.append(recording_sid)
        await artifact_store.put(storage_key, b"twilio-audio")
        return storage_key


class TelnyxDownloader:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def download_recording(
        self,
        recording_url: str,
        *,
        artifact_store: ArtifactStore,
        storage_key: str,
        max_bytes: int = 100 * 1024 * 1024,
    ) -> str:
        assert max_bytes > 0
        self.calls.append(recording_url)
        await artifact_store.put(storage_key, b"telnyx-audio")
        return storage_key


class VobizDownloader:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def download_recording(
        self,
        recording_url: str,
        *,
        artifact_store: ArtifactStore,
        storage_key: str,
        max_bytes: int = 100 * 1024 * 1024,
    ) -> str:
        assert max_bytes > 0
        self.calls.append(recording_url)
        await artifact_store.put(storage_key, b"vobiz-audio")
        return storage_key


class PlivoDownloader:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def download_recording(
        self,
        recording_url: str,
        *,
        artifact_store: ArtifactStore,
        storage_key: str,
        max_bytes: int = 100 * 1024 * 1024,
    ) -> str:
        assert max_bytes > 0
        self.calls.append(recording_url)
        await artifact_store.put(storage_key, b"plivo-audio")
        return storage_key


async def _recording_call(
    repository: SQLiteRepository,
    call_id: str,
    *,
    terminal: bool = True,
) -> None:
    now = datetime(2026, 7, 27, 12, 0, tzinfo=UTC)
    lease = await repository.begin_call(
        NewCall(
            call_id=call_id,
            agent_name="recording-test",
            runtime="pipecat",
            channel="phone",
            direction="inbound",
            provider="twilio",
            provider_call_id=call_id,
            config_hash=f"sha256:{'a' * 64}",
            started_at=now,
        ),
        owner_id="worker",
        delivery=ResultDeliveryConfig(
            endpoint="https://receiver.example.test/results",
            recording_enabled=True,
        ),
        lease_ttl=timedelta(seconds=30),
        now=now,
    )
    if terminal:
        await repository.terminalize(
            lease,
            TerminalRequest(
                event_type="call.completed",
                ended_reason="provider_hangup",
                ended_at=now + timedelta(seconds=1),
            ),
        )


@pytest.mark.asyncio
async def test_recording_handler_ingests_all_carriers_and_protects_reads(
    tmp_path: Path,
) -> None:
    current = "whsec_Y3VycmVudA=="  # pragma: allowlist secret
    previous = "whsec_cHJldmlvdXM="  # pragma: allowlist secret
    twilio = TwilioDownloader()
    telnyx = TelnyxDownloader()
    vobiz = VobizDownloader()
    plivo = PlivoDownloader()
    artifacts = LocalArtifactStore(tmp_path / "artifacts")
    async with SQLiteRepository(tmp_path / "calls.sqlite3") as repository:
        await _recording_call(repository, "CA-recording")
        await _recording_call(repository, "v3:recording")
        await _recording_call(repository, "vobiz-recording")
        await _recording_call(repository, "plivo-recording")
        handler = PipecatRecordingHandler(
            repository=repository,
            artifact_store=artifacts,
            access_base="https://voice.example.test",
            current_secret=current,
            previous_secret=previous,
            twilio=twilio,
            telnyx=telnyx,
            vobiz=vobiz,
            plivo=plivo,
        )

        await handler.handle_twilio(
            CallEvent(
                type="recording_ready",
                provider_call_id="CA-recording",
                provider_status="completed",
                recording_sid="RE-recording",
            )
        )
        await handler.handle_telnyx(
            CallEvent(
                type="recording_ready",
                provider_call_id="v3:recording",
                provider_status="call.recording.saved",
                recording_sid="recording-1",
                recording_url="https://storage.example.test/signed.mp3",
            )
        )
        await handler.handle_vobiz(
            CallEvent(
                type="recording_ready",
                provider_call_id="vobiz-recording",
                provider_status="completed",
                recording_sid="vobiz-recording-1",
                recording_url="https://storage.example.test/vobiz.mp3",
            )
        )
        await handler.handle_plivo(
            CallEvent(
                type="recording_ready",
                provider_call_id="plivo-recording",
                provider_status="completed",
                recording_sid="plivo-recording-1",
                recording_url="https://storage.example.test/plivo.mp3",
            )
        )
        twilio_snapshot = await repository.get_recording_for_call("CA-recording")
        telnyx_snapshot = await repository.get_recording_for_call("v3:recording")
        vobiz_snapshot = await repository.get_recording_for_call("vobiz-recording")
        plivo_snapshot = await repository.get_recording_for_call("plivo-recording")
        assert twilio_snapshot is not None
        assert telnyx_snapshot is not None
        assert vobiz_snapshot is not None
        assert plivo_snapshot is not None

        assert (
            await handler.read(
                twilio_snapshot.recording_id,
                f"Bearer {current}",
            )
            == b"twilio-audio"
        )
        assert (
            await handler.read(
                telnyx_snapshot.recording_id,
                f"Bearer {previous}",
            )
            == b"telnyx-audio"
        )
        assert (
            await handler.read(
                vobiz_snapshot.recording_id,
                f"Bearer {current}",
            )
            == b"vobiz-audio"
        )
        assert (
            await handler.read(
                plivo_snapshot.recording_id,
                f"Bearer {current}",
            )
            == b"plivo-audio"
        )
        with pytest.raises(VoiceyError) as unauthorized:
            await handler.read(twilio_snapshot.recording_id, "Bearer wrong")

        await handler.handle_twilio(
            CallEvent(
                type="recording_ready",
                provider_call_id="CA-recording",
                provider_status="completed",
                recording_sid="RE-recording",
            )
        )

    assert twilio.calls == ["RE-recording"]
    assert telnyx.calls == ["https://storage.example.test/signed.mp3"]
    assert vobiz.calls == ["https://storage.example.test/vobiz.mp3"]
    assert plivo.calls == ["https://storage.example.test/plivo.mp3"]
    assert unauthorized.value.code == "VY-WEB-004"
    assert twilio_snapshot.status == "ready"
    assert twilio_snapshot.access_url is not None
    assert "RE-recording" not in twilio_snapshot.access_url


@pytest.mark.asyncio
async def test_recording_handler_marks_failure_and_retries_premature_ready(
    tmp_path: Path,
) -> None:
    twilio = TwilioDownloader()
    async with SQLiteRepository(tmp_path / "calls.sqlite3") as repository:
        await _recording_call(repository, "CA-failed")
        await _recording_call(repository, "CA-active", terminal=False)
        handler = PipecatRecordingHandler(
            repository=repository,
            artifact_store=LocalArtifactStore(tmp_path / "artifacts"),
            access_base="https://voice.example.test",
            current_secret="whsec_dGVzdA==",  # pragma: allowlist secret
            twilio=twilio,
        )
        await handler.handle_twilio(
            CallEvent(
                type="recording_failed",
                provider_call_id="CA-failed",
                provider_status="absent",
            )
        )
        failed = await repository.get_recording_for_call("CA-failed")
        assert failed is not None
        assert failed.status == "failed"

        with pytest.raises(VoiceyError) as premature:
            await handler.handle_twilio(
                CallEvent(
                    type="recording_ready",
                    provider_call_id="CA-active",
                    provider_status="completed",
                    recording_sid="RE-active",
                )
            )

    assert premature.value.code == "VY-RES-010"
    assert twilio.calls == []


@pytest.mark.asyncio
async def test_recording_handler_catalogs_invalid_missing_and_failed_states(
    tmp_path: Path,
) -> None:
    async with SQLiteRepository(tmp_path / "calls.sqlite3") as repository:
        await _recording_call(repository, "CA-pending", terminal=False)
        handler = PipecatRecordingHandler(
            repository=repository,
            artifact_store=LocalArtifactStore(tmp_path / "artifacts"),
            access_base="https://voice.example.test",
            current_secret="whsec_dGVzdA==",  # pragma: allowlist secret
        )
        with pytest.raises(VoiceyError, match="invalid Twilio"):
            await handler.handle_twilio(
                CallEvent(
                    type="recording_ready",
                    provider_call_id="CA-pending",
                    provider_status="completed",
                )
            )
        with pytest.raises(VoiceyError, match="Twilio recording adapter"):
            await handler.handle_twilio(
                CallEvent(
                    type="recording_ready",
                    provider_call_id="CA-pending",
                    provider_status="completed",
                    recording_sid="RE-pending",
                )
            )
        with pytest.raises(VoiceyError, match="invalid Telnyx"):
            await handler.handle_telnyx(
                CallEvent(
                    type="recording_ready",
                    provider_call_id="CA-pending",
                    provider_status="saved",
                    recording_sid="recording-pending",
                )
            )
        with pytest.raises(VoiceyError, match="Telnyx recording adapter"):
            await handler.handle_telnyx(
                CallEvent(
                    type="recording_ready",
                    provider_call_id="CA-pending",
                    provider_status="saved",
                    recording_sid="recording-pending",
                    recording_url="https://storage.example.test/signed.mp3",
                )
            )
        with pytest.raises(VoiceyError, match="invalid Vobiz"):
            await handler.handle_vobiz(
                CallEvent(
                    type="recording_ready",
                    provider_call_id="CA-pending",
                    provider_status="saved",
                    recording_sid="recording-pending",
                )
            )
        with pytest.raises(VoiceyError, match="Vobiz recording adapter"):
            await handler.handle_vobiz(
                CallEvent(
                    type="recording_ready",
                    provider_call_id="CA-pending",
                    provider_status="saved",
                    recording_sid="recording-pending",
                    recording_url="https://storage.example.test/vobiz.mp3",
                )
            )
        with pytest.raises(VoiceyError, match="invalid Plivo"):
            await handler.handle_plivo(
                CallEvent(
                    type="recording_ready",
                    provider_call_id="CA-pending",
                    provider_status="saved",
                    recording_sid="recording-pending",
                )
            )
        with pytest.raises(VoiceyError, match="Plivo recording adapter"):
            await handler.handle_plivo(
                CallEvent(
                    type="recording_ready",
                    provider_call_id="CA-pending",
                    provider_status="saved",
                    recording_sid="recording-pending",
                    recording_url="https://storage.example.test/plivo.mp3",
                )
            )
        pending = await repository.get_recording_for_call("CA-pending")
        assert pending is not None
        with pytest.raises(VoiceyError) as unreadable:
            await handler.read(
                pending.recording_id,
                "Bearer whsec_dGVzdA==",  # pragma: allowlist secret
            )
        with pytest.raises(VoiceyError) as unknown:
            await repository.get_recording("rec_unknown")
        with pytest.raises(VoiceyError) as missing_failure:
            await repository.mark_recording_failed("call_unknown")

        await handler.handle_telnyx(
            CallEvent(
                type="recording_failed",
                provider_call_id="CA-pending",
                provider_status="failed",
            )
        )
        await handler.handle_telnyx(
            CallEvent(
                type="recording_failed",
                provider_call_id="CA-pending",
                provider_status="failed",
            )
        )
        await handler.handle_vobiz(
            CallEvent(
                type="recording_failed",
                provider_call_id="CA-pending",
                provider_status="failed",
            )
        )
        await handler.handle_plivo(
            CallEvent(
                type="recording_failed",
                provider_call_id="CA-pending",
                provider_status="failed",
            )
        )
        failed = await repository.get_recording_for_call("CA-pending")

    assert unreadable.value.code == "VY-RES-010"
    assert unknown.value.code == "VY-RES-010"
    assert missing_failure.value.code == "VY-RES-010"
    assert failed is not None
    assert failed.status == "failed"


def test_recording_handler_rejects_unsafe_access_configuration(tmp_path: Path) -> None:
    repository = SQLiteRepository(tmp_path / "calls.sqlite3")
    artifacts = LocalArtifactStore(tmp_path / "artifacts")
    with pytest.raises(VoiceyError, match="HTTPS"):
        PipecatRecordingHandler(
            repository=repository,
            artifact_store=artifacts,
            access_base="http://voice.example.test",
            current_secret="secret",  # pragma: allowlist secret
        )
    with pytest.raises(VoiceyError, match="webhook secret"):
        PipecatRecordingHandler(
            repository=repository,
            artifact_store=artifacts,
            access_base="https://voice.example.test",
            current_secret="",
        )
