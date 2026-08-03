import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, ClassVar, cast

import httpx
import pytest
from pipecat.runner.types import CallData
from starlette.datastructures import URL

from voicey import Agent, Behavior, Limits, Models, Phone, Results, Web, tool
from voicey.errors import VoiceyError
from voicey.playground.security import OriginPolicy, SessionTokenManager, WebSessionSecurity
from voicey.results.signing import encode_secret
from voicey.runtimes.pipecat import host as host_module
from voicey.runtimes.pipecat.host import (
    LongLivedRunner,
    PipecatHost,
    PipecatHostSettings,
    RecordingHandler,
    TelnyxRuntimeAdapter,
    TwilioRuntimeAdapter,
    WebSessionAuthorizer,
    telnyx_transport_params,
    twilio_transport_params,
)
from voicey.runtimes.pipecat.lifecycle import PipecatCall, PipecatCallLifecycle
from voicey.runtimes.pipecat.session import PipecatSessionBuilder
from voicey.storage.sqlite import SQLiteRepository
from voicey.telephony import CallEvent, PipecatTarget, TelephonyRequest
from voicey.telephony.ledger import WarmTransferRecord


@tool
def identify() -> str:
    """Return a stable test identity."""
    return "host-test"


def entry() -> dict[str, object]:
    return {
        "name": "entry",
        "task_messages": [{"role": "developer", "content": "Test."}],
        "respond_immediately": False,
    }


def _agent(
    *,
    max_concurrent: int = 2,
    voicemail: str = "hangup",
    provider: str = "twilio",
    record: bool = False,
) -> Agent:
    return Agent(
        name="host-test",
        runtime="pipecat",
        models=Models(
            stt="deepgram/nova-3",
            llm="anthropic/claude-sonnet-5",
            tts="cartesia/sonic-3.5",
        ),
        persona="Helpful.",
        flow=f"{__name__}:entry",
        tools=[identify],
        phone=Phone(
            provider=cast("Any", provider),
            number="+14155550123",
            record=record,
        ),
        web=Web(enabled=True, allowed_origins=["https://app.example"]),
        results=Results(
            webhook="https://receiver.example/results",
            secret_env="RESULT_SECRET",  # pragma: allowlist secret
        ),
        limits=Limits(
            max_duration_s=60,
            max_concurrent=max_concurrent,
            silence_hangup_s=10,
        ),
        behavior=Behavior(voicemail=cast("Any", voicemail)),
    )


class _Twilio:
    account_sid = "AC" + ("1" * 32)

    def __init__(self, *, verified: bool = True) -> None:
        self.verified = verified
        self.targets: list[PipecatTarget] = []
        self.amd_connect_machine: list[bool] = []
        self.amd_disposition = "hung_up"
        self.transfers: list[tuple[str, str]] = []
        self.recordings: list[tuple[str, PipecatTarget]] = []
        self.warm_accepts: list[TelephonyRequest] = []
        self.warm_events: list[TelephonyRequest] = []
        self.warm_conferences: list[TelephonyRequest] = []
        self.warm_recoveries = 0

    def verify_request(self, _request: TelephonyRequest) -> bool:
        return self.verified

    def answer_response(self, target: object) -> str:
        pipecat = cast(PipecatTarget, target)
        self.targets.append(pipecat)
        return "<Response><Connect><Stream /></Connect></Response>"

    def parse_event(self, request: TelephonyRequest) -> CallEvent:
        form = cast("Any", request.form)
        call_id = str(form.get("CallSid", "call_missing"))
        answered_by = form.get("AnsweredBy")
        recording_status = form.get("RecordingStatus")
        if recording_status:
            return CallEvent(
                type=("recording_ready" if recording_status == "completed" else "recording_failed"),
                provider_call_id=call_id,
                provider_status=str(recording_status),
                recording_sid=(
                    str(form.get("RecordingSid")) if recording_status == "completed" else None
                ),
            )
        if answered_by:
            return CallEvent(
                type="amd",
                provider_call_id=call_id,
                provider_status="amd",
                answered_by=str(answered_by),
            )
        return CallEvent(
            type="initiated",
            provider_call_id=call_id,
            provider_status=str(form.get("CallStatus", "ringing")),
        )

    def start_recording(self, call_sid: str, target: object) -> str:
        self.recordings.append((call_sid, cast("PipecatTarget", target)))
        return "RE" + ("2" * 32)

    def resume_after_amd(
        self,
        call_sid: str,
        *,
        answered_by: str,
        target: object,
        connect_machine: bool = False,
    ) -> str:
        del call_sid, answered_by, target
        self.amd_connect_machine.append(connect_machine)
        return self.amd_disposition

    def cold_transfer(self, call_sid: str, to_number: str) -> None:
        self.transfers.append((call_sid, to_number))

    def warm_transfer_accept_response(self, request: TelephonyRequest) -> str:
        self.warm_accepts.append(request)
        return "<Response><Dial><Conference>vk_test</Conference></Dial></Response>"

    def parse_warm_transfer_event(self, request: TelephonyRequest) -> object:
        self.warm_events.append(request)
        return object()

    def parse_warm_conference_event(self, request: TelephonyRequest) -> object:
        self.warm_conferences.append(request)
        return object()

    def recover_warm_transfers(self) -> int:
        self.warm_recoveries += 1
        return 0


