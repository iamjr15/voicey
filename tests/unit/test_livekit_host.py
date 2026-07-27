from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from livekit import rtc

from voicekit import Agent, Models, Results, Web
from voicekit.errors import VoicekitError
from voicekit.runtimes.livekit.host import (
    JobCallControl,
    LiveKitAdmissionGate,
    LiveKitHost,
    LiveKitHostSettings,
    _call_from_context,  # pyright: ignore[reportPrivateUsage]
    _close_repository,  # pyright: ignore[reportPrivateUsage]
    _metadata,  # pyright: ignore[reportPrivateUsage]
    _prewarm_process,  # pyright: ignore[reportPrivateUsage]
    _twilio_call_sid,  # pyright: ignore[reportPrivateUsage]
)
from voicekit.storage.sqlite import SQLiteRepository


def _agent() -> Agent:
    return Agent(
        name="livekit-host-test",
        runtime="livekit",
        models=Models(
            stt="deepgram/nova-3",
            llm="anthropic/claude-sonnet-5",
            tts="cartesia/sonic-3.5",
        ),
        persona="Test the host.",
        flow="flow:entry",
        tools="tools",
        web=Web(enabled=True, allowed_origins=["http://localhost:5173"]),
        results=Results(
            webhook="https://receiver.example.test/results",
            secret_env="VOICEKIT_WEBHOOK_SECRET",  # pragma: allowlist secret
        ),
    )


class FakeAgentServer:
    def __init__(self) -> None:
        self.registration: dict[str, object] = {}
        self.runs: list[bool] = []
        self.drains: list[float] = []
        self.closed = False

    def rtc_session(self, entrypoint: object, **values: object) -> None:
        self.registration = {"entrypoint": entrypoint, **values}

    async def run(self, *, devmode: bool) -> None:
        self.runs.append(devmode)

    async def drain(self, *, timeout: float) -> None:  # noqa: ASYNC109
        self.drains.append(timeout)

    async def aclose(self) -> None:
        self.closed = True


class FakeJobRequest:
    def __init__(self, job_id: str, metadata: str) -> None:
        self.id = job_id
        self.job = SimpleNamespace(metadata=metadata)
        self.accepted: dict[str, object] | None = None
        self.rejected = False

    async def accept(self, **values: object) -> None:
        self.accepted = values

    async def reject(self, *, terminate: bool) -> None:
        self.rejected = terminate


class FakeLocalParticipant:
    def __init__(self) -> None:
        self.dtmf: list[tuple[int, str]] = []

    async def publish_dtmf(self, *, code: int, digit: str) -> None:
        self.dtmf.append((code, digit))


class FakeRoom:
    def __init__(self) -> None:
        self.local_participant = FakeLocalParticipant()
        self.listeners: dict[str, object] = {}

    def on(self, event: str, callback: object) -> None:
        self.listeners[event] = callback


class FakeJobContext:
    def __init__(
        self,
        *,
        metadata: str,
        kind: rtc.ParticipantKind.ValueType = rtc.ParticipantKind.PARTICIPANT_KIND_SIP,
    ) -> None:
        self.room = FakeRoom()
        self.job = SimpleNamespace(
            id="job-1",
            metadata=metadata,
            participant=SimpleNamespace(
                identity="sip-caller",
                kind=kind,
                attributes={"sip.twilio.callSid": f"CA{'a' * 32}"},
            ),
        )
        self.proc = SimpleNamespace(userdata={})
        self.transfers: list[tuple[str, str, bool]] = []

    async def transfer_sip_participant(
        self,
        identity: str,
        destination: str,
        *,
        play_dialtone: bool,
    ) -> None:
        self.transfers.append((identity, destination, play_dialtone))

    def make_session_report(self, _session: object) -> dict[str, bool]:
        return {"ok": True}


