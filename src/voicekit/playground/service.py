"""Admin/read application paired with the runtime's public application."""

from __future__ import annotations

import hashlib
import json
import secrets
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, cast
from urllib.parse import urlsplit

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

from voicekit.config.models import Agent
from voicekit.errors import VoicekitError
from voicekit.obs.records import CallRecord, CallStatus
from voicekit.playground.security import SessionTokenManager
from voicekit.storage.models import (
    PersistedEvent,
    RecordingSnapshot,
    ResultSnapshot,
)

AdminAuthorizer = Callable[[Request], Awaitable[bool]]
ReloadStatus = Callable[[], Mapping[str, object]]
WebCallReserver = Callable[[], Awaitable[str]]
WebCallCanceler = Callable[[str], Awaitable[None]]


class PublicSessionSecurity(Protocol):
    """One-use public-listener authorization shared by both browser runtimes."""

    async def authorize(self, request: Request, *, pc_id: str | None) -> object: ...

    async def bind(self, identity: object, *, pc_id: str, call_id: str) -> None: ...

    async def cancel(self, identity: object) -> None: ...

    async def reserved_call_id(self, identity: object) -> str: ...


class BrowserRoomToken(Protocol):
    @property
    def server_url(self) -> str: ...

    @property
    def participant_token(self) -> str: ...

    @property
    def room_name(self) -> str: ...

    @property
    def participant_identity(self) -> str: ...


class BrowserRoomTokenIssuer(Protocol):
    """Runtime-neutral shape implemented by the pinned LiveKit token issuer."""

    def issue(
        self,
        *,
        call_id: str,
        room_name: str,
        participant_identity: str,
        metadata: dict[str, str],
    ) -> BrowserRoomToken: ...


class PlaygroundRepository(Protocol):
    """Protected reads shared by CLI and the local/admin listener."""

    async def get_call(self, call_id: str) -> CallRecord: ...

    async def list_calls(
        self,
        *,
        status: CallStatus | None = None,
        limit: int = 100,
    ) -> tuple[CallRecord, ...]: ...

    async def get_terminal_event_for_call(self, call_id: str) -> PersistedEvent: ...

    async def get_result_snapshot(self, call_id: str) -> ResultSnapshot: ...

    async def get_recording_for_call(self, call_id: str) -> RecordingSnapshot | None: ...


@dataclass(frozen=True, slots=True)
class PlaygroundSettings:
    """Non-secret listener contract; network binding is owned by the launcher."""

    admin_origin: str
    public_origin: str
    frontend_dir: Path
    local_only: bool = True
    connect_origins: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if _origin(self.admin_origin) != self.admin_origin:
            raise VoicekitError("VK-WEB-002", detail="admin_origin must be a normalized origin.")
        if _origin(self.public_origin) != self.public_origin:
            raise VoicekitError("VK-WEB-002", detail="public_origin must be a normalized origin.")
        if self.local_only and urlsplit(self.admin_origin).hostname not in {
            "127.0.0.1",
            "localhost",
            "::1",
        }:
            raise VoicekitError(
                "VK-WEB-004",
                detail="local admin listener must use a loopback origin.",
            )
        for value in self.connect_origins:
            _connect_origin(value)