class _Telnyx:
    def __init__(self, *, verified: bool = True) -> None:
        self.verified = verified
        self.answers: list[str] = []
        self.media: list[tuple[str, PipecatTarget]] = []
        self.transfers: list[tuple[str, str]] = []
        self.hangups: list[str] = []
        self.texml_targets: list[PipecatTarget] = []
        self.recordings: list[str] = []

    def verify_request(self, _request: TelephonyRequest) -> bool:
        return self.verified

    def answer_response(self, target: object) -> str:
        self.texml_targets.append(cast("PipecatTarget", target))
        return "<Response><Connect><Stream /></Connect></Response>"

    def parse_event(self, request: TelephonyRequest) -> CallEvent:
        if request.form is not None:
            form = cast("Any", request.form)
            return CallEvent(
                type="initiated",
                provider_call_id=str(form.get("CallControlId", "texml-call")),
                provider_status="initiated",
                direction="inbound",
                from_number="+14155550100",
                to_number="+14155550123",
            )
        document = json.loads(request.raw_body or "{}")
        data = cast("dict[str, Any]", document["data"])
        payload = cast("dict[str, Any]", data["payload"])
        event_type = str(data["event_type"])
        mapped = {
            "call.initiated": "initiated",
            "call.answered": "answered",
            "call.hangup": "completed",
            "call.recording.saved": "recording_ready",
            "call.recording.failed": "recording_failed",
        }[event_type]
        return CallEvent(
            type=cast("Any", mapped),
            provider_call_id=str(payload["call_control_id"]),
            provider_status=event_type,
            ended_reason="provider_hangup" if mapped == "completed" else None,
            direction="inbound" if payload.get("direction") == "incoming" else "outbound",
            from_number=str(payload.get("from", "")) or None,
            to_number=str(payload.get("to", "")) or None,
            recording_sid=(
                str(payload.get("recording_id")) if mapped == "recording_ready" else None
            ),
            recording_url=(
                str(cast("dict[str, object]", payload.get("recording_urls"))["mp3"])
                if mapped == "recording_ready"
                else None
            ),
        )

    def answer_call(self, call_control_id: str) -> None:
        self.answers.append(call_control_id)

    def start_media(self, call_control_id: str, target: object) -> None:
        self.media.append((call_control_id, cast("PipecatTarget", target)))

    def start_recording(self, call_control_id: str) -> None:
        self.recordings.append(call_control_id)

    def cold_transfer(self, call_control_id: str, to_number: str) -> None:
        self.transfers.append((call_control_id, to_number))

    def hangup(self, call_control_id: str) -> None:
        self.hangups.append(call_control_id)


class _RunnerSpy:
    def __init__(self) -> None:
        self.auto_end: bool | None = None
        self.ended = asyncio.Event()

    async def run(self, _worker: object = None, *, auto_end: bool = True) -> None:
        self.auto_end = auto_end
        await self.ended.wait()

    async def end(self, reason: str | None = None) -> None:
        del reason
        self.ended.set()


class _WarmAdapter:
    def __init__(self, *, bridge_fails: bool = False, accepts: bool = True) -> None:
        now = datetime(2026, 7, 28, tzinfo=UTC)
        self.record = WarmTransferRecord(
            transfer_id="warm_" + "a" * 32,
            provider="twilio",
            caller_call_id="CA" + "1" * 32,
            from_number="+14155550123",
            to_number="+14155550124",
            conference_name="vk_" + "b" * 32,
            briefing_digest="c" * 64,
            state="dialing",
            human_call_id="CA" + "2" * 32,
            conference_id=None,
            last_status="queued",
            created_at=now,
            updated_at=now,
        )
        self.bridge_fails = bridge_fails
        self.accepts = accepts
        self.events: list[str] = []

    def start_warm_transfer(self, **_arguments: object) -> WarmTransferRecord:
        self.events.append("start")
        return self.record

    def warm_transfer(self, _transfer_id: str) -> WarmTransferRecord:
        if self.accepts and self.record.state == "dialing":
            self.record = replace(self.record, state="accepted")
        return self.record

    def bridge_warm_transfer(self, _transfer_id: str) -> WarmTransferRecord:
        self.events.append("bridge")
        if self.bridge_fails:
            self.record = replace(self.record, state="failed")
            raise VoiceyError("VY-TEL-012", detail="definitive bridge failure.")
        self.record = replace(self.record, state="bridged")
        return self.record

    def abort_warm_transfer(self, _transfer_id: str, *, reason: str) -> WarmTransferRecord:
        self.events.append(f"abort:{reason}")
        self.record = replace(self.record, state="failed")
        return self.record


class _RecordingHandler:
    def __init__(self) -> None:
        self.twilio: list[CallEvent] = []
        self.telnyx: list[CallEvent] = []
        self.vobiz: list[CallEvent] = []
        self.plivo: list[CallEvent] = []

    async def handle_twilio(self, event: CallEvent) -> None:
        self.twilio.append(event)

    async def handle_telnyx(self, event: CallEvent) -> None:
        self.telnyx.append(event)

    async def handle_vobiz(self, event: CallEvent) -> None:
        self.vobiz.append(event)

    async def handle_plivo(self, event: CallEvent) -> None:
        self.plivo.append(event)

    async def read(self, recording_id: str, authorization: str | None) -> bytes:
        if authorization != "Bearer whsec-test":
            raise VoiceyError("VY-WEB-004", detail="denied")
        return f"audio:{recording_id}".encode()


class _Connection:
    pc_id = "pc_test"

    def __init__(self) -> None:
        self.handlers: dict[str, object] = {}
        self.disconnected = False

    def event_handler(self, name: str) -> Any:
        def decorator(function: object) -> object:
            self.handlers[name] = function
            return function

        return decorator

    async def disconnect(self) -> None:
        self.disconnected = True


class _WebSocket:
    url = URL("wss://voice.example/twilio/media")
    headers: ClassVar[dict[str, str]] = {}
    client = SimpleNamespace(host="127.0.0.1")

    def __init__(self) -> None:
        self.accepted = False
        self.closed: tuple[int, str] | None = None

    async def accept(self) -> None:
        self.accepted = True

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.closed = (code, reason)


class _RequestHandler:
    def __init__(self, *, answer: bool = True) -> None:
        self.connection = _Connection()
        self.answer = answer

    async def handle_web_request(
        self,
        request: object,
        callback: Any,
    ) -> dict[str, str] | None:
        del request
        if not self.answer:
            return None
        await callback(self.connection)
        return {"sdp": "answer", "type": "answer", "pc_id": self.connection.pc_id}

    async def handle_patch_request(self, _request: object) -> None:
        return None


class _WebSession:
    def __init__(self, call: object, lifecycle: PipecatCallLifecycle) -> None:
        self.call = call
        self.lifecycle = lifecycle
        self.started = False
        self.ended_reason: str | None = None

    async def start(self, _runner: object) -> None:
        self.started = True

    async def wait(self) -> object:
        return await self.lifecycle.finish("agent_hangup")

    async def end(self, reason: str) -> None:
        self.ended_reason = reason


class _SessionBuilder:
    def __init__(self) -> None:
        self.sessions: list[_WebSession] = []

    def build(
        self,
        *,
        agent: Agent,
        call: object,
        lifecycle: PipecatCallLifecycle,
        transport: object,
        sample_rate: int,
    ) -> _WebSession:
        del agent, transport, sample_rate
        session = _WebSession(call, lifecycle)
        self.sessions.append(session)
        return session