@pytest.mark.parametrize(
    "settings",
    [
        {"num_idle_processes": -1},
        {"drain_timeout_s": 0},
        {"session_end_timeout_s": 0},
        {"health_port": 0},
        {"browser_reservation_ttl_s": 29},
    ],
)
def test_livekit_host_settings_reject_invalid_values(
    settings: dict[str, object],
) -> None:
    with pytest.raises(VoicekitError) as caught:
        LiveKitHostSettings(**settings)  # type: ignore[arg-type]
    assert caught.value.code == "VK-RUN-002"


def test_livekit_host_and_gate_reject_wrong_runtime_or_capacity() -> None:
    with pytest.raises(VoicekitError, match="capacity"):
        LiveKitAdmissionGate(0)

    async def repository_factory() -> Any:
        raise AssertionError("no job should run")

    with pytest.raises(VoicekitError, match="requires runtime"):
        LiveKitHost(
            agent=_agent().model_copy(update={"runtime": "pipecat"}),
            repository_factory=repository_factory,
            server=cast(Any, FakeAgentServer()),
        )


@pytest.mark.asyncio
async def test_livekit_admission_request_accept_reject_release_and_run() -> None:
    server = FakeAgentServer()

    async def repository_factory() -> Any:
        raise AssertionError("no job should run")

    host = LiveKitHost(
        agent=_agent(),
        repository_factory=repository_factory,
        server=cast(Any, server),
    )
    host.gate = LiveKitAdmissionGate(1)
    assert server.registration["agent_name"] == _agent().name

    await host.gate.reserve("call-browser")
    with pytest.raises(VoicekitError, match="duplicate call id"):
        await host.gate.reserve("call-browser")
    accepted = FakeJobRequest(
        "job-browser",
        json.dumps({"call_id": "call-browser"}),
    )
    await host.on_request(cast(Any, accepted))
    assert accepted.accepted is not None
    assert json.loads(str(accepted.accepted["metadata"])) == {"call_id": "call-browser"}

    rejected = FakeJobRequest("job-rejected", "{}")
    await host.on_request(cast(Any, rejected))
    assert rejected.rejected is True
    await host.on_session_end(cast(Any, SimpleNamespace(job=SimpleNamespace(id="job-browser"))))
    assert host.gate.occupied == 0

    await host.run(devmode=True)
    assert server.runs == [True]


@pytest.mark.asyncio
async def test_livekit_reload_drain_prewarm_and_optional_repository_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = FakeAgentServer()

    async def repository_factory() -> Any:
        raise AssertionError("no job should run")

    host = LiveKitHost(
        agent=_agent(),
        repository_factory=repository_factory,
        server=cast(Any, server),
    )
    await host.gate.reserve("occupied")
    assert not await host.reload_agent(_agent(), restart_runner=True)
    await host.gate.release("occupied")
    assert await host.reload_agent(_agent(), restart_runner=False)
    with pytest.raises(VoicekitError, match="registered agent name"):
        await host.reload_agent(
            _agent().model_copy(update={"name": "different"}),
            restart_runner=False,
        )

    await host.drain()
    assert server.drains == [3600]
    assert server.closed

    monkeypatch.setattr(
        "voicekit.runtimes.livekit.host.silero.VAD.load",
        lambda: "prewarmed-vad",
    )
    process = SimpleNamespace(userdata={})
    _prewarm_process(cast(Any, process))
    assert process.userdata["voicekit_vad"] == "prewarmed-vad"

    closed: list[str] = []

    class AsyncClose:
        async def close(self) -> None:
            closed.append("async")

    class SyncClose:
        def close(self) -> None:
            closed.append("sync")

    await _close_repository(cast(Any, object()))
    await _close_repository(cast(Any, AsyncClose()))
    await _close_repository(cast(Any, SyncClose()))
    assert closed == ["async", "sync"]


