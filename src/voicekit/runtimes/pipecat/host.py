"""Long-lived FastAPI/WorkerRunner host for Twilio and SmallWebRTC."""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncGenerator, Mapping
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field, replace
from typing import Any, Protocol, cast

from fastapi import FastAPI, Request, Response, WebSocket
from fastapi.responses import JSONResponse
from pipecat.runner.types import CallData
from pipecat.runner.utils import parse_telephony_websocket
from pipecat.serializers.twilio import TwilioFrameSerializer
from pipecat.transports.base_transport import TransportParams
from pipecat.transports.smallwebrtc.request_handler import (
    IceCandidate,
    SmallWebRTCPatchRequest,
    SmallWebRTCRequest,
    SmallWebRTCRequestHandler,
)
from pipecat.transports.smallwebrtc.transport import SmallWebRTCTransport
from pipecat.transports.websocket.fastapi import (
    FastAPIWebsocketParams,
    FastAPIWebsocketTransport,
)
from pipecat.workers.runner import WorkerRunner

from voicekit.config.models import Agent
from voicekit.errors import VoicekitError
from voicekit.runtimes.pipecat.admission import AdmissionController, AdmissionLease
from voicekit.runtimes.pipecat.flows import TransferHandler
from voicekit.runtimes.pipecat.lifecycle import (
    PipecatCall,
    PipecatCallLifecycle,
    PipecatLifecycleManager,
    PipecatRepository,
)
from voicekit.runtimes.pipecat.session import PipecatSession, PipecatSessionBuilder
from voicekit.storage.models import EndedReason
from voicekit.telephony.models import (
    CallEvent,
    PipecatTarget,
    RuntimeTarget,
    TelephonyRequest,
)


class TwilioRuntimeAdapter(Protocol):
    """Twilio operations the Pipecat host consumes without forcing the extra."""

    account_sid: str

    def verify_request(self, request: TelephonyRequest) -> bool: ...

    def answer_response(self, target: RuntimeTarget) -> str: ...

    def parse_event(self, request: TelephonyRequest) -> CallEvent: ...

    def resume_after_amd(
        self,
        call_sid: str,
        *,
        answered_by: str,
        target: RuntimeTarget,
        connect_machine: bool = False,
    ) -> str: ...

    def cold_transfer(self, call_sid: str, to_number: str) -> None: ...


class WebSessionAuthorizer(Protocol):
    """Authenticate and bind browser signaling without coupling to token format."""

    async def authorize(
        self,
        request: Request,
        *,
        pc_id: str | None,
    ) -> object: ...

    async def bind(
        self,
        identity: object,
        *,
        pc_id: str,
        call_id: str,
    ) -> None: ...

    async def cancel(self, identity: object) -> None: ...

    async def reserved_call_id(self, identity: object) -> str: ...

    async def release(self, pc_id: str) -> None: ...


@dataclass(frozen=True, slots=True)
class PipecatHostSettings:
    """Non-secret public routing plus private media credentials."""

    public_base: str
    twilio_account_sid: str = field(repr=False)
    twilio_auth_token: str = field(repr=False)
    pending_media_timeout_s: float = 30
    web_sample_rate: int = 16000
    twilio_sample_rate: int = 8000
    allow_insecure_web_sessions_for_tests: bool = False
    storage_ready: bool = True

    @classmethod
    def from_env(cls, public_base: str) -> PipecatHostSettings:
        return cls(
            public_base=public_base,
            twilio_account_sid=os.environ.get("TWILIO_ACCOUNT_SID", ""),
            twilio_auth_token=os.environ.get("TWILIO_AUTH_TOKEN", ""),
        )

    def __post_init__(self) -> None:
        if not self.public_base.startswith("https://"):
            raise VoicekitError(
                "VK-RUN-002",
                detail="Pipecat public_base must be HTTPS.",
            )
        if self.pending_media_timeout_s <= 0:
            raise VoicekitError(
                "VK-RUN-002",
                detail="pending media timeout must be positive.",
            )
        if self.web_sample_rate < 8000 or self.twilio_sample_rate != 8000:
            raise VoicekitError(
                "VK-RUN-002",
                detail="web audio must be at least 8kHz and Twilio must be exactly 8kHz.",
            )