async def _host(
    tmp_path: Path,
    *,
    agent: Agent | None = None,
    twilio: _Twilio | None = None,
    request_handler: _RequestHandler | None = None,
    session_builder: _SessionBuilder | None = None,
    web_sessions: WebSessionAuthorizer | None = None,
    recording_handler: RecordingHandler | None = None,
) -> tuple[PipecatHost, SQLiteRepository, _Twilio]:
    repository = SQLiteRepository(tmp_path / "host.sqlite3")
    await repository.open()
    adapter = twilio or _Twilio()
    host = PipecatHost(
        agent=agent or _agent(),
        repository=repository,
        settings=PipecatHostSettings(
            public_base="https://voice.example",
            twilio_account_sid=adapter.account_sid,
            twilio_auth_token="secret",
            pending_media_timeout_s=60,
            allow_insecure_web_sessions_for_tests=web_sessions is None,
        ),
        twilio=cast(TwilioRuntimeAdapter, adapter),
        request_handler=cast(Any, request_handler) if request_handler else None,
        session_builder=(
            cast(PipecatSessionBuilder, session_builder) if session_builder is not None else None
        ),
        web_sessions=web_sessions,
        recording_handler=recording_handler,
    )
    return host, repository, adapter


async def _telnyx_host(
    tmp_path: Path,
    *,
    telnyx: _Telnyx | None = None,
    session_builder: _SessionBuilder | None = None,
    max_concurrent: int = 2,
    record: bool = False,
    recording_handler: RecordingHandler | None = None,
) -> tuple[PipecatHost, SQLiteRepository, _Telnyx]:
    repository = SQLiteRepository(tmp_path / "telnyx-host.sqlite3")
    await repository.open()
    adapter = telnyx or _Telnyx()
    host = PipecatHost(
        agent=_agent(
            provider="telnyx",
            max_concurrent=max_concurrent,
            record=record,
        ),
        repository=repository,
        settings=PipecatHostSettings(
            public_base="https://voice.example",
            twilio_account_sid="",
            twilio_auth_token="",
            telnyx_api_key="KEY-not-real",  # pragma: allowlist secret
            pending_media_timeout_s=60,
            allow_insecure_web_sessions_for_tests=True,
        ),
        telnyx=cast("TelnyxRuntimeAdapter", adapter),
        session_builder=(
            cast("PipecatSessionBuilder", session_builder) if session_builder is not None else None
        ),
        recording_handler=recording_handler,
    )
    return host, repository, adapter


async def test_long_lived_runner_uses_auto_end_false() -> None:
    runner = _RunnerSpy()
    host = LongLivedRunner(cast(Any, runner))

    await host.start()
    await asyncio.sleep(0)
    assert host.running
    assert runner.auto_end is False
    await host.stop()
    assert not host.running


async def test_drain_closes_admission_terminalizes_pending_and_changes_readiness(
    tmp_path: Path,
) -> None:
    host, repository, _adapter = await _host(tmp_path)
    pending = await host.reserve_call(
        PipecatCall(call_id="call-drain", channel="web", direction="inbound")
    )

    report = await host.drain(timeout_s=0.001)

    assert not host.accepting
    assert report.pending_at_start == 1
    assert report.forced_sessions == 1
    assert report.remaining_calls == 0
    terminal = await repository.get_terminal_event_for_call(pending.call.call_id)
    assert terminal.event_type == "call.completed"
    record = await repository.get_call(pending.call.call_id)
    assert record.terminal_reason == "duration_limit"
    with pytest.raises(VoiceyError) as rejected:
        await host.reserve_call(
            PipecatCall(call_id="call-after-drain", channel="web", direction="inbound")
        )
    assert rejected.value.code == "VY-RUN-008"

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=host.app)) as client:
        response = await client.get("http://test/health")
    assert response.status_code == 503
    assert response.json()["accepting"] is False
    await repository.close()


def test_twilio_transport_is_exactly_8khz_and_auto_hangs_up() -> None:
    params = twilio_transport_params(
        settings=PipecatHostSettings(
            public_base="https://voice.example",
            twilio_account_sid="AC" + ("1" * 32),
            twilio_auth_token="secret",
        ),
        call_id="CA-media",
        stream_id="MZ-media",
        max_duration_s=60,
    )
    serializer = cast(Any, params.serializer)

    assert params.audio_in_sample_rate == 8000
    assert params.audio_out_sample_rate == 8000
    assert params.session_timeout == 90
    assert params.allowed_origins == []
    assert serializer._params.twilio_sample_rate == 8000
    assert serializer._params.sample_rate == 8000
    assert serializer._params.auto_hang_up is True


def test_telnyx_transport_is_exactly_8khz_pcmu_and_auto_hangs_up() -> None:
    params = telnyx_transport_params(
        settings=PipecatHostSettings(
            public_base="https://voice.example",
            twilio_account_sid="",
            twilio_auth_token="",
            telnyx_api_key="KEY-not-real",  # pragma: allowlist secret
        ),
        call_id="v3:call-media",
        stream_id="stream-media",
        encoding="PCMU",
        max_duration_s=60,
    )
    serializer = cast("Any", params.serializer)

    assert params.audio_in_sample_rate == 8000
    assert params.audio_out_sample_rate == 8000
    assert params.session_timeout == 90
    assert params.allowed_origins == []
    assert serializer._params.telnyx_sample_rate == 8000
    assert serializer._params.sample_rate == 8000
    assert serializer._params.inbound_encoding == "PCMU"
    assert serializer._params.outbound_encoding == "PCMU"
    assert serializer._params.auto_hang_up is True

    with pytest.raises(VoiceyError, match="VY-TEL-010"):
        telnyx_transport_params(
            settings=PipecatHostSettings(
                public_base="https://voice.example",
                twilio_account_sid="",
                twilio_auth_token="",
                telnyx_api_key="KEY-not-real",  # pragma: allowlist secret
            ),
            call_id="v3:call-media",
            stream_id="stream-media",
            encoding="OPUS",
            max_duration_s=60,
        )


