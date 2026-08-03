from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from itertools import count
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from fastapi import FastAPI, Request

from voicey import Agent, Behavior, Limits, Models, Results, Web, tool
from voicey.errors import VoiceyError
from voicey.obs import NewCall
from voicey.playground.security import (
    OriginPolicy,
    SessionTokenManager,
    WebSessionSecurity,
)
from voicey.playground.service import PlaygroundService, PlaygroundSettings
from voicey.results.signing import encode_secret
from voicey.storage import (
    ResultDeliveryConfig,
    ResultSnapshot,
    SQLiteRepository,
    TerminalRequest,
)

SECRET = encode_secret(b"playground-service-test-secret")
NOW = datetime(2026, 7, 27, 12, 0, tzinfo=UTC)


@tool
def identify() -> str:
    """Return a stable test identity."""
    return "playground-test"


def _agent() -> Agent:
    return Agent(
        name="playground-test",
        runtime="pipecat",
        models=Models(
            stt="deepgram/nova-3",
            llm="anthropic/claude-sonnet-5",
            tts="cartesia/sonic-3.5",
        ),
        persona="Helpful.",
        flow="tests.fixtures.agent:entry",
        tools=[identify],
        web=Web(enabled=True, allowed_origins=["http://127.0.0.1:7860"]),
        results=Results(
            webhook="https://receiver.example/results",
            secret_env="RESULT_SECRET",  # pragma: allowlist secret
        ),
        limits=Limits(max_duration_s=60, max_concurrent=2, silence_hangup_s=10),
        behavior=Behavior(),
    )


def _livekit_agent() -> Agent:
    return _agent().model_copy(update={"runtime": "livekit"})


class FakeRoomTokenIssuer:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def issue(self, **values: object) -> Any:
        self.calls.append(values)
        return type(
            "IssuedRoomToken",
            (),
            {
                "server_url": "wss://project.livekit.cloud",
                "participant_token": "livekit-participant-token",
                "room_name": values["room_name"],
                "participant_identity": values["participant_identity"],
            },
        )()


def _frontend(tmp_path: Path) -> Path:
    frontend = tmp_path / "frontend"
    frontend.mkdir(parents=True)
    (frontend / "index.html").write_text("<!doctype html><title>voicey</title>")
    return frontend


def _tokens() -> SessionTokenManager:
    return SessionTokenManager(
        SECRET,
        audience="http://127.0.0.1:7861",
        agent_name="playground-test",
    )


def _settings(tmp_path: Path, *, local_only: bool = True) -> PlaygroundSettings:
    return PlaygroundSettings(
        admin_origin=("http://127.0.0.1:7860" if local_only else "https://admin.example"),
        public_origin="http://127.0.0.1:7861",
        frontend_dir=_frontend(tmp_path),
        local_only=local_only,
    )


def _reserver(repository: SQLiteRepository) -> Callable[[], Awaitable[str]]:
    sequence = count(1)

    async def reserve() -> str:
        call_id = f"call_web_reserved_{next(sequence)}"
        await repository.begin_call(
            NewCall(
                call_id=call_id,
                agent_name="playground-test",
                runtime="pipecat",
                channel="web",
                direction="inbound",
                config_hash=_agent().config_hash,
                started_at=NOW,
            ),
            owner_id="playground-reserver",
            delivery=ResultDeliveryConfig(
                endpoint="https://receiver.example/results",
            ),
            lease_ttl=timedelta(seconds=30),
            now=NOW,
        )
        return call_id

    return reserve


async def _unused_reserver() -> str:
    return "call_never_issued"


async def _request(
    app: FastAPI,
    method: str,
    path: str,
    *,
    base_url: str = "http://127.0.0.1:7860",
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url=base_url,
    ) as client:
        return await client.request(method, path, headers=headers)


