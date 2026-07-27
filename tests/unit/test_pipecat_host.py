import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any, ClassVar, cast

import httpx
import pytest
from pipecat.runner.types import CallData
from starlette.datastructures import URL

from voicekit import Agent, Behavior, Limits, Models, Phone, Results, Web, tool
from voicekit.runtimes.pipecat import host as host_module
from voicekit.runtimes.pipecat.host import (
    LongLivedRunner,
    PipecatHost,
    PipecatHostSettings,
    TwilioRuntimeAdapter,
    twilio_transport_params,
)
from voicekit.runtimes.pipecat.lifecycle import PipecatCall, PipecatCallLifecycle
from voicekit.runtimes.pipecat.session import PipecatSessionBuilder
from voicekit.storage.sqlite import SQLiteRepository
from voicekit.telephony import CallEvent, PipecatTarget, TelephonyRequest


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
        phone=Phone(provider="twilio", number="+14155550123"),
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
    def __init__(self) -> None:
        self.connection = _Connection()

    async def handle_web_request(self, request: object, callback: Any) -> dict[str, str]:
        del request
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
        ),
        twilio=cast(TwilioRuntimeAdapter, adapter),
        request_handler=cast(Any, request_handler) if request_handler else None,
        session_builder=(
            cast(PipecatSessionBuilder, session_builder) if session_builder is not None else None
        ),
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
    with pytest.raises(Exception, match="VK-RUN-002"):
        PipecatHostSettings(**cast(Any, settings))


def test_host_settings_load_credentials_from_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "AC-from-env")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "token-from-env")

    settings = PipecatHostSettings.from_env("https://voice.example")

    assert settings.twilio_account_sid == "AC-from-env"
    assert settings.twilio_auth_token == "token-from-env"


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
    assert adapter.targets[0].custom_parameters["voicekit_token"]
    assert adapter.targets[0].custom_parameters["from_number"] == "+14155550100"
    await host._finish_pending(  # pyright: ignore[reportPrivateUsage]
        "CA-first",
        "provider_hangup",
    )
    await repository.close()


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
        adapter.targets[0].custom_parameters["voicekit_token"]
        == adapter.targets[1].custom_parameters["voicekit_token"]
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
    with pytest.raises(Exception, match="VK-OBS-003"):
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
    assert response.json()["error"]["code"] == "VK-RUN-007"
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
                body={"voicekit_token": pending.admission.token},
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
    assert websocket.closed == (1008, "VK-RUN-007")
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
    assert websocket.closed == (1011, "VK-RUN-006")
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
    assert invalid.json()["error"]["code"] == "VK-RUN-007"
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
    assert response.json()["error"]["code"] == "VK-RUN-007"
    await repository.close()