@pytest.mark.parametrize(
    "settings",
    [
        {
            "public_base": "http://voice.example",
            "twilio_account_sid": "",
            "twilio_auth_token": "",
        },
        {
            "public_base": "https://voice.example",
            "twilio_account_sid": "",
            "twilio_auth_token": "",
            "pending_media_timeout_s": 0,
        },
        {
            "public_base": "https://voice.example",
            "twilio_account_sid": "",
            "twilio_auth_token": "",
            "twilio_sample_rate": 16000,
        },
    ],
)
def test_host_settings_reject_unsafe_media_configuration(
    settings: dict[str, object],
) -> None:
    with pytest.raises(Exception, match="VY-RUN-002"):
        PipecatHostSettings(**cast(Any, settings))


def test_host_settings_load_credentials_from_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "AC-from-env")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "token-from-env")

    settings = PipecatHostSettings.from_env("https://voice.example")

    assert settings.twilio_account_sid == "AC-from-env"
    assert settings.twilio_auth_token == "token-from-env"


async def test_twilio_warm_coordinator_marks_before_bridge_and_clears_definitive_failure() -> None:
    reasons: list[str | None] = []
    adapter = _WarmAdapter()
    coordinator = host_module._TwilioWarmTransfer(  # pyright: ignore[reportPrivateUsage]
        cast(TwilioRuntimeAdapter, adapter),
        from_number="+14155550123",
        target=PipecatTarget(https_base="https://voice.example"),
        timeout_s=1,
    )

    await coordinator(
        "CA" + "1" * 32,
        "+14155550124",
        "Billing needs a private handoff.",
        reasons.append,
    )

    assert reasons == ["transferred"]
    assert adapter.events == ["start", "bridge"]
    assert adapter.record.state == "bridged"

    failed = _WarmAdapter(bridge_fails=True)
    failure = host_module._TwilioWarmTransfer(  # pyright: ignore[reportPrivateUsage]
        cast(TwilioRuntimeAdapter, failed),
        from_number="+14155550123",
        target=PipecatTarget(https_base="https://voice.example"),
        timeout_s=1,
    )
    with pytest.raises(VoiceyError, match="VY-TEL-012"):
        await failure(
            "CA" + "1" * 32,
            "+14155550124",
            "Billing needs a private handoff.",
            reasons.append,
        )
    assert reasons[-2:] == ["transferred", None]


async def test_twilio_warm_coordinator_timeout_aborts_only_human_leg() -> None:
    adapter = _WarmAdapter(accepts=False)
    coordinator = host_module._TwilioWarmTransfer(  # pyright: ignore[reportPrivateUsage]
        cast(TwilioRuntimeAdapter, adapter),
        from_number="+14155550123",
        target=PipecatTarget(https_base="https://voice.example"),
        timeout_s=0.001,
    )

    with pytest.raises(VoiceyError, match="timed out"):
        await coordinator(
            "CA" + "1" * 32,
            "+14155550124",
            "Billing needs a private handoff.",
            lambda _reason: None,
        )

    assert adapter.events == ["start", "abort:accept_timeout"]
    assert adapter.record.state == "failed"


async def test_twilio_answer_reserves_before_returning_stream(tmp_path: Path) -> None:
    host, repository, adapter = await _host(tmp_path)
    transport = httpx.ASGITransport(app=host.app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="https://voice.example",
    ) as client:
        response = await client.post(
            "/twilio/answer",
            data={
                "CallSid": "CA-first",
                "CallStatus": "ringing",
                "From": "+14155550100",
                "To": "+14155550123",
            },
        )

    record = await repository.get_call("CA-first")
    assert response.status_code == 200
    assert record.status == "active"
    assert host.admission.active_count == 1
    assert adapter.targets[0].custom_parameters["voicey_token"]
    assert adapter.targets[0].custom_parameters["from_number"] == "+14155550100"
    await host._finish_pending(  # pyright: ignore[reportPrivateUsage]
        "CA-first",
        "provider_hangup",
    )
    await repository.close()


async def test_pipecat_inbound_recording_starts_before_media_answer(
    tmp_path: Path,
) -> None:
    host, repository, adapter = await _host(
        tmp_path,
        agent=_agent(record=True),
    )
    transport = httpx.ASGITransport(app=host.app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="https://voice.example",
    ) as client:
        response = await client.post(
            "/twilio/answer",
            data={"CallSid": "CA-record", "CallStatus": "ringing"},
        )

    assert response.status_code == 200
    assert adapter.recordings[0][0] == "CA-record"
    assert adapter.recordings[0][1].recording_url == ("https://voice.example/twilio/recordings")
    recording = await repository.get_recording_for_call("CA-record")
    assert recording is not None
    assert recording.status == "pending"
    await host._finish_pending(  # pyright: ignore[reportPrivateUsage]
        "CA-record",
        "provider_hangup",
    )
    await repository.close()


async def test_twilio_recording_callback_and_artifact_route_are_runtime_wired(
    tmp_path: Path,
) -> None:
    recording_handler = _RecordingHandler()
    host, repository, _adapter = await _host(
        tmp_path,
        recording_handler=recording_handler,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=host.app),
        base_url="https://voice.example",
    ) as client:
        callback = await client.post(
            "/twilio/recordings",
            data={
                "CallSid": "CA-recording-callback",
                "RecordingStatus": "completed",
                "RecordingSid": "RE-recording-callback",
            },
        )
        denied = await client.get("/recordings/rec_engine")
        artifact = await client.get(
            "/recordings/rec_engine",
            headers={"authorization": "Bearer whsec-test"},
        )

    assert callback.status_code == 204
    assert len(recording_handler.twilio) == 1
    assert recording_handler.twilio[0].recording_sid == "RE-recording-callback"
    assert denied.status_code == 403
    assert artifact.status_code == 200
    assert artifact.content == b"audio:rec_engine"
    assert artifact.headers["cache-control"] == "private, no-store"
    await repository.close()