@pytest.mark.asyncio
async def test_playground_serves_assets_bootstrap_and_one_use_session(
    tmp_path: Path,
) -> None:
    public = FastAPI()
    tokens = _tokens()
    async with SQLiteRepository(tmp_path / "calls.sqlite3") as repository:
        service = PlaygroundService(
            agent=_agent(),
            public_app=public,
            repository=repository,
            tokens=tokens,
            settings=_settings(tmp_path),
            reserve_web_call=_reserver(repository),
            reload_status=lambda: {
                "revision": 7,
                "state": "ready",
                "message": "configuration loaded",
            },
        )

        page = await _request(service.admin_app, "GET", "/")
        deep_link = await _request(
            service.admin_app,
            "GET",
            "/sessions/current",
            headers={"accept": "text/html"},
        )
        bootstrap = await _request(
            service.admin_app,
            "GET",
            "/api/playground/bootstrap",
        )
        session = await _request(
            service.admin_app,
            "POST",
            "/api/playground/sessions",
            headers={"user-agent": "voicey-test"},
        )
        issued = cast("dict[str, Any]", session.json())
        identity = await tokens.authorize(str(issued["token"]), pc_id=None)
        assert identity.call_id is not None
        reserved_call = await repository.get_call(identity.call_id)
        await tokens.bind(identity, pc_id="pc_browser", call_id=identity.call_id)
        with pytest.raises(VoiceyError, match="replayed"):
            await tokens.authorize(str(issued["token"]), pc_id=None)

    assert page.status_code == 200
    assert "voicey" in page.text
    assert deep_link.status_code == 200
    assert deep_link.text == page.text
    assert page.headers["cache-control"] == "no-store"
    assert "frame-ancestors 'none'" in page.headers["content-security-policy"]
    assert "script-src 'self' blob:" in page.headers["content-security-policy"]
    assert "worker-src 'self' blob:" in page.headers["content-security-policy"]
    assert "daily.co" not in page.headers["content-security-policy"]
    assert "microphone=(self)" in page.headers["permissions-policy"]
    assert bootstrap.json() == {
        "agent": "playground-test",
        "runtime": "pipecat",
        "public_origin": "http://127.0.0.1:7861",
        "models": {
            "stt": "deepgram/nova-3",
            "llm": "anthropic/claude-sonnet-5",
            "tts": "cartesia/sonic-3.5",
            "fallbacks": {},
        },
        "reload": {
            "revision": 7,
            "state": "ready",
            "message": "configuration loaded",
        },
    }
    assert session.status_code == 200
    assert issued["webrtc_url"] == "http://127.0.0.1:7861/api/offer"
    assert issued["token"].count(".") == 2
    assert reserved_call.status == "active"


@pytest.mark.asyncio
async def test_livekit_token_exchange_stays_on_public_listener_and_is_one_use(
    tmp_path: Path,
) -> None:
    public = FastAPI()
    tokens = _tokens()
    issuer = FakeRoomTokenIssuer()
    canceled: list[str] = []
    security = WebSessionSecurity(
        tokens,
        OriginPolicy(
            allowed_origins=frozenset({"http://127.0.0.1:7860"}),
            expected_public_origin="http://127.0.0.1:7861",
        ),
    )
    async with SQLiteRepository(tmp_path / "calls.sqlite3") as repository:
        service = PlaygroundService(
            agent=_livekit_agent(),
            public_app=public,
            repository=repository,
            tokens=tokens,
            settings=PlaygroundSettings(
                admin_origin="http://127.0.0.1:7860",
                public_origin="http://127.0.0.1:7861",
                frontend_dir=_frontend(tmp_path),
                connect_origins=("wss://project.livekit.cloud",),
            ),
            reserve_web_call=_reserver(repository),
            public_security=security,
            room_token_issuer=issuer,
            cancel_web_call=lambda call_id: _record_cancel(canceled, call_id),
        )
        session_response = await _request(
            service.admin_app,
            "POST",
            "/api/playground/sessions",
        )
        session = cast("dict[str, Any]", session_response.json())
        exchanged = await _request(
            service.public_app,
            "POST",
            "/api/livekit/token",
            base_url="http://127.0.0.1:7861",
            headers={
                "origin": "http://127.0.0.1:7860",
                "authorization": f"Bearer {session['token']}",
            },
        )
        replay = await _request(
            service.public_app,
            "POST",
            "/api/livekit/token",
            base_url="http://127.0.0.1:7861",
            headers={
                "origin": "http://127.0.0.1:7860",
                "authorization": f"Bearer {session['token']}",
            },
        )
        admin_token_route = await _request(
            service.admin_app,
            "POST",
            "/api/livekit/token",
        )
        page = await _request(service.admin_app, "GET", "/")

    body = exchanged.json()
    assert session["runtime"] == "livekit"
    assert session["token_url"] == "http://127.0.0.1:7861/api/livekit/token"
    assert "webrtc_url" not in session
    assert body["server_url"] == "wss://project.livekit.cloud"
    assert body["participant_token"] == "livekit-participant-token"
    assert body["room_name"].startswith("web-")
    assert issuer.calls[0]["call_id"] == "call_web_reserved_1"
    assert replay.status_code == 404
    assert replay.json()["error"]["code"] == "VY-WEB-001"
    assert admin_token_route.status_code == 404
    assert "wss://project.livekit.cloud" in page.headers["content-security-policy"]
    assert canceled == []


async def _record_cancel(values: list[str], call_id: str) -> None:
    values.append(call_id)


