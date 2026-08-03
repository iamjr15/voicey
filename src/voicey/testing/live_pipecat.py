"""Native Pipecat caller behind a signed Twilio Media Stream."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, cast

import uvicorn
from fastapi import FastAPI, Request, Response, WebSocket
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.frames.frames import EndFrame, LLMRunFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    AssistantTurnStoppedMessage,
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
    UserTurnMessageAddedMessage,
)
from pipecat.processors.frame_processor import FrameProcessor
from pipecat.runner.types import CallData
from pipecat.runner.utils import parse_telephony_websocket
from pipecat.serializers.twilio import TwilioFrameSerializer
from pipecat.transports.websocket.fastapi import (
    FastAPIWebsocketParams,
    FastAPIWebsocketTransport,
)

from voicey.config.models import Voice
from voicey.errors import VoiceyError
from voicey.runtimes.pipecat.host import LongLivedRunner
from voicey.runtimes.pipecat.providers import DefaultProviderFactory, ProviderFactory
from voicey.telephony.models import CallEvent, PipecatTarget, TelephonyRequest
from voicey.telephony.twilio import TwilioAdapter
from voicey.testing.live import LiveCallEvidence, LiveCallPlan, LiveEnvironment
from voicey.testing.models import LiveTestingConfig
from voicey.tunnel import TunnelHandle, TunnelManager


class LiveTwilioAdapter(Protocol):
    account_sid: str

    @property
    def auth_token(self) -> str: ...

    def verify_request(self, request: TelephonyRequest) -> bool: ...

    def parse_event(self, request: TelephonyRequest) -> CallEvent: ...

    def start_call(
        self,
        from_no: str,
        to_no: str,
        target: PipecatTarget,
        *,
        intent_id: str | None = None,
        amd: bool = False,
        send_digits: str | None = None,
        record: bool = False,
        timeout_s: int = 30,
    ) -> str: ...

    def hangup(self, call_sid: str) -> None: ...


@dataclass(slots=True)
class _CallState:
    plan: LiveCallPlan
    transcript: list[str] = field(default_factory=list[str])
    provider_call_id: str = ""
    terminal_status: str = "queued"
    error_type: str | None = None
    terminal: asyncio.Event = field(default_factory=asyncio.Event)
    media_done: asyncio.Event = field(default_factory=asyncio.Event)
    heard_target: asyncio.Event = field(default_factory=asyncio.Event)
    tasks: set[asyncio.Task[None]] = field(default_factory=set[asyncio.Task[None]])


class PipecatTwilioPstnBackend:
    """Own the callback server, tunnel, durable Twilio dial, and caller workers."""

    def __init__(
        self,
        *,
        root: Path,
        config: LiveTestingConfig,
        live: LiveEnvironment,
        environment: Mapping[str, str],
        adapter_factory: Callable[[str], LiveTwilioAdapter] | None = None,
        tunnel_manager: TunnelManager | None = None,
        provider_factory: ProviderFactory | None = None,
        runner_host: LongLivedRunner | None = None,
        server_factory: Callable[[FastAPI, int], Any] | None = None,
        worker_builder: Callable[[_CallState, FastAPIWebsocketTransport], Any] | None = None,
    ) -> None:
        if live.runtime != "pipecat" or live.twilio_from_number is None:
            raise VoiceyError("VY-TST-003", detail="invalid Pipecat live-test configuration.")
        self._root = root
        self._config = config
        self._live = live
        self._environment = dict(environment)
        self._adapter_factory = adapter_factory or self._default_adapter
        self._tunnels = tunnel_manager or TunnelManager(environment=self._environment)
        self._providers = provider_factory or DefaultProviderFactory(self._environment)
        self._runner = runner_host or LongLivedRunner()
        self._server_factory = server_factory or _uvicorn_server
        self._worker_builder = worker_builder
        self._adapter: LiveTwilioAdapter | None = None
        self._server: Any | None = None
        self._server_task: asyncio.Task[None] | None = None
        self._tunnel: TunnelHandle | None = None
        self._states: dict[str, _CallState] = {}
        self._runner_started = False
        self._started = False
        self._closed = False
        self.app = FastAPI()
        self._mount_routes()

    async def run_call(self, plan: LiveCallPlan) -> LiveCallEvidence:
        if self._closed:
            raise VoiceyError("VY-TST-003", detail="Pipecat live caller is already closed.")
        await self._ensure_started()
        adapter = self._require_adapter()
        public_url = self._require_tunnel().public_url
        state = _CallState(plan=plan)
        self._states[plan.run_id] = state
        target = PipecatTarget(
            https_base=public_url,
            ws_path="/live/twilio/media",
            answer_path="/live/twilio/unused",
            event_path="/live/twilio/events",
            recording_path="/live/twilio/recordings",
            amd_path="/live/twilio/amd",
            custom_parameters={"run_id": plan.run_id},
        )
        started = time.monotonic()
        try:
            call_sid = await asyncio.to_thread(
                adapter.start_call,
                cast(str, self._live.twilio_from_number),
                self._live.target_number,
                target,
                intent_id=plan.run_id,
                timeout_s=self._config.answer_timeout_s,
            )
            state.provider_call_id = call_sid
            try:
                await asyncio.wait_for(
                    state.terminal.wait(),
                    timeout=float(plan.max_duration_s + self._config.answer_timeout_s + 15),
                )
            except TimeoutError:
                state.terminal_status = "timeout"
                with suppress(Exception):
                    await asyncio.to_thread(adapter.hangup, call_sid)
            with suppress(TimeoutError):
                await asyncio.wait_for(state.media_done.wait(), timeout=15)
            if state.error_type is not None and state.terminal_status == "completed":
                state.terminal_status = "media-error"
            return LiveCallEvidence(
                transcript=tuple(state.transcript),
                duration_ms=int((time.monotonic() - started) * 1000),
                terminal_status=state.terminal_status,
                provider="twilio",
                path="pipecat-native-caller-twilio-pstn",
                provider_call_id=state.provider_call_id,
                runtime_call_id=plan.run_id,
            )
        except VoiceyError:
            raise
        except Exception as exc:
            raise VoiceyError(
                "VY-TST-003",
                detail=(
                    "Twilio could not establish or execute the acknowledged PSTN call; "
                    f"provider error type {type(exc).__name__}."
                ),
            ) from exc
        finally:
            tasks = tuple(state.tasks)
            for task in tasks:
                task.cancel()
            for task in tasks:
                with suppress(asyncio.CancelledError):
                    await task
            self._states.pop(plan.run_id, None)

    async def aclose(self) -> None:
        if self._closed:
            return
        for state in tuple(self._states.values()):
            if state.provider_call_id and self._adapter is not None:
                with suppress(Exception):
                    await asyncio.to_thread(self._adapter.hangup, state.provider_call_id)
        await self._shutdown_services()
        self._closed = True

    async def _shutdown_services(self) -> None:
        if self._runner_started:
            await self._runner.stop()
            self._runner_started = False
        if self._tunnel is not None:
            await self._tunnel.close()
            self._tunnel = None
        if self._server is not None:
            self._server.should_exit = True
        if self._server_task is not None:
            with suppress(TimeoutError):
                await asyncio.wait_for(self._server_task, timeout=10)
            if not self._server_task.done():
                self._server_task.cancel()
                with suppress(asyncio.CancelledError):
                    await self._server_task
        self._server_task = None
        self._server = None
        self._adapter = None
        self._started = False

    async def _ensure_started(self) -> None:
        if self._started:
            return
        try:
            server = self._server_factory(self.app, self._config.port)
            self._server = server
            self._server_task = asyncio.create_task(
                server.serve(),
                name="voicey-live-pstn-callback-server",
            )
            deadline = asyncio.get_running_loop().time() + 10
            while not server.started:
                if self._server_task.done() or asyncio.get_running_loop().time() >= deadline:
                    raise VoiceyError(
                        "VY-TST-003",
                        detail="live PSTN callback server did not start; no call was placed.",
                    )
                await asyncio.sleep(0.05)
            preference = "url" if self._live.public_url is not None else self._config.tunnel
            self._tunnel = await self._tunnels.open(
                self._config.port,
                preference=preference,
                public_url=self._live.public_url,
                cloudflared_protocol="http2",
            )
            self._adapter = self._adapter_factory(self._tunnel.public_url)
            await self._runner.start()
            self._runner_started = True
            self._started = True
        except VoiceyError:
            await self._shutdown_services()
            raise
        except Exception as exc:
            await self._shutdown_services()
            raise VoiceyError(
                "VY-TST-003",
                detail=(
                    "live PSTN caller services did not start; no call was placed; "
                    f"failure type {type(exc).__name__}."
                ),
            ) from exc

    def _mount_routes(self) -> None:
        @self.app.websocket("/live/twilio/media")
        async def twilio_media(  # pyright: ignore[reportUnusedFunction]
            websocket: WebSocket,
        ) -> None:
            adapter = self._require_adapter()
            if not adapter.verify_request(_websocket_request(websocket)):
                await websocket.close(code=1008, reason="VY-TST-003")
                return
            await websocket.accept()
            state: _CallState | None = None
            try:
                parsed = cast(
                    "tuple[str, CallData]",
                    await parse_telephony_websocket(websocket),
                )
                transport_type, call_data = parsed
                if transport_type != "twilio":
                    raise VoiceyError(
                        "VY-TST-003",
                        detail=f"live caller expected Twilio media, received {transport_type!r}.",
                    )
                run_id = str(cast("dict[str, Any]", call_data.body).get("run_id", ""))
                state = self._states.get(run_id)
                if state is None or not call_data.call_id or not call_data.stream_id:
                    raise VoiceyError(
                        "VY-TST-003",
                        detail="live Twilio media has no matching paid-call reservation.",
                    )
                if state.provider_call_id and state.provider_call_id != call_data.call_id:
                    raise VoiceyError(
                        "VY-TST-003",
                        detail="live Twilio media call id does not match its reservation.",
                    )
                transport = FastAPIWebsocketTransport(
                    websocket=websocket,
                    params=_transport_params(
                        adapter=adapter,
                        call_id=call_data.call_id,
                        stream_id=call_data.stream_id,
                        max_duration_s=state.plan.max_duration_s,
                    ),
                )
                worker = (
                    self._build_worker(state, transport)
                    if self._worker_builder is None
                    else self._worker_builder(state, transport)
                )
                await self._runner.runner.add_workers(worker)
                await worker.wait()
            except Exception as exc:
                if state is not None:
                    state.error_type = type(exc).__name__
                with suppress(RuntimeError):
                    await websocket.close(code=1011, reason="VY-TST-003")
            finally:
                if state is not None:
                    state.media_done.set()

        @self.app.post("/live/twilio/events/{intent_id}")
        async def twilio_event(  # pyright: ignore[reportUnusedFunction]
            request: Request,
            intent_id: str,
        ) -> Response:
            adapter = self._require_adapter()
            form = await request.form()
            telephony = _http_request(
                request,
                form,
                route_params={"intent_id": intent_id},
            )
            if not adapter.verify_request(telephony):
                return Response(status_code=403)
            try:
                event = adapter.parse_event(telephony)
            except VoiceyError:
                return Response(status_code=400)
            state = self._states.get(intent_id)
            if state is None or (
                state.provider_call_id and state.provider_call_id != event.provider_call_id
            ):
                return Response(status_code=404)
            if not state.provider_call_id:
                state.provider_call_id = event.provider_call_id
            state.terminal_status = event.provider_status
            if event.ended_reason is not None:
                state.terminal.set()
            return Response(status_code=204)

    def _build_worker(
        self,
        state: _CallState,
        transport: FastAPIWebsocketTransport,
    ) -> PipelineWorker:
        voice = Voice(language="en")
        stt = self._providers.create_stt("deepgram/nova-3", voice, 8000)
        llm = self._providers.create_llm("anthropic/claude-sonnet-5", state.plan.prompt)
        tts = self._providers.create_tts("cartesia/sonic-3.5", voice, 8000)
        aggregators = LLMContextAggregatorPair(
            LLMContext(messages=[{"role": "system", "content": state.plan.prompt}]),
            user_params=LLMUserAggregatorParams(
                user_idle_timeout=min(30.0, float(state.plan.max_duration_s)),
                vad_analyzer=SileroVADAnalyzer(sample_rate=8000),
            ),
        )

        @aggregators.user().event_handler("on_user_turn_message_added")
        async def on_target_turn(  # pyright: ignore[reportUnusedFunction]
            _aggregator: object,
            message: UserTurnMessageAddedMessage,
        ) -> None:
            if message.content:
                state.transcript.append(f"agent: {message.content}")
                state.heard_target.set()

        worker_holder: dict[str, PipelineWorker] = {}

        @aggregators.assistant().event_handler("on_assistant_turn_stopped")
        async def on_caller_turn(  # pyright: ignore[reportUnusedFunction]
            _aggregator: object,
            message: AssistantTurnStoppedMessage,
        ) -> None:
            if not message.content:
                return
            state.transcript.append(f"caller: {message.content}")
            if "thank you, goodbye" in message.content.casefold():

                async def end_after_playout() -> None:
                    await asyncio.sleep(1)
                    await worker_holder["worker"].queue_frame(EndFrame(reason="caller-finished"))

                _spawn(
                    state,
                    end_after_playout(),
                    name=f"voicey-live-farewell-{state.plan.run_id}",
                )

        pipeline = Pipeline(
            [
                transport.input(),
                stt,
                aggregators.user(),
                cast(FrameProcessor, llm),
                tts,
                transport.output(),
                aggregators.assistant(),
            ]
        )
        worker = PipelineWorker(
            pipeline,
            params=PipelineParams(
                audio_in_sample_rate=8000,
                audio_out_sample_rate=8000,
                enable_metrics=True,
                enable_usage_metrics=True,
            ),
            idle_timeout_secs=float(state.plan.max_duration_s),
            cancel_on_idle_timeout=True,
            cancel_runner_on_idle_timeout=False,
            enable_turn_tracking=True,
            enable_rtvi=False,
        )
        worker_holder["worker"] = worker

        @transport.event_handler("on_client_connected")
        async def on_connected(  # pyright: ignore[reportUnusedFunction]
            _transport: object,
            _client: object,
        ) -> None:
            async def start_if_silent() -> None:
                try:
                    await asyncio.wait_for(state.heard_target.wait(), timeout=3)
                except TimeoutError:
                    await worker.queue_frame(LLMRunFrame())

            _spawn(
                state,
                start_if_silent(),
                name=f"voicey-live-opening-{state.plan.run_id}",
            )

        @transport.event_handler("on_client_disconnected")
        async def on_disconnected(  # pyright: ignore[reportUnusedFunction]
            _transport: object,
            _client: object,
        ) -> None:
            await worker.queue_frame(EndFrame(reason="carrier-disconnected"))

        return worker

    def _default_adapter(self, public_url: str) -> LiveTwilioAdapter:
        data_dir = self._root / ".voicey"
        data_dir.mkdir(parents=True, exist_ok=True)
        return TwilioAdapter(
            account_sid=self._environment["TWILIO_ACCOUNT_SID"],
            auth_token=self._environment["TWILIO_AUTH_TOKEN"],
            ledger_path=data_dir / "live-pstn-telephony.sqlite3",
            expected_public_base=public_url,
        )

    def _require_adapter(self) -> LiveTwilioAdapter:
        if self._adapter is None:
            raise VoiceyError("VY-TST-003", detail="live Twilio callback server is not ready.")
        return self._adapter

    def _require_tunnel(self) -> TunnelHandle:
        if self._tunnel is None:
            raise VoiceyError("VY-TST-003", detail="live Twilio tunnel is not ready.")
        return self._tunnel


def _transport_params(
    *,
    adapter: LiveTwilioAdapter,
    call_id: str,
    stream_id: str,
    max_duration_s: int,
) -> FastAPIWebsocketParams:
    serializer = TwilioFrameSerializer(
        stream_sid=stream_id,
        call_sid=call_id,
        account_sid=adapter.account_sid,
        auth_token=adapter.auth_token,
        params=TwilioFrameSerializer.InputParams(
            twilio_sample_rate=8000,
            sample_rate=8000,
            auto_hang_up=True,
        ),
    )
    return FastAPIWebsocketParams(
        audio_in_enabled=True,
        audio_out_enabled=True,
        audio_in_sample_rate=8000,
        audio_out_sample_rate=8000,
        add_wav_header=False,
        serializer=serializer,
        session_timeout=max_duration_s + 30,
        allowed_origins=[],
    )


def _http_request(
    request: Request,
    form: object | None,
    *,
    route_params: dict[str, str],
) -> TelephonyRequest:
    return TelephonyRequest(
        scheme=request.url.scheme,
        host=request.url.netloc,
        path=request.url.path,
        headers=dict(request.headers),
        query_string=request.url.query,
        form=form,
        peer_host=None if request.client is None else request.client.host,
        route_params=route_params,
    )


def _websocket_request(websocket: WebSocket) -> TelephonyRequest:
    return TelephonyRequest(
        scheme=websocket.url.scheme,
        host=websocket.url.netloc,
        path=websocket.url.path,
        headers=dict(websocket.headers),
        query_string=websocket.url.query,
        peer_host=None if websocket.client is None else websocket.client.host,
        is_websocket=True,
    )


def _uvicorn_server(app: FastAPI, port: int) -> uvicorn.Server:
    return uvicorn.Server(
        uvicorn.Config(
            app,
            host="127.0.0.1",
            port=port,
            log_level="warning",
            proxy_headers=True,
            forwarded_allow_ips="127.0.0.1,::1",
        )
    )


def _spawn(
    state: _CallState,
    coroutine: Any,
    *,
    name: str,
) -> None:
    task = asyncio.create_task(coroutine, name=name)
    state.tasks.add(task)
    task.add_done_callback(state.tasks.discard)