async def test_twilio_warm_transfer_callbacks_are_signed_and_route_bound(
    tmp_path: Path,
) -> None:
    host, repository, adapter = await _host(tmp_path)
    transfer_id = "warm_" + "a" * 32
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=host.app),
        base_url="https://voice.example",
    ) as client:
        accept = await client.post(
            f"/twilio/warm-transfer/{transfer_id}/accept",
            data={"CallSid": "CA-human", "Digits": "1"},
        )
        event = await client.post(
            f"/twilio/warm-transfer/{transfer_id}/events",
            data={"CallSid": "CA-human", "CallStatus": "ringing"},
        )
        conference = await client.post(
            f"/twilio/warm-transfer/{transfer_id}/conference",
            data={
                "ConferenceSid": "CF-conference",
                "StatusCallbackEvent": "participant-join",
            },
        )

    assert accept.status_code == 200
    assert accept.headers["content-type"].startswith("application/xml")
    assert event.status_code == 204
    assert conference.status_code == 204
    assert adapter.warm_accepts[0].route_params == {"transfer_id": transfer_id}
    assert adapter.warm_events[0].route_params == {"transfer_id": transfer_id}
    assert adapter.warm_conferences[0].route_params == {"transfer_id": transfer_id}
    await repository.close()

    denied, denied_repository, denied_adapter = await _host(
        tmp_path / "denied",
        twilio=_Twilio(verified=False),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=denied.app),
        base_url="https://voice.example",
    ) as client:
        responses = [
            await client.post(
                f"/twilio/warm-transfer/{transfer_id}/{action}",
                data={"CallSid": "CA-human"},
            )
            for action in ("accept", "events", "conference")
        ]
    assert {response.status_code for response in responses} == {403}
    assert denied_adapter.warm_accepts == []
    assert denied_adapter.warm_events == []
    assert denied_adapter.warm_conferences == []
    await denied_repository.close()


async def test_recording_routes_fail_closed_and_telnyx_dispatches(
    tmp_path: Path,
) -> None:
    no_handler, repository, _adapter = await _host(tmp_path / "missing")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=no_handler.app),
        base_url="https://voice.example",
    ) as client:
        missing_artifact = await client.get("/recordings/rec_missing")
        missing_ingestor = await client.post(
            "/twilio/recordings",
            data={
                "CallSid": "CA-missing",
                "RecordingStatus": "completed",
                "RecordingSid": "RE-missing",
            },
        )
        wrong_event = await client.post(
            "/twilio/recordings",
            data={"CallSid": "CA-wrong", "CallStatus": "ringing"},
        )
    assert missing_artifact.status_code == 404
    assert missing_ingestor.status_code == 503
    assert wrong_event.status_code == 400
    await repository.close()

    recording_handler = _RecordingHandler()
    telnyx_host, telnyx_repository, _telnyx = await _telnyx_host(
        tmp_path / "telnyx",
        recording_handler=recording_handler,
    )
    callback_body = {
        "data": {
            "event_type": "call.recording.saved",
            "payload": {
                "call_control_id": "v3:recording-callback",
                "recording_id": "recording-1",
                "recording_urls": {
                    "mp3": "https://storage.example.test/signed.mp3",
                },
            },
        }
    }
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=telnyx_host.app),
        base_url="https://voice.example",
    ) as client:
        telnyx_callback = await client.post(
            "/telnyx/recordings",
            content=json.dumps(callback_body),
            headers={"content-type": "application/json"},
        )

    assert telnyx_callback.status_code == 204
    assert len(recording_handler.telnyx) == 1
    assert recording_handler.telnyx[0].recording_sid == "recording-1"
    await telnyx_repository.close()


async def test_recording_callback_retry_and_signature_failures_are_visible(
    tmp_path: Path,
) -> None:
    class RetryHandler(_RecordingHandler):
        async def handle_twilio(self, event: CallEvent) -> None:
            del event
            raise VoiceyError("VY-RES-010", detail="terminal pending")

        async def handle_telnyx(self, event: CallEvent) -> None:
            del event
            raise VoiceyError("VY-RES-010", detail="terminal pending")

        async def handle_plivo(self, event: CallEvent) -> None:
            del event
            raise VoiceyError("VY-RES-010", detail="terminal pending")

    retry, repository, _adapter = await _host(
        tmp_path / "retry",
        recording_handler=RetryHandler(),
    )
    denied, denied_repository, _ = await _host(
        tmp_path / "denied",
        twilio=_Twilio(verified=False),
        recording_handler=_RecordingHandler(),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=retry.app),
        base_url="https://voice.example",
    ) as client:
        retry_response = await client.post(
            "/twilio/recordings",
            data={
                "CallSid": "CA-retry",
                "RecordingStatus": "completed",
                "RecordingSid": "RE-retry",
            },
        )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=denied.app),
        base_url="https://voice.example",
    ) as client:
        denied_response = await client.post(
            "/twilio/recordings",
            data={
                "CallSid": "CA-denied",
                "RecordingStatus": "completed",
                "RecordingSid": "RE-denied",
            },
        )

    assert retry_response.status_code == 503
    assert denied_response.status_code == 403
    await repository.close()
    await denied_repository.close()


async def test_twilio_answer_is_idempotent_for_retries(tmp_path: Path) -> None:
    host, repository, adapter = await _host(tmp_path)
    transport = httpx.ASGITransport(app=host.app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="https://voice.example",
    ) as client:
        first = await client.post(
            "/twilio/answer",
            data={"CallSid": "CA-retry", "CallStatus": "ringing"},
        )
        second = await client.post(
            "/twilio/answer",
            data={"CallSid": "CA-retry", "CallStatus": "ringing"},
        )

    assert first.status_code == second.status_code == 200
    assert host.admission.active_count == 1
    assert (
        adapter.targets[0].custom_parameters["voicey_token"]
        == adapter.targets[1].custom_parameters["voicey_token"]
    )
    await host._finish_pending("CA-retry", "provider_hangup")  # pyright: ignore[reportPrivateUsage]
    await repository.close()


async def test_twilio_answer_returns_native_busy_at_limit(tmp_path: Path) -> None:
    host, repository, _ = await _host(tmp_path, agent=_agent(max_concurrent=1))
    transport = httpx.ASGITransport(app=host.app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="https://voice.example",
    ) as client:
        first = await client.post(
            "/twilio/answer",
            data={"CallSid": "CA-one", "CallStatus": "ringing"},
        )
        second = await client.post(
            "/twilio/answer",
            data={"CallSid": "CA-two", "CallStatus": "ringing"},
        )

    assert first.status_code == 200
    assert second.status_code == 200
    assert '<Reject reason="busy"' in second.text
    with pytest.raises(Exception, match="VY-OBS-003"):
        await repository.get_call("CA-two")
    await host._finish_pending(  # pyright: ignore[reportPrivateUsage]
        "CA-one",
        "provider_hangup",
    )
    await repository.close()