@pytest.mark.asyncio
async def test_active_and_terminal_session_snapshots_are_durable_and_exact(
    tmp_path: Path,
) -> None:
    public = FastAPI()
    tokens = _tokens()
    async with SQLiteRepository(tmp_path / "calls.sqlite3") as repository:
        service = PlaygroundService(
            agent=_agent(),
            public_app=public,
            repository=repository,
            tokens=tokens,
            settings=_settings(tmp_path),
            reserve_web_call=_reserver(repository),
        )
        issued = await tokens.issue(client_key="browser")
        identity = await tokens.authorize(issued.token, pc_id=None)
        lease = await repository.begin_call(
            NewCall(
                call_id="call_browser",
                agent_name="playground-test",
                runtime="pipecat",
                channel="web",
                direction="inbound",
                config_hash=f"sha256:{'a' * 64}",
                started_at=NOW,
            ),
            owner_id="worker",
            delivery=ResultDeliveryConfig(
                endpoint="https://receiver.example/results",
                recording_enabled=True,
            ),
            lease_ttl=timedelta(seconds=30),
            now=NOW,
        )
        await repository.flush_results(
            lease,
            ResultSnapshot(
                outcome="qualified",
                data={"account": "north"},
                interruptions=1,
            ),
        )
        await tokens.bind(identity, pc_id="pc_browser", call_id="call_browser")

        active = await _request(
            service.admin_app,
            "GET",
            f"/api/playground/sessions/{identity.session_id}",
        )
        terminal_event = await repository.terminalize(
            lease,
            TerminalRequest(
                event_type="call.completed",
                ended_reason="caller_hangup",
                ended_at=NOW + timedelta(seconds=12),
            ),
        )
        await tokens.release("pc_browser")
        finished = await _request(
            service.admin_app,
            "GET",
            f"/api/playground/sessions/{identity.session_id}",
        )
        pull = await _request(
            service.admin_app,
            "GET",
            "/api/admin/calls/call_browser/result",
        )
        calls = await _request(service.admin_app, "GET", "/api/admin/calls?limit=5")
        call = await _request(
            service.admin_app,
            "GET",
            "/api/admin/calls/call_browser",
        )
        recording = await _request(
            service.admin_app,
            "GET",
            "/api/admin/recordings/call_browser",
        )

    active_body = cast("dict[str, Any]", active.json())
    assert active_body["session"]["active"] is True
    assert active_body["call"]["status"] == "active"
    assert active_body["data"] == {
        "outcome": "qualified",
        "data": {"account": "north"},
        "interruptions": 1,
    }
    assert active_body["recording"]["status"] == "pending"
    assert active_body["terminal_event"] is None

    expected = json.loads(terminal_event.body)
    assert finished.json()["session"]["active"] is False
    assert finished.json()["terminal_event"] == expected
    assert pull.json() == expected
    assert calls.json()["items"][0]["call_id"] == "call_browser"
    assert call.json()["call"]["status"] == "completed"
    assert recording.json()["recording"]["status"] == "pending"


@pytest.mark.asyncio
async def test_admin_host_origin_and_deployed_authorizer_are_enforced(
    tmp_path: Path,
) -> None:
    public = FastAPI()
    async with SQLiteRepository(tmp_path / "calls.sqlite3") as repository:
        local = PlaygroundService(
            agent=_agent(),
            public_app=public,
            repository=repository,
            tokens=_tokens(),
            settings=_settings(tmp_path / "local"),
            reserve_web_call=_reserver(repository),
        )
        hostile_host = await _request(
            local.admin_app,
            "GET",
            "/api/playground/bootstrap",
            base_url="http://attacker.example",
        )
        hostile_origin = await _request(
            local.admin_app,
            "GET",
            "/api/playground/bootstrap",
            headers={"origin": "https://attacker.example"},
        )

        with pytest.raises(VoiceyError, match="auth hook"):
            PlaygroundService(
                agent=_agent(),
                public_app=FastAPI(),
                repository=repository,
                tokens=_tokens(),
                settings=_settings(tmp_path / "missing-auth", local_only=False),
                reserve_web_call=_reserver(repository),
            )

        async def authorize(request: Request) -> bool:
            return request.headers.get("x-integrator-key") == "allowed"

        deployed = PlaygroundService(
            agent=_agent(),
            public_app=FastAPI(),
            repository=repository,
            tokens=_tokens(),
            settings=_settings(tmp_path / "deployed", local_only=False),
            reserve_web_call=_reserver(repository),
            admin_authorizer=authorize,
        )
        denied = await _request(
            deployed.admin_app,
            "GET",
            "/api/playground/bootstrap",
            base_url="https://admin.example",
        )
        allowed = await _request(
            deployed.admin_app,
            "GET",
            "/api/playground/bootstrap",
            base_url="https://admin.example",
            headers={"x-integrator-key": "allowed"},
        )

    assert hostile_host.status_code == 403
    assert hostile_host.json()["error"]["code"] == "VY-WEB-004"
    assert hostile_origin.status_code == 403
    assert hostile_origin.json()["error"]["code"] == "VY-WEB-002"
    assert denied.status_code == 403
    assert allowed.status_code == 200