@dataclass(slots=True)
class _PendingCall:
    call: PipecatCall
    admission: AdmissionLease
    lifecycle: PipecatCallLifecycle
    expires: asyncio.Task[None] | None = None


@dataclass(frozen=True, slots=True)
class DrainReport:
    """Bounded shutdown result exposed to the production supervisor."""

    pending_at_start: int
    active_at_start: int
    forced_sessions: int
    remaining_calls: int


class _TwilioTransfer(TransferHandler):
    def __init__(self, adapter: TwilioRuntimeAdapter) -> None:
        self._adapter = adapter

    async def __call__(self, call_id: str, number: str) -> None:
        await asyncio.to_thread(self._adapter.cold_transfer, call_id, number)


class LongLivedRunner:
    """Own a WorkerRunner whose zero-worker state never ends the process."""

    def __init__(self, runner: WorkerRunner | None = None) -> None:
        self.runner = runner or WorkerRunner(handle_sigint=False, handle_sigterm=False)
        self._task: asyncio.Task[None] | None = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        if self.running:
            return
        self._task = asyncio.create_task(
            self.runner.run(auto_end=False),
            name="voicekit-pipecat-runner",
        )
        await asyncio.sleep(0)

    async def stop(self) -> None:
        if self._task is None:
            return
        await self.runner.end(reason="host shutdown")
        await self._task
        self._task = None