async def test_unauthenticated_twilio_answer_is_rejected(tmp_path: Path) -> None:
    host, repository, _ = await _host(tmp_path, twilio=_Twilio(verified=False))
    transport = httpx.ASGITransport(app=host.app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="https://voice.example",
    ) as client:
        response = await client.post(
            "/twilio/answer",
            data={"CallSid": "CA-bad", "CallStatus": "ringing"},
        )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "VY-RUN-007"
    assert host.admission.active_count == 0
    await repository.close()


async def test_signed_twilio_terminal_event_finishes_pending_call(tmp_path: Path) -> None:
    class _TerminalTwilio(_Twilio):
        def parse_event(self, request: TelephonyRequest) -> CallEvent:
            form = cast("Any", request.form)
            return CallEvent(
                type="completed",
                provider_call_id=str(form["CallSid"]),
                provider_status="completed",
                ended_reason="provider_hangup",
            )

    host, repository, _ = await _host(tmp_path, twilio=_TerminalTwilio())
    transport = httpx.ASGITransport(app=host.app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="https://voice.example",
    ) as client:
        await client.post(
            "/twilio/answer",
            data={"CallSid": "CA-terminal", "CallStatus": "ringing"},
        )
        response = await client.post(
            "/twilio/events/intent-test",
            data={"CallSid": "CA-terminal", "CallStatus": "completed"},
        )

    record = await repository.get_call("CA-terminal")
    assert response.status_code == 204
    assert record.status == "completed"
    assert record.terminal_reason == "provider_hangup"
    assert host.admission.active_count == 0
    await repository.close()


async def test_twilio_media_route_claims_reservation_and_runs_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_builder = _SessionBuilder()
    host, repository, _ = await _host(tmp_path, session_builder=session_builder)
    pending = await host.reserve_call(
        PipecatCall(
            call_id="CA-media-route",
            channel="phone",
            direction="inbound",
            provider="twilio",
        )
    )

    async def parse(_websocket: object) -> tuple[str, CallData]:
        return (
            "twilio",
            CallData(
                call_id="CA-media-route",
                stream_id="MZ-media-route",
                body={"voicey_token": pending.admission.token},
            ),
        )

    monkeypatch.setattr(host_module, "parse_telephony_websocket", parse)
    websocket = _WebSocket()
    route = next(
        route for route in host.app.routes if getattr(route, "path", None) == "/twilio/media"
    )
    await cast(Any, route).endpoint(websocket)

    record = await repository.get_call("CA-media-route")
    assert websocket.accepted
    assert websocket.closed is None
    assert session_builder.sessions[0].started
    assert record.status == "completed"
    assert host.admission.active_count == 0
    await repository.close()


async def test_twilio_media_rejects_unauthenticated_socket(tmp_path: Path) -> None:
    host, repository, _ = await _host(tmp_path, twilio=_Twilio(verified=False))
    websocket = _WebSocket()
    route = next(
        route for route in host.app.routes if getattr(route, "path", None) == "/twilio/media"
    )
    await cast(Any, route).endpoint(websocket)

    assert not websocket.accepted
    assert websocket.closed == (1008, "VY-RUN-007")
    await repository.close()


async def test_twilio_media_rejects_wrong_transport(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host, repository, _ = await _host(tmp_path)

    async def parse(_websocket: object) -> tuple[str, CallData]:
        return "telnyx", CallData(call_id="call-wrong", stream_id="stream-wrong")

    monkeypatch.setattr(host_module, "parse_telephony_websocket", parse)
    websocket = _WebSocket()
    route = next(
        route for route in host.app.routes if getattr(route, "path", None) == "/twilio/media"
    )
    await cast(Any, route).endpoint(websocket)

    assert websocket.accepted
    assert websocket.closed == (1011, "VY-RUN-006")
    await repository.close()


async def test_telnyx_json_events_reserve_answer_and_start_capability_media(
    tmp_path: Path,
) -> None:
    host, repository, adapter = await _telnyx_host(tmp_path, record=True)
    transport = httpx.ASGITransport(app=host.app)
    initiated = {
        "data": {
            "event_type": "call.initiated",
            "payload": {
                "call_control_id": "v3:telnyx-route",
                "direction": "incoming",
                "from": "+14155550100",
                "to": "+14155550123",
            },
        }
    }
    answered = {
        "data": {
            "event_type": "call.answered",
            "payload": {
                "call_control_id": "v3:telnyx-route",
                "direction": "incoming",
                "from": "+14155550100",
                "to": "+14155550123",
            },
        }
    }
    async with httpx.AsyncClient(
        transport=transport,
        base_url="https://voice.example",
    ) as client:
        first = await client.post("/telnyx/events", json=initiated)
        second = await client.post("/telnyx/events", json=answered)

    assert first.status_code == 204
    assert second.status_code == 204
    assert adapter.answers == ["v3:telnyx-route"]
    assert adapter.recordings == ["v3:telnyx-route"]
    assert len(adapter.media) == 1
    call_id, target = adapter.media[0]
    assert call_id == "v3:telnyx-route"
    assert target.ws_path.startswith("/telnyx/media/")
    token = target.ws_path.rsplit("/", maxsplit=1)[-1]
    assert token
    record = await repository.get_call("v3:telnyx-route")
    assert record.direction == "inbound"
    assert record.from_number == "+14155550100"
    await host._finish_pending(  # pyright: ignore[reportPrivateUsage]
        "v3:telnyx-route",
        "provider_hangup",
    )
    await repository.close()


async def test_telnyx_media_claims_one_use_path_and_runs_native_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_builder = _SessionBuilder()
    host, repository, _ = await _telnyx_host(
        tmp_path,
        session_builder=session_builder,
    )
    pending = await host.reserve_call(
        PipecatCall(
            call_id="v3:telnyx-media",
            channel="phone",
            direction="inbound",
            provider="telnyx",
            provider_call_id="v3:telnyx-media",
        )
    )

    async def parse(_websocket: object) -> tuple[str, CallData]:
        return (
            "telnyx",
            CallData(
                call_id="v3:telnyx-media",
                stream_id="stream-telnyx-media",
                body={"outbound_encoding": "PCMU"},
            ),
        )

    monkeypatch.setattr(host_module, "parse_telephony_websocket", parse)
    websocket = _WebSocket()
    route = next(
        route
        for route in host.app.routes
        if getattr(route, "path", None) == "/telnyx/media/{token}"
    )
    await cast("Any", route).endpoint(websocket, pending.admission.token)

    record = await repository.get_call("v3:telnyx-media")
    assert websocket.accepted
    assert websocket.closed is None
    assert session_builder.sessions[0].started
    assert record.status == "completed"
    assert host.admission.active_count == 0
    await repository.close()


async def test_telnyx_media_rejects_wrong_token_transport_and_codec(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host, repository, _ = await _telnyx_host(tmp_path)
    pending = await host.reserve_call(
        PipecatCall(
            call_id="v3:telnyx-reject",
            channel="phone",
            direction="inbound",
            provider="telnyx",
        )
    )
    route = next(
        route
        for route in host.app.routes
        if getattr(route, "path", None) == "/telnyx/media/{token}"
    )

    async def wrong_transport(_websocket: object) -> tuple[str, CallData]:
        return "twilio", CallData(call_id="v3:telnyx-reject", stream_id="stream")

    monkeypatch.setattr(host_module, "parse_telephony_websocket", wrong_transport)
    wrong = _WebSocket()
    await cast("Any", route).endpoint(wrong, pending.admission.token)
    assert wrong.closed == (1011, "VY-RUN-006")

    async def wrong_codec(_websocket: object) -> tuple[str, CallData]:
        return (
            "telnyx",
            CallData(
                call_id="v3:telnyx-reject",
                stream_id="stream",
                body={"outbound_encoding": "OPUS"},
            ),
        )

    monkeypatch.setattr(host_module, "parse_telephony_websocket", wrong_codec)
    codec = _WebSocket()
    await cast("Any", route).endpoint(codec, pending.admission.token)
    assert codec.closed == (1011, "VY-RUN-006")

    await host._finish_pending(  # pyright: ignore[reportPrivateUsage]
        "v3:telnyx-reject",
        "carrier_error",
    )
    await repository.close()


async def test_telnyx_signed_routes_reject_invalid_and_busy_calls(
    tmp_path: Path,
) -> None:
    adapter = _Telnyx(verified=False)
    host, repository, _ = await _telnyx_host(
        tmp_path,
        telnyx=adapter,
        max_concurrent=1,
    )
    body = {
        "data": {
            "event_type": "call.initiated",
            "payload": {
                "call_control_id": "v3:invalid",
                "direction": "incoming",
            },
        }
    }
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=host.app),
        base_url="https://voice.example",
    ) as client:
        rejected = await client.post("/telnyx/events", json=body)
    assert rejected.status_code == 403

    adapter.verified = True
    await host.reserve_web_call()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=host.app),
        base_url="https://voice.example",
    ) as client:
        busy = await client.post("/telnyx/events", json=body)
    assert busy.status_code == 204
    assert adapter.hangups == ["v3:invalid"]
    await host._finish_pending(  # pyright: ignore[reportPrivateUsage]
        next(iter(host._pending)),  # pyright: ignore[reportPrivateUsage]
        "provider_hangup",
    )
    await repository.close()