@pytest.mark.asyncio
async def test_public_listener_only_exposes_cors_protected_signaling(
    tmp_path: Path,
) -> None:
    public = FastAPI()
    async with SQLiteRepository(tmp_path / "calls.sqlite3") as repository:
        service = PlaygroundService(
            agent=_agent(),
            public_app=public,
            repository=repository,
            tokens=_tokens(),
            settings=_settings(tmp_path),
            reserve_web_call=_reserver(repository),
        )
        preflight = await _request(
            service.public_app,
            "OPTIONS",
            "/api/offer",
            base_url="http://127.0.0.1:7861",
            headers={
                "origin": "http://127.0.0.1:7860",
                "access-control-request-method": "POST",
                "access-control-request-headers": "authorization,content-type",
            },
        )
        token_route = await _request(
            service.public_app,
            "POST",
            "/api/playground/sessions",
            base_url="http://127.0.0.1:7861",
        )
        admin_route = await _request(
            service.public_app,
            "GET",
            "/api/admin/calls",
            base_url="http://127.0.0.1:7861",
        )
        updated = _agent().model_copy(
            update={
                "web": Web(
                    enabled=True,
                    allowed_origins=["https://new-admin.example"],
                )
            }
        )
        service.update_agent(updated)
        reloaded_origin = await _request(
            service.public_app,
            "OPTIONS",
            "/api/offer",
            base_url="http://127.0.0.1:7861",
            headers={
                "origin": "https://new-admin.example",
                "access-control-request-method": "POST",
                "access-control-request-headers": "authorization",
            },
        )

    assert preflight.status_code == 200
    assert preflight.headers["access-control-allow-origin"] == "http://127.0.0.1:7860"
    assert "authorization" in preflight.headers["access-control-allow-headers"].lower()
    assert token_route.status_code == 404
    assert admin_route.status_code == 404
    assert reloaded_origin.status_code == 200
    assert reloaded_origin.headers["access-control-allow-origin"] == "https://new-admin.example"


def test_playground_settings_and_assets_reject_unsafe_shapes(tmp_path: Path) -> None:
    with pytest.raises(VoiceyError, match="normalized origin"):
        PlaygroundSettings(
            admin_origin="http://127.0.0.1:7860/",
            public_origin="http://127.0.0.1:7861",
            frontend_dir=tmp_path,
        )
    with pytest.raises(VoiceyError, match="loopback"):
        PlaygroundSettings(
            admin_origin="https://admin.example",
            public_origin="https://voice.example",
            frontend_dir=tmp_path,
        )
    with pytest.raises(VoiceyError, match="not an origin"):
        PlaygroundSettings(
            admin_origin="ftp://127.0.0.1",
            public_origin="https://voice.example",
            frontend_dir=tmp_path,
        )

    missing = PlaygroundSettings(
        admin_origin="http://127.0.0.1:7860",
        public_origin="http://127.0.0.1:7861",
        frontend_dir=tmp_path / "missing",
    )
    with pytest.raises(VoiceyError, match="entrypoint"):
        PlaygroundService(
            agent=_agent(),
            public_app=FastAPI(),
            repository=cast("Any", object()),
            tokens=_tokens(),
            settings=missing,
            reserve_web_call=_unused_reserver,
        )


@pytest.mark.asyncio
async def test_missing_sessions_recordings_and_events_use_catalog_errors(
    tmp_path: Path,
) -> None:
    public = FastAPI()
    async with SQLiteRepository(tmp_path / "calls.sqlite3") as repository:
        service = PlaygroundService(
            agent=_agent(),
            public_app=public,
            repository=repository,
            tokens=_tokens(),
            settings=_settings(tmp_path),
            reserve_web_call=_reserver(repository),
        )
        session = await _request(
            service.admin_app,
            "GET",
            "/api/playground/sessions/web_missing",
        )
        recording = await _request(
            service.admin_app,
            "GET",
            "/api/admin/recordings/call_missing",
        )
        result = await _request(
            service.admin_app,
            "GET",
            "/api/admin/calls/call_missing/result",
        )

    assert session.status_code == 404
    assert session.json()["error"]["code"] == "VY-WEB-001"
    assert recording.status_code == 404
    assert recording.json()["error"]["code"] == "VY-OBS-003"
    assert result.status_code == 404
    assert result.json()["error"]["code"] == "VY-RES-009"