@pytest.mark.asyncio
async def test_livekit_browser_reservation_is_durable_before_token_and_fails_cleanly(
    tmp_path: Path,
) -> None:
    database = tmp_path / "calls.sqlite3"

    async def repository_factory() -> SQLiteRepository:
        return await SQLiteRepository(database).open()

    host = LiveKitHost(
        agent=_agent(),
        repository_factory=repository_factory,
        server=cast(Any, FakeAgentServer()),
    )
    call_id = await host.reserve_web_call()
    async with SQLiteRepository(database) as reader:
        reserved = await reader.get_call(call_id)
    assert reserved.runtime == "livekit"
    assert reserved.channel == "web"
    assert reserved.status == "active"
    assert reserved.timeline[-1].event_type == "runtime.reserved"

    await host.fail_web_reservation(call_id)
    async with SQLiteRepository(database) as reader:
        failed = await reader.get_call(call_id)
        terminal = await reader.get_terminal_event_for_call(call_id)
    assert failed.status == "failed"
    assert failed.terminal_reason == "setup_error"
    assert terminal.event_type == "call.failed"
    assert host.gate.occupied == 0


@pytest.mark.asyncio
async def test_job_call_control_uses_native_transfer_and_dtmf(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = FakeJobContext(metadata="{}")
    control = JobCallControl(cast(Any, context), participant_identity="sip-caller")

    async def no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr("voicekit.runtimes.livekit.host.asyncio.sleep", no_sleep)
    await control.cold_transfer("+14155550101")
    await control.send_dtmf("1#A")
    assert context.transfers == [
        ("sip-caller", "tel:+14155550101", True),
    ]
    assert context.room.local_participant.dtmf == [(1, "1"), (11, "#"), (12, "A")]

    with pytest.raises(VoicekitError) as caught:
        await control.send_dtmf("X")
    assert caught.value.code == "VK-TEL-002"


def test_livekit_metadata_and_job_mapping_are_strict() -> None:
    assert _metadata("") == {}
    assert _metadata('{"call_id":"call-1"}') == {"call_id": "call-1"}
    for raw in ("[]", '{"call_id":1}', "{"):
        with pytest.raises(VoicekitError) as caught:
            _metadata(raw)
        assert caught.value.code == "VK-RUN-007"

    phone = FakeJobContext(
        metadata=json.dumps(
            {
                "call_id": "call-phone",
                "channel": "phone",
                "direction": "outbound",
                "provider": "twilio",
                "provider_call_id": "sip-1",
                "from_number": "+14155550100",
                "to_number": "+14155550101",
            }
        )
    )
    call = _call_from_context(cast(Any, phone))
    assert call.call_id == "call-phone"
    assert call.channel == "phone"
    assert call.direction == "outbound"
    assert call.provider_call_id == "sip-1"

    web = FakeJobContext(
        metadata="{}",
        kind=rtc.ParticipantKind.PARTICIPANT_KIND_STANDARD,
    )
    assert _call_from_context(cast(Any, web)).channel == "web"
    invalid = FakeJobContext(metadata='{"channel":"video"}')
    with pytest.raises(VoicekitError):
        _call_from_context(cast(Any, invalid))

    web.job.participant.attributes = None
    assert _twilio_call_sid(cast(Any, web)) is None
    web.job.participant.attributes = {"sip.twilio.callSid": "not-a-call-sid"}
    assert _twilio_call_sid(cast(Any, web)) is None


@pytest.mark.asyncio
async def test_livekit_host_entrypoint_persists_and_terminalizes_job(
    tmp_path: Path,
) -> None:
    server = FakeAgentServer()
    database = tmp_path / "calls.sqlite3"
    sessions: list[Any] = []

    async def repository_factory() -> SQLiteRepository:
        return await SQLiteRepository(database).open()

    class FakeObservations:
        def __init__(self) -> None:
            self.events: list[tuple[str, dict[str, object]]] = []

        def schedule_timeline(self, event: str, **details: object) -> None:
            self.events.append((event, details))

        async def timeline(self, event: str, **details: object) -> None:
            self.events.append((event, details))

    class FakeSession:
        def __init__(self, lifecycle: Any, call: Any) -> None:
            self.lifecycle = lifecycle
            self.call = call
            self.policy = SimpleNamespace(
                dtmf=True,
                record=call.channel == "phone",
            )
            self.observations = FakeObservations()
            self.started = False
            self.ended_reason = None
            sessions.append(self)

        async def start(self, room: object) -> None:
            assert room is context.room
            self.started = True

        async def wait(self, *, report_factory: object) -> object:
            assert callable(report_factory)
            return await self.lifecycle.finish("caller_hangup")

        def set_reason(self, reason: str) -> None:
            self.ended_reason = reason

        async def end(self, reason: str) -> None:
            self.ended_reason = reason

    class FakeBuilder:
        async def build(self, *, agent: object, call: object, lifecycle: object) -> Any:
            assert agent is host.agent
            assert cast(Any, call).call_id == expected_call_id
            return FakeSession(lifecycle, call)

    recording_reconciliations: list[tuple[str, str, float]] = []

    class FakeRecordingReconciler:
        async def wait_until_ready(
            self,
            *,
            call_id: str,
            twilio_call_sid: str,
            timeout_s: float,
        ) -> bool:
            recording_reconciliations.append((call_id, twilio_call_sid, timeout_s))
            return True

    def builder_factory(
        repository: object,
        control: object,
        job_context: object,
    ) -> FakeBuilder:
        assert repository is not None
        assert isinstance(control, JobCallControl)
        assert job_context is context
        return FakeBuilder()

    host = LiveKitHost(
        agent=_agent(),
        repository_factory=repository_factory,
        server=cast(Any, server),
        session_builder_factory=cast(Any, builder_factory),
        recording_reconciler_factory=lambda _repository: FakeRecordingReconciler(),
    )
    context = FakeJobContext(
        metadata=json.dumps(
            {
                "call_id": "call-host-entry",
                "channel": "phone",
                "direction": "inbound",
                "provider": "twilio",
            }
        )
    )
    expected_call_id = "call-host-entry"

    await host.entrypoint(cast(Any, context))
    callback = cast(Any, context.room.listeners["sip_dtmf_received"])
    callback(SimpleNamespace(digit="5", code=5))
    assert sessions[0].observations.events == [
        (
            "runtime.recording_ready",
            {"twilio_call_sid": f"CA{'a' * 32}"},
        ),
        ("runtime.dtmf_received", {"digit": "5", "code": 5}),
    ]
    assert recording_reconciliations == [("call-host-entry", f"CA{'a' * 32}", 120.0)]
    async with SQLiteRepository(database) as repository:
        event = await repository.get_terminal_event_for_call("call-host-entry")
    assert event.event_type == "call.completed"

    expected_call_id = await host.reserve_web_call()
    context = FakeJobContext(
        metadata=json.dumps(
            {
                "call_id": expected_call_id,
                "channel": "web",
                "direction": "inbound",
                "provider": "livekit",
            }
        ),
        kind=rtc.ParticipantKind.PARTICIPANT_KIND_STANDARD,
    )
    await host.entrypoint(cast(Any, context))
    async with SQLiteRepository(database) as repository:
        web_event = await repository.get_terminal_event_for_call(expected_call_id)
        web_call = await repository.get_call(expected_call_id)
    assert web_event.event_type == "call.completed"
    assert web_call.timeline[0].event_type == "runtime.reserved"
    assert web_call.timeline[1].event_type == "runtime.admitted"


@pytest.mark.asyncio
async def test_livekit_reservation_expires() -> None:
    gate = LiveKitAdmissionGate(1, reservation_ttl_s=0.01)
    await gate.reserve("expires")
    await asyncio.sleep(0.03)
    assert gate.occupied == 0