class PipecatHost:
    """Production host coordinating admission, storage, and native transports."""

    def __init__(
        self,
        *,
        agent: Agent,
        repository: PipecatRepository,
        settings: PipecatHostSettings,
        twilio: TwilioRuntimeAdapter | None = None,
        runner: WorkerRunner | None = None,
        request_handler: SmallWebRTCRequestHandler | None = None,
        session_builder: PipecatSessionBuilder | None = None,
        web_sessions: WebSessionAuthorizer | None = None,
    ) -> None:
        if agent.runtime != "pipecat":
            raise VoicekitError("VK-RUN-001", detail="PipecatHost requires runtime='pipecat'.")
        if agent.phone is not None and agent.phone.provider == "twilio" and twilio is None:
            raise VoicekitError(
                "VK-RUN-001",
                detail="Twilio phone config requires the voicekit[twilio] adapter.",
            )
        if (
            agent.web.enabled
            and web_sessions is None
            and not settings.allow_insecure_web_sessions_for_tests
        ):
            raise VoicekitError(
                "VK-WEB-001",
                detail="web signaling requires the P1.9 scoped session authorizer.",
            )
        self.agent = agent
        self.repository = repository
        self.settings = settings
        self.twilio = twilio
        self.target = PipecatTarget(https_base=settings.public_base)
        self.admission = AdmissionController(agent.limits.max_concurrent)
        self.lifecycle = PipecatLifecycleManager(repository, self.admission)
        self.runner_host = LongLivedRunner(runner)
        self.request_handler = request_handler or SmallWebRTCRequestHandler()
        self.session_builder = session_builder or PipecatSessionBuilder(
            repository,
            transfer_handler=None if twilio is None else _TwilioTransfer(twilio),
        )
        self.web_sessions = web_sessions
        self._pending: dict[str, _PendingCall] = {}
        self._active: dict[str, PipecatSession] = {}
        self._web_sessions: dict[str, str] = {}
        self._session_tasks: set[asyncio.Task[object]] = set()
        self._state_lock = asyncio.Lock()
        self._accepting = True
        self._idle = asyncio.Event()
        self._idle.set()
        self.app = self._build_app()

    @property
    def accepting(self) -> bool:
        """Whether answer/token paths may expose a new call."""
        return self._accepting

    async def reload_agent(self, agent: Agent, *, restart_runner: bool) -> bool:
        """Atomically apply a revision only when no call owns runtime capacity."""
        if agent.runtime != "pipecat":
            raise VoicekitError(
                "VK-WEB-005",
                detail="hot reload cannot change the runtime away from Pipecat.",
            )
        if (
            agent.name != self.agent.name
            or agent.phone != self.agent.phone
            or agent.results != self.agent.results
            or agent.web.enabled != self.agent.web.enabled
        ):
            raise VoicekitError(
                "VK-WEB-005",
                detail=(
                    "agent identity, phone, results, and channel changes require "
                    "restarting voicekit dev."
                ),
            )
        async with self._state_lock:
            if self.admission.active_count:
                return False
            runner_was_running = self.runner_host.running
            if restart_runner and runner_was_running:
                await self.runner_host.stop()
            self.agent = agent
            self.admission = AdmissionController(agent.limits.max_concurrent)
            self.lifecycle = PipecatLifecycleManager(self.repository, self.admission)
            if restart_runner and runner_was_running:
                await self.runner_host.start()
            return True

    async def reserve_call(self, call: PipecatCall) -> _PendingCall:
        """Reserve capacity and durable storage before returning an answer."""
        async with self._state_lock:
            existing = self._pending.get(call.call_id)
            if existing is not None:
                return existing
            if not self._accepting:
                raise VoicekitError(
                    "VK-RUN-008",
                    detail="the runtime is draining and no longer accepts new calls.",
                )
            admission = await self.admission.acquire(call.call_id)
            lifecycle = await self.lifecycle.begin(self.agent, call, admission)
            pending = _PendingCall(
                call=call,
                admission=admission,
                lifecycle=lifecycle,
            )
            pending.expires = asyncio.create_task(
                self._expire_pending(pending),
                name=f"voicekit-pending-{call.call_id}",
            )
            self._pending[call.call_id] = pending
            self._idle.clear()
            return pending

    async def begin_drain(self) -> None:
        """Atomically close admission while preserving already-visible calls."""
        async with self._state_lock:
            self._accepting = False

    async def drain(self, *, timeout_s: float | None = None) -> DrainReport:
        """Finish admitted calls, then force the bounded duration limit if needed."""
        if timeout_s is not None and timeout_s <= 0:
            raise VoicekitError("VK-RUN-008", detail="drain timeout must be positive.")
        await self.begin_drain()
        async with self._state_lock:
            pending_at_start = len(self._pending)
            active_at_start = len(self._active)
        timeout = float(self.agent.limits.max_duration_s) if timeout_s is None else timeout_s
        forced = 0
        try:
            await asyncio.wait_for(self._wait_until_idle(), timeout=timeout)
        except TimeoutError:
            async with self._state_lock:
                sessions = tuple(self._active.values())
                pending_ids = tuple(self._pending)
            forced = len(sessions) + len(pending_ids)
            await asyncio.gather(
                *(session.end("duration_limit") for session in sessions),
                return_exceptions=True,
            )
            await asyncio.gather(
                *(self._finish_pending(call_id, "duration_limit") for call_id in pending_ids),
                return_exceptions=True,
            )
            with suppress(TimeoutError):
                await asyncio.wait_for(self._wait_until_idle(), timeout=5)
        return DrainReport(
            pending_at_start=pending_at_start,
            active_at_start=active_at_start,
            forced_sessions=forced,
            remaining_calls=self.admission.active_count,
        )

    async def _wait_until_idle(self) -> None:
        if self.admission.active_count:
            await self._idle.wait()

    async def reserve_web_call(self) -> str:
        """Create the durable web call before its browser token is returned."""
        call = PipecatCall(
            call_id=f"call_web_{uuid.uuid4().hex}",
            channel="web",
            direction="inbound",
        )
        await self.reserve_call(call)
        return call.call_id

    async def _expire_pending(self, pending: _PendingCall) -> None:
        await asyncio.sleep(self.settings.pending_media_timeout_s)
        async with self._state_lock:
            if self._pending.get(pending.call.call_id) is not pending:
                return
            del self._pending[pending.call.call_id]
        await pending.lifecycle.fail_setup()
        self._mark_idle_if_empty()

    async def _claim_pending(self, call_id: str, token: str) -> _PendingCall:
        await self.admission.claim(call_id, token)
        async with self._state_lock:
            try:
                pending = self._pending.pop(call_id)
            except KeyError as exc:
                raise VoicekitError(
                    "VK-RUN-005",
                    detail=f"no pending media reservation for {call_id}.",
                ) from exc
        if pending.expires is not None:
            pending.expires.cancel()
            with suppress(asyncio.CancelledError):
                await pending.expires
            pending.expires = None
        return pending

    def _build_app(self) -> FastAPI:
        @asynccontextmanager
        async def lifespan(_app: FastAPI) -> AsyncGenerator[None, None]:
            await self.runner_host.start()
            try:
                yield
            finally:
                await self.drain()
                await self.runner_host.stop()
                if self._session_tasks:
                    await asyncio.gather(*self._session_tasks, return_exceptions=True)

        app = FastAPI(title=f"voicekit:{self.agent.name}", lifespan=lifespan)

        @app.exception_handler(VoicekitError)
        async def voicekit_error_handler(  # pyright: ignore[reportUnusedFunction]
            _request: Request,
            error: VoicekitError,
        ) -> JSONResponse:
            status = {
                "VK-RUN-004": 429,
                "VK-RUN-008": 503,
                "VK-WEB-001": 401,
                "VK-WEB-002": 403,
                "VK-WEB-003": 429,
                "VK-WEB-004": 403,
            }.get(error.code, 400)
            return JSONResponse(
                status_code=status,
                content={"error": {"code": error.code, "message": str(error)}},
            )

        @app.get("/health")
        async def health() -> JSONResponse:  # pyright: ignore[reportUnusedFunction]
            ready = self.runner_host.running and self._accepting and self.settings.storage_ready
            return JSONResponse(
                status_code=200 if ready else 503,
                content={
                    "ok": ready,
                    "runtime": "pipecat",
                    "active_calls": self.admission.active_count,
                    "accepting": self._accepting,
                    "storage_ready": self.settings.storage_ready,
                },
            )

        @app.post("/twilio/answer")
        async def twilio_answer(  # pyright: ignore[reportUnusedFunction]
            request: Request,
        ) -> Response:
            adapter = self._require_twilio()
            form = await request.form()
            telephony = _http_telephony_request(request, form)
            if not adapter.verify_request(telephony):
                return _error_response("VK-RUN-007", 403)
            event = adapter.parse_event(telephony)
            values = _string_mapping(form)
            try:
                pending = await self.reserve_call(
                    PipecatCall(
                        call_id=event.provider_call_id,
                        channel="phone",
                        direction="inbound",
                        provider="twilio",
                        provider_call_id=event.provider_call_id,
                        from_number=values.get("From"),
                        to_number=values.get("To"),
                    )
                )
            except VoicekitError as exc:
                if exc.code not in {"VK-RUN-004", "VK-RUN-008"}:
                    raise
                return Response(
                    content='<Response><Reject reason="busy" /></Response>',
                    status_code=200,
                    media_type="application/xml",
                )
            target = replace(
                self.target,
                custom_parameters={
                    "voicekit_token": pending.admission.token,
                    "from_number": pending.call.from_number or "",
                    "to_number": pending.call.to_number or "",
                },
            )
            return Response(
                content=adapter.answer_response(target),
                media_type="application/xml",
            )

        @app.websocket("/twilio/media")
        async def twilio_media(  # pyright: ignore[reportUnusedFunction]
            websocket: WebSocket,
        ) -> None:
            adapter = self._require_twilio()
            if not adapter.verify_request(_websocket_telephony_request(websocket)):
                await websocket.close(code=1008, reason="VK-RUN-007")
                return
            await websocket.accept()
            pending: _PendingCall | None = None
            try:
                parsed = cast(
                    "tuple[str, CallData]",
                    await parse_telephony_websocket(websocket),
                )
                transport_type, call_data = parsed
                if transport_type != "twilio":
                    raise VoicekitError(
                        "VK-RUN-007",
                        detail=f"expected Twilio media; received {transport_type!r}.",
                    )
                call_id, stream_id, token = _twilio_handshake(call_data)
                pending = await self._claim_pending(call_id, token)
                transport = FastAPIWebsocketTransport(
                    websocket=websocket,
                    params=twilio_transport_params(
                        settings=self.settings,
                        call_id=call_id,
                        stream_id=stream_id,
                        max_duration_s=self.agent.limits.max_duration_s,
                    ),
                )
                session = self.session_builder.build(
                    agent=self.agent,
                    call=pending.call,
                    lifecycle=pending.lifecycle,
                    transport=transport,
                    sample_rate=self.settings.twilio_sample_rate,
                )
                async with self._state_lock:
                    self._active[call_id] = session
                await session.start(self.runner_host.runner)
                await session.wait()
            except Exception:
                if pending is not None and pending.lifecycle.terminal_event is None:
                    await pending.lifecycle.fail_setup()
                with suppress(RuntimeError):
                    await websocket.close(code=1011, reason="VK-RUN-006")
            finally:
                if pending is not None:
                    async with self._state_lock:
                        self._active.pop(pending.call.call_id, None)
                    self._mark_idle_if_empty()

        @app.post("/twilio/events")
        @app.post("/twilio/events/{intent_id}")
        async def twilio_events(  # pyright: ignore[reportUnusedFunction]
            request: Request,
            intent_id: str | None = None,
        ) -> Response:
            adapter = self._require_twilio()
            form = await request.form()
            telephony = _http_telephony_request(
                request,
                form,
                route_params={} if intent_id is None else {"intent_id": intent_id},
            )
            if not adapter.verify_request(telephony):
                return _error_response("VK-RUN-007", 403)
            event = adapter.parse_event(telephony)
            if event.ended_reason is not None:
                await self._end_from_provider(event)
            return Response(status_code=204)

        @app.post("/twilio/amd")
        async def twilio_amd(  # pyright: ignore[reportUnusedFunction]
            request: Request,
        ) -> Response:
            adapter = self._require_twilio()
            form = await request.form()
            telephony = _http_telephony_request(request, form)
            if not adapter.verify_request(telephony):
                return _error_response("VK-RUN-007", 403)
            event = adapter.parse_event(telephony)
            if event.answered_by is None:
                return _error_response("VK-RUN-007", 400)
            disposition = await asyncio.to_thread(
                adapter.resume_after_amd,
                event.provider_call_id,
                answered_by=event.answered_by,
                target=self.target,
                connect_machine=self.agent.behavior.voicemail == "leave_message",
            )
            if disposition == "hung_up":
                await self._finish_pending(event.provider_call_id, "voicemail")
            return Response(status_code=204)

        @app.post("/api/offer")
        async def web_offer(  # pyright: ignore[reportUnusedFunction]
            request: Request,
            body: dict[str, Any],
        ) -> dict[str, str]:
            try:
                offer = SmallWebRTCRequest.from_dict(dict(body))
            except (TypeError, ValueError) as exc:
                raise VoicekitError(
                    "VK-RUN-007",
                    detail="invalid SmallWebRTC offer payload.",
                ) from exc
            identity = await self._authorize_web(request, pc_id=offer.pc_id)
            if offer.pc_id is not None and offer.pc_id in self._web_sessions:
                answer = await self.request_handler.handle_web_request(offer, _no_new_connection)
                if answer is None:
                    raise VoicekitError("VK-RUN-007", detail="WebRTC renegotiation had no answer.")
                return answer

            pending: _PendingCall | None = None
            try:
                if identity is None:
                    call_id = await self.reserve_web_call()
                else:
                    call_id = await self._reserved_web_call(identity)
                async with self._state_lock:
                    pending = self._pending.get(call_id)
                if pending is None:
                    raise VoicekitError(
                        "VK-RUN-005",
                        detail=f"no pending browser reservation for {call_id}.",
                    )
                call = pending.call
                callback_error: list[Exception] = []

                async def start_connection(connection: Any) -> None:
                    pc_id = str(connection.pc_id)
                    try:
                        transport = SmallWebRTCTransport(
                            connection,
                            TransportParams(
                                audio_in_enabled=True,
                                audio_out_enabled=True,
                                audio_in_sample_rate=self.settings.web_sample_rate,
                                audio_out_sample_rate=self.settings.web_sample_rate,
                            ),
                        )
                        claimed = await self._claim_pending(
                            call.call_id,
                            pending.admission.token,
                        )
                        session = self.session_builder.build(
                            agent=self.agent,
                            call=call,
                            lifecycle=claimed.lifecycle,
                            transport=transport,
                            sample_rate=self.settings.web_sample_rate,
                        )
                        await self._bind_web(identity, pc_id=pc_id, call_id=call.call_id)
                        async with self._state_lock:
                            self._active[call.call_id] = session
                            self._web_sessions[pc_id] = call.call_id
                        await session.start(self.runner_host.runner)
                        self._track_session(
                            asyncio.create_task(
                                self._wait_web_session(pc_id, session),
                                name=f"voicekit-web-{call.call_id}",
                            )
                        )
                    except Exception as exc:
                        callback_error.append(exc)
                        await pending.lifecycle.fail_setup()
                        self._mark_idle_if_empty()
                        await self._release_web(pc_id)
                        with suppress(Exception):
                            await connection.disconnect()

                answer = await self.request_handler.handle_web_request(offer, start_connection)
                if callback_error:
                    raise VoicekitError(
                        "VK-RUN-006",
                        detail=f"web session setup failed: {type(callback_error[0]).__name__}.",
                    )
                if answer is None:
                    raise VoicekitError("VK-RUN-007", detail="WebRTC offer produced no answer.")
                return answer
            except Exception:
                await self._cancel_web(identity)
                if pending is not None:
                    await self._fail_pending_setup(pending)
                raise

        @app.patch("/api/offer")
        async def web_ice_patch(  # pyright: ignore[reportUnusedFunction]
            request: Request,
            body: dict[str, Any],
        ) -> Response:
            try:
                candidates = [
                    IceCandidate(
                        candidate=str(candidate["candidate"]),
                        sdp_mid=str(candidate.get("sdpMid", candidate.get("sdp_mid", ""))),
                        sdp_mline_index=int(
                            candidate.get(
                                "sdpMLineIndex",
                                candidate.get("sdp_mline_index", 0),
                            )
                        ),
                    )
                    for candidate in cast("list[dict[str, Any]]", body["candidates"])
                ]
                patch = SmallWebRTCPatchRequest(
                    pc_id=str(body.get("pc_id", body.get("pcId"))),
                    candidates=candidates,
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise VoicekitError(
                    "VK-RUN-007",
                    detail="invalid SmallWebRTC ICE payload.",
                ) from exc
            await self._authorize_web(request, pc_id=patch.pc_id)
            await self.request_handler.handle_patch_request(patch)
            return Response(status_code=204)

        return app

    async def _wait_web_session(self, pc_id: str, session: PipecatSession) -> object:
        try:
            return await session.wait()
        finally:
            async with self._state_lock:
                self._active.pop(session.call.call_id, None)
                self._web_sessions.pop(pc_id, None)
            self._mark_idle_if_empty()
            await self._release_web(pc_id)

    def _track_session(self, task: asyncio.Task[object]) -> None:
        self._session_tasks.add(task)
        task.add_done_callback(self._session_tasks.discard)

    async def _authorize_web(self, request: Request, *, pc_id: str | None) -> object | None:
        if self.web_sessions is None:
            if self.settings.allow_insecure_web_sessions_for_tests:
                return None
            raise VoicekitError("VK-WEB-001", detail="browser session security is unavailable.")
        return await self.web_sessions.authorize(request, pc_id=pc_id)

    async def _bind_web(
        self,
        identity: object | None,
        *,
        pc_id: str,
        call_id: str,
    ) -> None:
        if self.web_sessions is not None and identity is not None:
            await self.web_sessions.bind(identity, pc_id=pc_id, call_id=call_id)

    async def _cancel_web(self, identity: object | None) -> None:
        if self.web_sessions is not None and identity is not None:
            await self.web_sessions.cancel(identity)

    async def _reserved_web_call(self, identity: object) -> str:
        if self.web_sessions is None:
            raise VoicekitError("VK-WEB-001", detail="browser session security is unavailable.")
        return await self.web_sessions.reserved_call_id(identity)

    async def _fail_pending_setup(self, pending: _PendingCall) -> None:
        async with self._state_lock:
            if self._pending.get(pending.call.call_id) is not pending:
                return
            del self._pending[pending.call.call_id]
        if pending.expires is not None:
            pending.expires.cancel()
            with suppress(asyncio.CancelledError):
                await pending.expires
            pending.expires = None
        await pending.lifecycle.fail_setup()
        self._mark_idle_if_empty()

    async def _release_web(self, pc_id: str) -> None:
        if self.web_sessions is not None:
            await self.web_sessions.release(pc_id)

    async def _end_from_provider(self, event: CallEvent) -> None:
        async with self._state_lock:
            session = self._active.get(event.provider_call_id)
        if session is not None:
            await session.end(event.ended_reason or "provider_hangup")
            return
        await self._finish_pending(
            event.provider_call_id,
            event.ended_reason or "provider_hangup",
        )

    async def _finish_pending(self, call_id: str, reason: EndedReason) -> None:
        async with self._state_lock:
            pending = self._pending.pop(call_id, None)
        if pending is None:
            return
        if pending.expires is not None:
            pending.expires.cancel()
            with suppress(asyncio.CancelledError):
                await pending.expires
        await pending.lifecycle.finish(reason, provider_state="completed")
        self._mark_idle_if_empty()

    def _mark_idle_if_empty(self) -> None:
        if self.admission.active_count == 0:
            self._idle.set()

    def _require_twilio(self) -> TwilioRuntimeAdapter:
        if self.twilio is None:
            raise VoicekitError("VK-RUN-001", detail="Twilio adapter is not configured.")
        return self.twilio


async def _no_new_connection(_connection: Any) -> None:
    return None


def twilio_transport_params(
    *,
    settings: PipecatHostSettings,
    call_id: str,
    stream_id: str,
    max_duration_s: int,
) -> FastAPIWebsocketParams:
    """Build the certified 8kHz serializer/transport parameter pair."""
    serializer = TwilioFrameSerializer(
        stream_sid=stream_id,
        call_sid=call_id,
        account_sid=settings.twilio_account_sid,
        auth_token=settings.twilio_auth_token,
        params=TwilioFrameSerializer.InputParams(
            twilio_sample_rate=8000,
            sample_rate=8000,
            auto_hang_up=True,
        ),
    )
    return FastAPIWebsocketParams(
        audio_in_enabled=True,
        audio_out_enabled=True,
        audio_in_sample_rate=settings.twilio_sample_rate,
        audio_out_sample_rate=settings.twilio_sample_rate,
        add_wav_header=False,
        serializer=serializer,
        session_timeout=max_duration_s + 30,
        allowed_origins=[],
    )


def _twilio_handshake(call_data: CallData) -> tuple[str, str, str]:
    call_id = call_data.call_id
    stream_id = call_data.stream_id
    body = cast("dict[str, Any]", call_data.body)
    token = str(body.get("voicekit_token", ""))
    if not call_id or not stream_id or not token:
        raise VoicekitError(
            "VK-RUN-005",
            detail="Twilio start frame lacks call, stream, or reservation data.",
        )
    return call_id, stream_id, token


def _http_telephony_request(
    request: Request,
    form: object,
    *,
    route_params: dict[str, str] | None = None,
) -> TelephonyRequest:
    return TelephonyRequest(
        scheme=request.url.scheme,
        host=request.url.netloc,
        path=request.url.path,
        headers=dict(request.headers),
        query_string=request.url.query,
        form=form,
        peer_host=None if request.client is None else request.client.host,
        route_params=route_params or {},
    )


def _websocket_telephony_request(websocket: WebSocket) -> TelephonyRequest:
    return TelephonyRequest(
        scheme=websocket.url.scheme,
        host=websocket.url.netloc,
        path=websocket.url.path,
        headers=dict(websocket.headers),
        query_string=websocket.url.query,
        peer_host=None if websocket.client is None else websocket.client.host,
        is_websocket=True,
    )


def _string_mapping(form: object) -> dict[str, str]:
    if isinstance(form, Mapping):
        values = cast("Mapping[object, object]", form)
        return {str(key): str(value) for key, value in values.items()}
    return {}


def _error_response(code: str, status: int) -> JSONResponse:
    error = VoicekitError(code)
    return JSONResponse(
        status_code=status,
        content={"error": {"code": error.code, "message": str(error)}},
    )