async def test_telnyx_texml_answer_uses_one_use_media_path(
    tmp_path: Path,
) -> None:
    host, repository, adapter = await _telnyx_host(tmp_path)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=host.app),
        base_url="https://voice.example",
    ) as client:
        response = await client.post(
            "/telnyx/answer",
            data={
                "CallControlId": "v3:texml-call",
                "CallStatus": "ringing",
                "From": "+14155550100",
                "To": "+14155550123",
            },
        )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/xml")
    assert len(adapter.texml_targets) == 1
    target = adapter.texml_targets[0]
    assert target.ws_path.startswith("/telnyx/media/")
    assert target.custom_parameters["voicey_token"] == target.ws_path.rsplit("/", 1)[-1]
    await host._finish_pending(  # pyright: ignore[reportPrivateUsage]
        "v3:texml-call",
        "provider_hangup",
    )
    await repository.close()


@pytest.mark.parametrize(
    ("voicemail", "connect_machine"),
    [("hangup", False), ("leave_message", True)],
)
async def test_voicemail_policy_controls_amd_disposition(
    tmp_path: Path,
    voicemail: str,
    connect_machine: bool,
) -> None:
    adapter = _Twilio()
    adapter.amd_disposition = "hung_up" if voicemail == "hangup" else "connected"
    host, repository, _ = await _host(
        tmp_path,
        agent=_agent(voicemail=voicemail),
        twilio=adapter,
    )
    transport = httpx.ASGITransport(app=host.app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="https://voice.example",
    ) as client:
        await client.post(
            "/twilio/answer",
            data={"CallSid": "CA-amd", "CallStatus": "ringing"},
        )
        response = await client.post(
            "/twilio/amd",
            data={"CallSid": "CA-amd", "AnsweredBy": "machine_start"},
        )

    assert response.status_code == 204
    assert adapter.amd_connect_machine == [connect_machine]
    if voicemail == "hangup":
        record = await repository.get_call("CA-amd")
        assert record.terminal_reason == "voicemail"
    else:
        await host._finish_pending(  # pyright: ignore[reportPrivateUsage]
            "CA-amd",
            "voicemail",
        )
    await repository.close()


async def test_post_offer_starts_small_webrtc_session(tmp_path: Path) -> None:
    request_handler = _RequestHandler()
    session_builder = _SessionBuilder()
    host, repository, _ = await _host(
        tmp_path,
        request_handler=request_handler,
        session_builder=session_builder,
    )
    transport = httpx.ASGITransport(app=host.app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="https://voice.example",
    ) as client:
        response = await client.post(
            "/api/offer",
            json={"sdp": "offer", "type": "offer"},
        )
        for _ in range(100):
            if host.admission.active_count == 0:
                break
            await asyncio.sleep(0.01)

    assert response.status_code == 200
    assert response.json() == {
        "sdp": "answer",
        "type": "answer",
        "pc_id": "pc_test",
    }
    assert len(session_builder.sessions) == 1
    assert session_builder.sessions[0].started
    assert host.admission.active_count == 0
    await repository.close()