class PlaygroundService:
    """Serve local/admin assets and reads while the runtime app stays public-only."""

    def __init__(
        self,
        *,
        agent: Agent,
        public_app: FastAPI,
        repository: PlaygroundRepository,
        tokens: SessionTokenManager,
        settings: PlaygroundSettings,
        reserve_web_call: WebCallReserver,
        admin_authorizer: AdminAuthorizer | None = None,
        reload_status: ReloadStatus | None = None,
        public_security: PublicSessionSecurity | None = None,
        room_token_issuer: BrowserRoomTokenIssuer | None = None,
        cancel_web_call: WebCallCanceler | None = None,
    ) -> None:
        if not settings.local_only and admin_authorizer is None:
            raise VoicekitError(
                "VK-WEB-004",
                detail="deployed session issuance requires an integrator auth hook.",
            )
        self.agent = agent
        self.public_app = public_app
        self.repository = repository
        self.tokens = tokens
        self.settings = settings
        self.reserve_web_call = reserve_web_call
        self.admin_authorizer = admin_authorizer
        self.public_security = public_security
        self.room_token_issuer = room_token_issuer
        self.cancel_web_call = cancel_web_call
        if agent.runtime == "livekit" and (
            public_security is None or room_token_issuer is None or cancel_web_call is None
        ):
            raise VoicekitError(
                "VK-WEB-005",
                detail="LiveKit playground requires public session security and room tokens.",
            )
        self.reload_status = reload_status or (
            lambda: {"revision": 0, "state": "ready", "message": None}
        )
        self._install_public_browser_boundary()
        self.admin_app = self._build_admin_app()

    def update_agent(self, agent: Agent) -> None:
        """Expose a safely reloaded agent revision to bootstrap and dynamic CORS."""
        self.agent = agent

    def frontend(self) -> Path:
        """Return the wheel-embedded SPA directory after verifying its entrypoint."""
        index = self.settings.frontend_dir / "index.html"
        if not self.settings.frontend_dir.is_dir() or not index.is_file() or index.is_symlink():
            raise VoicekitError(
                "VK-WEB-005",
                detail=f"embedded playground entrypoint is missing at {index}.",
            )
        return self.settings.frontend_dir

    def _install_public_browser_boundary(self) -> None:
        @self.public_app.exception_handler(VoicekitError)
        async def public_voicekit_error_handler(  # pyright: ignore[reportUnusedFunction]
            _request: Request,
            error: VoicekitError,
        ) -> JSONResponse:
            return _voicekit_error_response(error)

        @self.public_app.middleware("http")
        async def browser_cors(  # pyright: ignore[reportUnusedFunction]
            request: Request,
            call_next: Callable[[Request], Awaitable[Response]],
        ) -> Response:
            origin = request.headers.get("origin", "")
            allowed = {
                *self.agent.web.allowed_origins,
                self.settings.admin_origin,
            }
            if request.method == "OPTIONS":
                requested_method = request.headers.get("access-control-request-method", "")
                requested_headers = {
                    value.strip().lower()
                    for value in request.headers.get(
                        "access-control-request-headers",
                        "",
                    ).split(",")
                    if value.strip()
                }
                if (
                    origin not in allowed
                    or requested_method not in {"POST", "PATCH"}
                    or not requested_headers <= {"authorization", "content-type"}
                ):
                    return Response(status_code=400)
                response = Response(status_code=200)
            else:
                response = await call_next(request)
            if origin in allowed:
                response.headers["access-control-allow-origin"] = origin
                response.headers["access-control-allow-methods"] = "POST, PATCH, OPTIONS"
                response.headers["access-control-allow-headers"] = "authorization, content-type"
                response.headers["access-control-max-age"] = "600"
                response.headers["vary"] = "Origin"
            return response

        if self.agent.runtime == "livekit":

            @self.public_app.post("/api/livekit/token")
            async def livekit_token(  # pyright: ignore[reportUnusedFunction]
                request: Request,
            ) -> dict[str, str]:
                assert self.public_security is not None
                assert self.room_token_issuer is not None
                assert self.cancel_web_call is not None
                identity = await self.public_security.authorize(request, pc_id=None)
                call_id = await self.public_security.reserved_call_id(identity)
                room_name = f"web-{call_id.removeprefix('call_web_')}"
                participant_identity = f"caller-{call_id.removeprefix('call_web_')}"
                try:
                    issued = self.room_token_issuer.issue(
                        call_id=call_id,
                        room_name=room_name,
                        participant_identity=participant_identity,
                        metadata={
                            "channel": "web",
                            "direction": "inbound",
                            "provider": "livekit",
                        },
                    )
                    await self.public_security.bind(
                        identity,
                        pc_id=room_name,
                        call_id=call_id,
                    )
                except Exception:
                    await self.public_security.cancel(identity)
                    await self.cancel_web_call(call_id)
                    raise
                return {
                    "server_url": issued.server_url,
                    "participant_token": issued.participant_token,
                    "room_name": issued.room_name,
                    "participant_identity": issued.participant_identity,
                }

    def _build_admin_app(self) -> FastAPI:
        app = FastAPI(
            title=f"voicekit playground:{self.agent.name}",
            docs_url=None,
            redoc_url=None,
            openapi_url=None,
        )

        @app.middleware("http")
        async def secure_responses(  # pyright: ignore[reportUnusedFunction]
            request: Request,
            call_next: Callable[[Request], Awaitable[Response]],
        ) -> Response:
            response = await call_next(request)
            response.headers["cache-control"] = "no-store"
            response.headers["x-content-type-options"] = "nosniff"
            response.headers["referrer-policy"] = "no-referrer"
            response.headers["permissions-policy"] = "camera=(), geolocation=(), microphone=(self)"
            response.headers["content-security-policy"] = (
                "default-src 'self'; "
                "script-src 'self' blob:; "
                "style-src 'self'; "
                "font-src 'self'; "
                f"connect-src 'self' {self.settings.public_origin}"
                f"{''.join(f' {value}' for value in self.settings.connect_origins)}; "
                "media-src 'self' blob:; "
                "img-src 'self' data:; "
                "worker-src 'self' blob:; "
                "object-src 'none'; "
                "base-uri 'none'; "
                "frame-ancestors 'none'"
            )
            return response

        @app.exception_handler(VoicekitError)
        async def voicekit_error_handler(  # pyright: ignore[reportUnusedFunction]
            _request: Request,
            error: VoicekitError,
        ) -> JSONResponse:
            return _voicekit_error_response(error)

        @app.get("/api/playground/bootstrap")
        async def bootstrap(  # pyright: ignore[reportUnusedFunction]
            request: Request,
        ) -> dict[str, object]:
            await self._authorize_admin(request)
            return {
                "agent": self.agent.name,
                "runtime": self.agent.runtime,
                "public_origin": self.settings.public_origin,
                "models": self.agent.models.model_dump(mode="json"),
                "reload": dict(self.reload_status()),
            }

        @app.post("/api/playground/sessions")
        async def issue_session(  # pyright: ignore[reportUnusedFunction]
            request: Request,
        ) -> dict[str, object]:
            await self._authorize_admin(request)
            issued = await self.tokens.issue_for_call(
                client_key=_client_key(request),
                reserve_call=self.reserve_web_call,
            )
            return {
                "session_id": issued.identity.session_id,
                "token": issued.token,
                "expires_at": issued.identity.expires_at,
                "runtime": self.agent.runtime,
                **(
                    {"webrtc_url": f"{self.settings.public_origin}/api/offer"}
                    if self.agent.runtime == "pipecat"
                    else {"token_url": f"{self.settings.public_origin}/api/livekit/token"}
                ),
                "poll_url": f"/api/playground/sessions/{issued.identity.session_id}",
            }

        @app.get("/api/playground/sessions/{session_id}")
        async def session_snapshot(  # pyright: ignore[reportUnusedFunction]
            request: Request,
            session_id: str,
        ) -> dict[str, object]:
            await self._authorize_admin(request)
            session = await self.tokens.snapshot(session_id)
            call: CallRecord | None = None
            data: ResultSnapshot | None = None
            terminal: dict[str, object] | None = None
            recording: RecordingSnapshot | None = None
            if session.call_id is not None:
                call = await self.repository.get_call(session.call_id)
                data = await self.repository.get_result_snapshot(session.call_id)
                recording = await self.repository.get_recording_for_call(session.call_id)
                if call.status != "active":
                    event = await self.repository.get_terminal_event_for_call(session.call_id)
                    terminal = _event_payload(event)
                    if session.pc_id is not None and session.active:
                        await self.tokens.release(session.pc_id)
                        session = await self.tokens.snapshot(session_id)
            return {
                "session": {
                    "session_id": session.session_id,
                    "expires_at": session.expires_at,
                    "call_id": session.call_id,
                    "pc_id": session.pc_id,
                    "active": session.active,
                },
                "call": None if call is None else call.model_dump(mode="json"),
                "data": None if data is None else data.model_dump(mode="json"),
                "recording": (None if recording is None else recording.model_dump(mode="json")),
                "terminal_event": terminal,
                "reload": dict(self.reload_status()),
            }

        @app.get("/api/admin/calls")
        async def calls(  # pyright: ignore[reportUnusedFunction]
            request: Request,
            limit: int = 100,
        ) -> dict[str, object]:
            await self._authorize_admin(request)
            records = await self.repository.list_calls(limit=limit)
            return {"items": [record.model_dump(mode="json") for record in records]}

        @app.get("/api/admin/calls/{call_id}")
        async def call(  # pyright: ignore[reportUnusedFunction]
            request: Request,
            call_id: str,
        ) -> dict[str, object]:
            await self._authorize_admin(request)
            record = await self.repository.get_call(call_id)
            return {"call": record.model_dump(mode="json")}

        @app.get("/api/admin/calls/{call_id}/result")
        async def result(  # pyright: ignore[reportUnusedFunction]
            request: Request,
            call_id: str,
        ) -> dict[str, object]:
            await self._authorize_admin(request)
            return _event_payload(await self.repository.get_terminal_event_for_call(call_id))

        @app.get("/api/admin/recordings/{call_id}")
        async def recording(  # pyright: ignore[reportUnusedFunction]
            request: Request,
            call_id: str,
        ) -> dict[str, object]:
            await self._authorize_admin(request)
            value = await self.repository.get_recording_for_call(call_id)
            if value is None:
                raise VoicekitError("VK-OBS-003", detail=f"recording for {call_id}")
            return {"recording": value.model_dump(mode="json")}

        app.frontend("/", directory=self.frontend(), fallback="index.html")
        return app

    async def _authorize_admin(self, request: Request) -> None:
        expected_host = urlsplit(self.settings.admin_origin).netloc
        if not secrets.compare_digest(request.headers.get("host", ""), expected_host):
            raise VoicekitError("VK-WEB-004", detail="admin host does not match its listener.")
        origin = request.headers.get("origin")
        if origin is not None and not secrets.compare_digest(origin, self.settings.admin_origin):
            raise VoicekitError("VK-WEB-002", detail="admin browser origin is not allowed.")
        if self.admin_authorizer is not None and not await self.admin_authorizer(request):
            raise VoicekitError("VK-WEB-004", detail="integrator authorization failed.")


