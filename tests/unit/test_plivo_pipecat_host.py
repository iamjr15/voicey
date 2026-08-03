"""Plivo-specific Pipecat routing, media, and terminal-authority tests."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, ClassVar, cast

import httpx
from starlette.datastructures import URL

from voicey import Agent, Behavior, Limits, Models, Phone, Results, Web, tool
from voicey.runtimes.pipecat.host import (
    PipecatHost,
    PipecatHostSettings,
    PlivoRuntimeAdapter,
    _plivo_handshake,  # pyright: ignore[reportPrivateUsage]
    plivo_transport_params,
)
from voicey.runtimes.pipecat.lifecycle import PipecatCallLifecycle
from voicey.runtimes.pipecat.session import PipecatSessionBuilder
from voicey.storage.sqlite import SQLiteRepository
from voicey.telephony import CallEvent, PipecatTarget, TelephonyRequest


def entry() -> dict[str, object]:
    return {
        "name": "entry",
        "task_messages": [{"role": "developer", "content": "Test."}],
        "respond_immediately": False,
    }


@tool
def identify() -> str:
    """Return a stable test identity."""
    return "plivo-host-test"


def _agent(*, record: bool = False) -> Agent:
    return Agent(
        name="plivo-host-test",
        runtime="pipecat",
        models=Models(
            stt="deepgram/nova-3",
            llm="anthropic/claude-sonnet-5",
            tts="cartesia/sonic-3.5",
        ),
        persona="Helpful.",
        flow=f"{__name__}:entry",
        tools=[identify],
        phone=Phone(provider="plivo", number="+14155550100", record=record),
        web=Web(enabled=False),
        results=Results(
            webhook="https://receiver.example/results",
            secret_env="RESULT_SECRET",  # pragma: allowlist secret
        ),
        limits=Limits(max_duration_s=60, max_concurrent=2, silence_hangup_s=10),
        behavior=Behavior(voicemail="hangup"),
    )


class _Plivo:
    def __init__(self, *, verified: bool = True) -> None:
        self.verified = verified
        self.targets: list[PipecatTarget] = []
        self.recordings: list[tuple[str, PipecatTarget]] = []
        self.hangups: list[str] = []

    def verify_request(self, _request: TelephonyRequest) -> bool:
        return self.verified

    def answer_response(self, target: object) -> str:
        self.targets.append(cast("PipecatTarget", target))
        return "<Response><Stream>media</Stream></Response>"

    def transfer_response(self, to_number: str, *, caller_id: str | None = None) -> str:
        return f"<Response><Dial callerId={caller_id}>{to_number}</Dial></Response>"

    def parse_event(self, request: TelephonyRequest) -> CallEvent:
        form = cast("Any", request.form)
        status = str(form.get("CallStatus", "initiated"))
        call_id = str(form.get("CallUUID", form.get("RequestUUID", "missing")))
        terminal = status == "completed"
        return CallEvent(
            type="completed" if terminal else "initiated",
            provider_call_id=call_id,
            provider_status=status,
            ended_reason="provider_hangup" if terminal else None,
            direction="inbound",
            from_number=str(form.get("From", "")) or None,
            to_number=str(form.get("To", "")) or None,
        )

    def start_recording(self, call_uuid: str, target: object) -> str:
        self.recordings.append((call_uuid, cast("PipecatTarget", target)))
        return "recording-test"

    def cold_transfer(self, _call_uuid: str, _to_number: str) -> None:
        return None

    def hangup(self, call_uuid: str) -> None:
        self.hangups.append(call_uuid)


class _Session:
    def __init__(self, lifecycle: PipecatCallLifecycle) -> None:
        self.lifecycle = lifecycle
        self.provider_terminal_required = False
        self.started = False
        self.ended = asyncio.Event()

    async def start(self, _runner: object) -> None:
        self.started = True

    async def wait(self) -> object:
        await self.ended.wait()
        return await self.lifecycle.finish("provider_hangup")

    async def end(self, _reason: str) -> None:
        self.ended.set()


class _SessionBuilder:
    def __init__(self) -> None:
        self.sessions: list[_Session] = []

    def build(
        self,
        *,
        agent: Agent,
        call: object,
        lifecycle: PipecatCallLifecycle,
        transport: object,
        sample_rate: int,
    ) -> _Session:
        del agent, call, transport
        assert sample_rate == 8000
        session = _Session(lifecycle)
        self.sessions.append(session)
        return session


class _WebSocket:
    url = URL("wss://voice.example/plivo/media/token")
    headers: ClassVar[dict[str, str]] = {}
    client = SimpleNamespace(host="203.0.113.1")

    def __init__(self, frame: dict[str, object]) -> None:
        self.frame = frame
        self.receive_count = 0
        self.closed: tuple[int, str] | None = None

    async def accept(self) -> None:
        return None

    async def receive_text(self) -> str:
        self.receive_count += 1
        if self.receive_count > 1:
            raise AssertionError("host consumed a media frame")
        return json.dumps(self.frame)

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.closed = (code, reason)


async def _host(
    tmp_path: Path,
    *,
    adapter: _Plivo | None = None,
    record: bool = False,
) -> tuple[PipecatHost, SQLiteRepository, _Plivo, _SessionBuilder]:
    repository = SQLiteRepository(tmp_path / "plivo-host.sqlite3")
    await repository.open()
    plivo = adapter or _Plivo()
    sessions = _SessionBuilder()
    host = PipecatHost(
        agent=_agent(record=record),
        repository=repository,
        settings=PipecatHostSettings(
            public_base="https://voice.example",
            twilio_account_sid="",
            twilio_auth_token="",
            pending_media_timeout_s=60,
            allow_insecure_web_sessions_for_tests=True,
        ),
        plivo=cast("PlivoRuntimeAdapter", plivo),
        session_builder=cast("PipecatSessionBuilder", sessions),
    )
    return host, repository, plivo, sessions


def test_plivo_transport_and_handshake_are_exactly_pcmu_8khz() -> None:
    params = plivo_transport_params(
        settings=PipecatHostSettings(
            public_base="https://voice.example",
            twilio_account_sid="",
            twilio_auth_token="",
        ),
        call_id="plivo-call",
        stream_id="plivo-stream",
        max_duration_s=60,
    )
    serializer = cast("Any", params.serializer)
    assert params.audio_in_sample_rate == params.audio_out_sample_rate == 8000
    assert params.session_timeout == 90
    assert serializer._params.plivo_sample_rate == 8000
    assert serializer._params.sample_rate == 8000
    assert serializer._params.auto_hang_up is False
    frame = {
        "event": "start",
        "start": {
            "callId": "plivo-call",
            "streamId": "plivo-stream",
            "mediaFormat": {"contentType": "audio/x-mulaw", "sampleRate": 8000},
        },
    }
    assert _plivo_handshake(json.dumps(frame)) == ("plivo-call", "plivo-stream")


async def test_answer_media_and_terminal_callback_share_one_lifecycle(tmp_path: Path) -> None:
    host, repository, adapter, sessions = await _host(tmp_path, record=True)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=host.app),
        base_url="https://voice.example",
    ) as client:
        answer = await client.post(
            "/plivo/answer",
            data={
                "CallUUID": "plivo-call",
                "CallStatus": "initiated",
                "From": "+14155550101",
                "To": "+14155550100",
            },
        )
    assert answer.status_code == 200
    target = adapter.targets[0]
    token = target.ws_path.rsplit("/", maxsplit=1)[-1]
    assert target.ws_path.startswith("/plivo/media/")
    assert adapter.recordings[0][0] == "plivo-call"

    websocket = _WebSocket(
        {
            "event": "start",
            "start": {
                "callId": "plivo-call",
                "streamId": "plivo-stream",
                "mediaFormat": {"contentType": "audio/x-mulaw", "sampleRate": 8000},
            },
        }
    )
    route = next(
        route for route in host.app.routes if getattr(route, "path", None) == "/plivo/media/{token}"
    )
    media_task = asyncio.create_task(cast("Any", route).endpoint(websocket, token))
    for _ in range(20):
        if sessions.sessions and sessions.sessions[0].started:
            break
        await asyncio.sleep(0)
    assert sessions.sessions[0].provider_terminal_required
    assert websocket.receive_count == 1
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=host.app),
        base_url="https://voice.example",
    ) as client:
        terminal = await client.post(
            "/plivo/events",
            data={"CallUUID": "plivo-call", "CallStatus": "completed"},
        )
    await media_task
    persisted = await repository.get_call("plivo-call")
    assert terminal.status_code == 204
    assert persisted.status == "completed"
    assert persisted.terminal_reason == "provider_hangup"
    assert host.admission.active_count == 0
    await repository.close()


async def test_unsigned_answer_fails_before_reservation(tmp_path: Path) -> None:
    host, repository, _, _ = await _host(tmp_path, adapter=_Plivo(verified=False))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=host.app),
        base_url="https://voice.example",
    ) as client:
        denied = await client.post(
            "/plivo/answer",
            data={"CallUUID": "plivo-denied", "CallStatus": "initiated"},
        )
    assert denied.status_code == 403
    assert host.admission.active_count == 0
    await repository.close()