async def test_web_ice_patch_accepts_browser_candidate_shapes(tmp_path: Path) -> None:
    request_handler = _RequestHandler()
    host, repository, _ = await _host(tmp_path, request_handler=request_handler)
    transport = httpx.ASGITransport(app=host.app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="https://voice.example",
    ) as client:
        response = await client.patch(
            "/api/offer",
            json={
                "pcId": "pc_test",
                "candidates": [
                    {
                        "candidate": "candidate:1 1 udp 1 127.0.0.1 5000 typ host",
                        "sdpMid": "0",
                        "sdpMLineIndex": 0,
                    }
                ],
            },
        )
        invalid = await client.patch("/api/offer", json={})

    assert response.status_code == 204
    assert invalid.status_code == 400
    assert invalid.json()["error"]["code"] == "VY-RUN-007"
    await repository.close()


async def test_invalid_web_offer_is_cataloged(tmp_path: Path) -> None:
    host, repository, _ = await _host(tmp_path)
    transport = httpx.ASGITransport(app=host.app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="https://voice.example",
    ) as client:
        response = await client.post("/api/offer", json={"type": "not-an-offer"})

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "VY-RUN-007"
    await repository.close()


async def test_web_signaling_requires_scoped_token_origin_and_peer_binding(
    tmp_path: Path,
) -> None:
    tokens = SessionTokenManager(
        encode_secret(b"w" * 32),
        audience="https://voice.example",
        agent_name="host-test",
    )
    security = WebSessionSecurity(
        tokens,
        OriginPolicy(
            allowed_origins=frozenset({"https://app.example"}),
            expected_public_origin="https://voice.example",
        ),
    )
    request_handler = _RequestHandler()
    host, repository, _ = await _host(
        tmp_path,
        request_handler=request_handler,
        session_builder=_SessionBuilder(),
        web_sessions=security,
    )
    transport = httpx.ASGITransport(app=host.app)
    issued = await tokens.issue_for_call(
        client_key="browser",
        reserve_call=host.reserve_web_call,
    )
    authorization = {"authorization": f"Bearer {issued.token}"}
    async with httpx.AsyncClient(
        transport=transport,
        base_url="https://voice.example",
    ) as client:
        missing = await client.post(
            "/api/offer",
            headers={"origin": "https://app.example"},
            json={"sdp": "offer", "type": "offer"},
        )
        wrong_origin = await client.post(
            "/api/offer",
            headers={**authorization, "origin": "https://attacker.example"},
            json={"sdp": "offer", "type": "offer"},
        )
        connected = await client.post(
            "/api/offer",
            headers={**authorization, "origin": "https://app.example"},
            json={"sdp": "offer", "type": "offer"},
        )
        replay = await client.post(
            "/api/offer",
            headers={**authorization, "origin": "https://app.example"},
            json={"sdp": "offer", "type": "offer"},
        )
        admin_records = await client.get("/api/admin/calls")
        token_mint = await client.post("/api/playground/sessions")

    assert missing.status_code == 401
    assert wrong_origin.status_code == 403
    assert connected.status_code == 200
    assert replay.status_code == 401
    assert admin_records.status_code == 404
    assert token_mint.status_code == 404
    snapshot = await tokens.snapshot(issued.identity.session_id)
    assert snapshot.call_id is not None
    assert snapshot.call_id == issued.identity.call_id
    assert snapshot.pc_id == "pc_test"
    await repository.close()


async def test_failed_authenticated_offer_consumes_token_and_terminalizes_reservation(
    tmp_path: Path,
) -> None:
    tokens = SessionTokenManager(
        encode_secret(b"f" * 32),
        audience="https://voice.example",
        agent_name="host-test",
    )
    security = WebSessionSecurity(
        tokens,
        OriginPolicy(
            allowed_origins=frozenset({"https://app.example"}),
            expected_public_origin="https://voice.example",
        ),
    )
    host, repository, _ = await _host(
        tmp_path,
        request_handler=_RequestHandler(answer=False),
        web_sessions=security,
    )
    issued = await tokens.issue_for_call(
        client_key="browser",
        reserve_call=host.reserve_web_call,
    )
    assert issued.identity.call_id is not None
    before = await repository.get_call(issued.identity.call_id)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=host.app),
        base_url="https://voice.example",
    ) as client:
        failed = await client.post(
            "/api/offer",
            headers={
                "authorization": f"Bearer {issued.token}",
                "origin": "https://app.example",
            },
            json={"sdp": "offer", "type": "offer"},
        )
        replay = await client.post(
            "/api/offer",
            headers={
                "authorization": f"Bearer {issued.token}",
                "origin": "https://app.example",
            },
            json={"sdp": "offer", "type": "offer"},
        )

    after = await repository.get_call(issued.identity.call_id)
    terminal = await repository.get_terminal_event_for_call(issued.identity.call_id)
    assert before.status == "active"
    assert failed.status_code == 400
    assert failed.json()["error"]["code"] == "VY-RUN-007"
    assert replay.status_code == 401
    assert after.status == "failed"
    assert terminal.event_type == "call.failed"
    await repository.close()


async def test_host_refuses_ungated_web_channel_by_default(tmp_path: Path) -> None:
    repository = SQLiteRepository(tmp_path / "host.sqlite3")
    await repository.open()
    adapter = _Twilio()
    with pytest.raises(Exception, match="VY-WEB-001"):
        PipecatHost(
            agent=_agent(),
            repository=repository,
            settings=PipecatHostSettings(
                public_base="https://voice.example",
                twilio_account_sid=adapter.account_sid,
                twilio_auth_token="secret",
            ),
            twilio=cast(TwilioRuntimeAdapter, adapter),
        )
    await repository.close()


async def test_host_reload_is_fenced_at_call_boundary(tmp_path: Path) -> None:
    host, repository, _ = await _host(tmp_path)
    await host.reserve_call(
        PipecatCall(
            call_id="call_reload",
            channel="web",
            direction="inbound",
        )
    )

    updated = _agent(max_concurrent=4)
    assert not await host.reload_agent(updated, restart_runner=True)
    assert host.agent.limits.max_concurrent == 2

    await host._finish_pending(  # pyright: ignore[reportPrivateUsage]
        "call_reload",
        "agent_hangup",
    )
    assert await host.reload_agent(updated, restart_runner=True)
    assert host.agent.limits.max_concurrent == 4
    assert host.admission.max_concurrent == 4

    renamed = updated.model_copy(update={"name": "renamed"})
    with pytest.raises(VoiceyError, match="require restarting"):
        await host.reload_agent(renamed, restart_runner=False)
    await repository.close()