def _event_payload(event: PersistedEvent) -> dict[str, object]:
    try:
        value: object = json.loads(event.body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise VoicekitError("VK-RES-007", detail=event.event_id) from exc
    if not isinstance(value, dict):
        raise VoicekitError("VK-RES-007", detail=event.event_id)
    return dict(cast("dict[str, object]", value))


def _voicekit_error_response(error: VoicekitError) -> JSONResponse:
    status = {
        "VK-WEB-001": 404,
        "VK-WEB-002": 403,
        "VK-WEB-003": 429,
        "VK-WEB-004": 403,
        "VK-RUN-004": 429,
        "VK-OBS-003": 404,
        "VK-RES-009": 404,
    }.get(error.code, 400)
    return JSONResponse(
        status_code=status,
        content={
            "error": {
                "code": error.code,
                "message": error.definition.cause,
                "detail": error.detail,
                "fix": error.definition.fix,
            }
        },
    )


def _client_key(request: Request) -> str:
    address = request.client.host if request.client else "local"
    user_agent = request.headers.get("user-agent", "")
    return hashlib.sha256(f"{address}\0{user_agent}".encode()).hexdigest()


def _origin(value: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or parsed.username
        or parsed.password
    ):
        raise VoicekitError("VK-WEB-002", detail=f"{value!r} is not an origin.")
    return value.removesuffix("/")


def _connect_origin(value: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https", "ws", "wss"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise VoicekitError("VK-WEB-002", detail=f"{value!r} is not a connect origin.")
    return value.removesuffix("/")
