from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any, ClassVar, cast

import httpx
import pytest
from livekit.agents import ConversationItemAddedEvent
from livekit.agents.llm import ChatMessage
from pipecat.pipeline.worker import PipelineWorker
from pipecat.processors.frame_processor import FrameProcessor
from pipecat.runner.types import CallData
from starlette.datastructures import URL

from voicekit.config.manifest import ManifestStore
from voicekit.errors import VoicekitError
from voicekit.telephony.models import CallEvent, PipecatTarget, TelephonyRequest
from voicekit.testing import (
    LiveTestingConfig,
    ResultExpectation,
    ScenarioDefinition,
)
from voicekit.testing import (
    TestProfile as ScenarioProfile,
)
from voicekit.testing import live as live_testing
from voicekit.testing import live_pipecat as pipecat_live
from voicekit.testing.discovery import discover_scenarios
from voicekit.testing.live import (
    LiveCallEvidence,
    LiveCallPlan,
    LiveEnvironment,
    LivePstnExecutor,
    build_live_executor,
    caller_prompt,
    live_judge_criteria,
    validate_live_environment,
)
from voicekit.testing.live_livekit import LiveKitSipPstnBackend
from voicekit.testing.models import JudgeConfig, ScenarioTurn, TurnExpectation
from voicekit.testing.sim_caller import JudgeDecision, load_testing_config


def _base_environment() -> dict[str, str]:
    return {
        "VOICEKIT_LIVE_PSTN_ACK": "I_ACKNOWLEDGE_PAID_PSTN",
        "VOICEKIT_LIVE_PSTN_MAX_CALLS": "4",
        "VOICEKIT_LIVE_TARGET_NUMBER": "+14155550123",
        "DEEPGRAM_API_KEY": "deepgram-test",  # pragma: allowlist secret
        "ANTHROPIC_API_KEY": "anthropic-test",  # pragma: allowlist secret
        "CARTESIA_API_KEY": "cartesia-test",  # pragma: allowlist secret
        "TWILIO_ACCOUNT_SID": "AC" + "1" * 32,
        "TWILIO_AUTH_TOKEN": "twilio-test",  # pragma: allowlist secret
        "VOICEKIT_LIVE_TWILIO_FROM": "+14155550124",
        "LIVEKIT_URL": "wss://test.livekit.cloud",
        "LIVEKIT_API_KEY": "livekit-key",  # pragma: allowlist secret
        "LIVEKIT_API_SECRET": "livekit-secret-xxxxxxxxxxxxxxxxxxxxxxxx",  # pragma: allowlist secret
        "VOICEKIT_LIVEKIT_OUTBOUND_TRUNK_ID": "ST_test_trunk",
    }


def _definition() -> ScenarioDefinition:
    return ScenarioDefinition(
        name="live_booking",
        caller="A concise caller.",
        goals=("book the requested time",),
        expect=ResultExpectation(outcome="booked"),
        judge=("agent confirms the booking",),
        turns=(
            ScenarioTurn(
                user="Book for {email}.",
                expect=TurnExpectation(judge=("asks for confirmation",)),
            ),
        ),
        profiles=(ScenarioProfile(identity={"email": "alex@example.com"}),),
    )


@pytest.mark.parametrize("runtime", ["pipecat", "livekit"])
def test_live_environment_accepts_only_complete_acknowledged_budget(
    runtime: str,
) -> None:
    config = LiveTestingConfig()
    environment = _base_environment()
    resolved = validate_live_environment(
        config,
        environment,
        runtime=cast(Any, runtime),
        case_count=1,
    )
    assert resolved.runtime == runtime
    assert resolved.max_calls == 4
    assert resolved.target_number == "+14155550123"


@pytest.mark.parametrize(
    ("name", "value", "remove", "match"),
    [
        ("VOICEKIT_LIVE_PSTN_ACK", "", True, "must equal"),
        ("VOICEKIT_LIVE_PSTN_ACK", "yes", False, "must equal"),
        ("VOICEKIT_LIVE_PSTN_MAX_CALLS", "many", False, "must be an integer"),
        ("VOICEKIT_LIVE_PSTN_MAX_CALLS", "3", False, "between 4 and 1000"),
        ("VOICEKIT_LIVE_TARGET_NUMBER", "555", False, "must contain an E.164"),
        ("DEEPGRAM_API_KEY", "", True, "prerequisites are missing"),
        (
            "VOICEKIT_LIVE_PUBLIC_URL",
            "http://not-secure.example",
            False,
            "HTTPS origin",
        ),
    ],
)
def test_live_environment_rejects_before_spending(
    name: str,
    value: str,
    remove: bool,
    match: str,
) -> None:
    environment = _base_environment()
    if remove:
        environment.pop(name)
    else:
        environment[name] = value
    with pytest.raises(VoicekitError, match=match):
        validate_live_environment(
            LiveTestingConfig(),
            environment,
            runtime="pipecat",
            case_count=1,
        )


def test_live_environment_rejects_path_specific_misconfiguration() -> None:
    same_number = _base_environment()
    same_number["VOICEKIT_LIVE_TWILIO_FROM"] = same_number["VOICEKIT_LIVE_TARGET_NUMBER"]
    with pytest.raises(VoicekitError, match="must differ"):
        validate_live_environment(
            LiveTestingConfig(),
            same_number,
            runtime="pipecat",
            case_count=1,
        )

    no_trunk = _base_environment()
    no_trunk["VOICEKIT_LIVEKIT_OUTBOUND_TRUNK_ID"] = "!"
    with pytest.raises(VoicekitError, match="required and malformed"):
        validate_live_environment(
            LiveTestingConfig(),
            no_trunk,
            runtime="livekit",
            case_count=1,
        )

    with pytest.raises(VoicekitError, match="requires VOICEKIT_LIVE_PUBLIC_URL"):
        validate_live_environment(
            LiveTestingConfig(tunnel="url"),
            _base_environment(),
            runtime="pipecat",
            case_count=1,
        )


def test_live_config_rejects_bad_environment_reference() -> None:
    with pytest.raises(ValueError, match="uppercase names"):
        LiveTestingConfig(target_number_env="bad-name")
    with pytest.raises(ValueError, match="uppercase names"):
        LiveTestingConfig(target_number_env="1STARTS_WITH_DIGIT")


@pytest.mark.parametrize("runtime", ["pipecat", "livekit"])
def test_paid_live_fixtures_are_one_case_and_secret_free(runtime: str) -> None:
    fixture = (
        Path(__file__).parents[1]
        / "fixtures"
        / f"live-pstn-{'pipecat' if runtime == 'pipecat' else 'livekit'}"
    )
    assert ManifestStore(fixture / "voicekit.jsonc").load().runtime == runtime
    scenarios = discover_scenarios(fixture)
    assert len(scenarios) == 1
    assert scenarios[0].turns
    config = load_testing_config(fixture)
    assert config.judge.api_key_env == "OPENAI_API_KEY"  # pragma: allowlist secret
    assert config.sim_caller.api_key_env == "OPENAI_API_KEY"  # pragma: allowlist secret
    source = "\n".join(
        path.read_text(encoding="utf-8")
        for path in fixture.rglob("*")
        if path.suffix in {".py", ".jsonc"}
    )
    assert "I_ACKNOWLEDGE_PAID_PSTN" not in source


def test_paid_live_workflow_is_guarded_and_bounded() -> None:
    workflow = Path(__file__).parents[2] / ".github" / "workflows" / "live-pstn.yml"
    source = workflow.read_text(encoding="utf-8")
    assert source.count("vars.VOICEKIT_LIVE_PSTN_ENABLED == 'true'") == 2
    assert source.count('VOICEKIT_LIVE_PSTN_MAX_CALLS: "4"') == 2
    assert source.count("VOICEKIT_LIVE_PSTN_ACK: ${{ vars.VOICEKIT_LIVE_PSTN_ACK }}") == 2
    assert "cancel-in-progress: false" in source
    assert "voicekit test --live --report junit" in source


def test_caller_prompt_and_criteria_are_profile_bound_and_black_box() -> None:
    definition = _definition()
    prompt = caller_prompt(definition, definition.turns)
    assert "alex@example.com" in prompt
    assert "Thank you, goodbye." in prompt
    criteria = live_judge_criteria(definition)
    assert "caller-visible conversation achieved goal: book the requested time" in criteria
    assert "agent's spoken response supports terminal outcome 'booked'" in criteria
    assert "asks for confirmation" in criteria

    missing = definition.model_copy(
        update={
            "turns": (ScenarioTurn(user="Hello {missing}"),),
        }
    )
    with pytest.raises(VoicekitError, match="missing profile field"):
        caller_prompt(missing, missing.turns)


class _Backend:
    def __init__(self, evidence: LiveCallEvidence) -> None:
        self.evidence = evidence
        self.plans: list[LiveCallPlan] = []
        self.closed = False

    async def run_call(self, plan: LiveCallPlan) -> LiveCallEvidence:
        self.plans.append(plan)
        return self.evidence

    async def aclose(self) -> None:
        self.closed = True


class _Judge:
    decision = JudgeDecision(True, "supported by line 2", (2,))

    def __init__(self, _client: object) -> None:
        return

    async def evaluate(
        self,
        _criteria: tuple[str, ...],
        _transcript: tuple[str, ...],
        *,
        seed: int,
    ) -> JudgeDecision:
        assert seed == 7
        return self.decision


async def test_live_executor_reports_call_evidence_and_visible_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(live_testing, "TranscriptJudge", _Judge)
    evidence = LiveCallEvidence(
        transcript=("agent: How can I help?", "caller: Thank you, goodbye."),
        duration_ms=250,
        terminal_status="completed",
        provider="twilio",
        path="pipecat-native-caller-twilio-pstn",
        provider_call_id="CA" + "1" * 32,
        runtime_call_id="run-one",
    )
    backend = _Backend(evidence)
    executor = LivePstnExecutor(
        backend=backend,
        judge=JudgeConfig(),
        environment={},
    )
    result = await executor.execute(
        "live_booking[default]",
        _definition(),
        _definition().turns,
        attempt=1,
    )
    assert result.passed
    assert result.evidence["provider_call_id"].startswith("CA")
    assert backend.plans[0].prompt.endswith("'Thank you, goodbye.'")
    await executor.aclose()
    assert backend.closed

    _Judge.decision = JudgeDecision(False, "not supported", ())
    failed_backend = _Backend(
        LiveCallEvidence(
            transcript=(),
            duration_ms=999_999,
            terminal_status="busy",
            provider="twilio",
            path="pipecat-native-caller-twilio-pstn",
            provider_call_id="CA" + "2" * 32,
            runtime_call_id="run-two",
        )
    )
    failed = await LivePstnExecutor(
        backend=failed_backend,
        judge=JudgeConfig(),
        environment={},
    ).execute(
        "live_booking[default]",
        _definition(),
        _definition().turns,
        attempt=2,
    )
    assert not failed.passed
    assert any("carrier status" in failure for failure in failed.failures)
    assert any("no target-agent speech" in failure for failure in failed.failures)
    assert any("no simulated-caller speech" in failure for failure in failed.failures)
    assert any("exceeds" in failure for failure in failed.failures)
    assert "judge: not supported" in failed.failures
    _Judge.decision = JudgeDecision(True, "supported", (1,))


async def test_live_executor_factory_selects_both_native_backends(tmp_path: Path) -> None:
    for runtime in ("pipecat", "livekit"):
        executor = build_live_executor(
            tmp_path,
            runtime=cast(Any, runtime),
            config=LiveTestingConfig(),
            judge=JudgeConfig(),
            environment=_base_environment(),
            case_count=1,
        )
        backend = cast(Any, executor)._backend
        if runtime == "pipecat":
            assert isinstance(backend, pipecat_live.PipecatTwilioPstnBackend)
        else:
            assert isinstance(backend, LiveKitSipPstnBackend)
        await executor.aclose()


class _ProviderFactory:
    def create_stt(self, *_args: object) -> object:
        return object()

    def create_llm(self, *_args: object) -> object:
        return object()

    def create_tts(self, *_args: object) -> object:
        return object()

    def create_vad(self) -> object:
        return object()

    def create_turn_detector(self) -> object:
        return object()


class _LiveKitRoom:
    def __init__(self) -> None:
        self.handlers: dict[str, Callable[[Any], None]] = {}
        self.connected = False
        self.disconnected = False

    async def connect(self, *_args: object, **_kwargs: object) -> None:
        self.connected = True

    def on(self, event: str, callback: Callable[[Any], None]) -> None:
        self.handlers[event] = callback

    async def disconnect(self) -> None:
        self.disconnected = True


class _SpeechHandle:
    def __init__(self) -> None:
        self.played = False

    async def wait_for_playout(self) -> None:
        self.played = True


class _LiveKitSession:
    def __init__(self, **_kwargs: object) -> None:
        self.handlers: dict[str, Callable[[Any], None]] = {}
        self.started = False
        self.closed = False
        self.shutdown_called = False
        self.opening = _SpeechHandle()

    def on(self, event: str, callback: Callable[[Any], None]) -> None:
        self.handlers[event] = callback

    async def start(self, *_args: object, **_kwargs: object) -> None:
        self.started = True

    def generate_reply(self, **_kwargs: object) -> _SpeechHandle:
        return self.opening

    def shutdown(self, *, drain: bool) -> None:
        assert drain
        self.shutdown_called = True

    async def aclose(self) -> None:
        self.closed = True


class _RoomService:
    def __init__(self) -> None:
        self.created: list[object] = []
        self.deleted: list[object] = []

    async def create_room(self, request: object) -> object:
        self.created.append(request)
        return object()

    async def delete_room(self, request: object) -> object:
        self.deleted.append(request)
        return object()


class _SipService:
    def __init__(self, session: _LiveKitSession) -> None:
        self.session = session
        self.requests: list[object] = []

    async def create_sip_participant(
        self,
        request: object,
        **kwargs: object,
    ) -> object:
        assert kwargs["timeout"] == 50
        self.requests.append(request)
        self.session.handlers["conversation_item_added"](
            ConversationItemAddedEvent(
                item=ChatMessage(role="user", content=["Welcome to the agent."])
            )
        )
        self.session.handlers["conversation_item_added"](
            ConversationItemAddedEvent(
                item=ChatMessage(role="assistant", content=["Thank you, goodbye."])
            )
        )
        return SimpleNamespace(sip_call_id="SCL_test")


class _LiveKitApi:
    def __init__(self, session: _LiveKitSession) -> None:
        self.room = _RoomService()
        self.sip = _SipService(session)
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True


async def _no_sleep(_delay: float) -> None:
    return


async def test_livekit_sip_backend_runs_native_room_call_and_cleans_up() -> None:
    session = _LiveKitSession()
    room = _LiveKitRoom()
    api_client = _LiveKitApi(session)

    def session_factory(**_kwargs: object) -> _LiveKitSession:
        return session

    def agent_factory(**kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(**kwargs)

    backend = LiveKitSipPstnBackend(
        config=LiveTestingConfig(),
        live=LiveEnvironment(
            runtime="livekit",
            target_number="+14155550123",
            max_calls=4,
            livekit_outbound_trunk_id="ST_test",
        ),
        environment=_base_environment(),
        api_client=api_client,
        room_factory=lambda: room,
        session_factory=session_factory,
        agent_factory=agent_factory,
        provider_factory=cast(Any, _ProviderFactory()),
        sleep=_no_sleep,
    )
    result = await backend.run_call(
        LiveCallPlan(
            run_id="case-1",
            case_name="case",
            prompt="caller prompt",
            max_duration_s=10,
            max_turns=4,
        )
    )
    assert result.terminal_status == "completed"
    assert result.provider_call_id == "SCL_test"
    assert result.transcript == (
        "agent: Welcome to the agent.",
        "caller: Thank you, goodbye.",
    )
    assert session.started
    assert session.shutdown_called
    assert session.closed
    assert room.connected
    assert room.disconnected
    assert len(api_client.room.created) == len(api_client.room.deleted) == 1
    await backend.aclose()
    await backend.aclose()
    assert not api_client.closed
    with pytest.raises(VoicekitError, match="already closed"):
        await backend.run_call(LiveCallPlan("closed", "case", "prompt", 10, 2))


async def test_livekit_sip_backend_maps_provider_setup_failure() -> None:
    class BrokenRoom(_LiveKitRoom):
        async def connect(self, *_args: object, **_kwargs: object) -> None:
            raise RuntimeError("secret must not escape")

    session = _LiveKitSession()

    def session_factory(**_kwargs: object) -> _LiveKitSession:
        return session

    def agent_factory(**kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(**kwargs)

    backend = LiveKitSipPstnBackend(
        config=LiveTestingConfig(),
        live=LiveEnvironment(
            runtime="livekit",
            target_number="+14155550123",
            max_calls=4,
            livekit_outbound_trunk_id="ST_test",
        ),
        environment=_base_environment(),
        api_client=_LiveKitApi(session),
        room_factory=BrokenRoom,
        session_factory=session_factory,
        agent_factory=agent_factory,
        provider_factory=cast(Any, _ProviderFactory()),
        sleep=_no_sleep,
    )
    with pytest.raises(VoicekitError, match="provider error type RuntimeError") as captured:
        await backend.run_call(LiveCallPlan("broken", "case", "prompt", 10, 2))
    assert "secret must not escape" not in str(captured.value)

    with pytest.raises(VoicekitError, match="invalid LiveKit"):
        LiveKitSipPstnBackend(
            config=LiveTestingConfig(),
            live=LiveEnvironment(
                runtime="pipecat",
                target_number="+14155550123",
                max_calls=4,
            ),
            environment=_base_environment(),
        )


class _TwilioAdapter:
    account_sid = "AC" + "1" * 32
    auth_token = "test-token"  # pragma: allowlist secret

    def __init__(self, *, verified: bool = True) -> None:
        self.verified = verified
        self.media_error = False
        self.spawn_pending_task = False
        self.backend: Any | None = None
        self.loop: asyncio.AbstractEventLoop | None = None
        self.started: list[tuple[str, str, str]] = []
        self.hung_up: list[str] = []

    def verify_request(self, request: TelephonyRequest) -> bool:
        del request
        return self.verified

    def parse_event(self, request: TelephonyRequest) -> CallEvent:
        return CallEvent(
            type="completed",
            provider_call_id="CA" + "2" * 32,
            provider_status="completed",
            ended_reason="provider_hangup",
            intent_id=request.route_params.get("intent_id"),
        )

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
    ) -> str:
        del target, amd, send_digits, record, timeout_s
        assert intent_id is not None
        assert self.backend is not None
        assert self.loop is not None
        self.started.append((from_no, to_no, intent_id))

        def finish() -> None:
            backend = self.backend
            assert backend is not None
            state = backend._states[intent_id]
            state.provider_call_id = "CA" + "2" * 32
            state.transcript.extend(("agent: Welcome.", "caller: Thank you, goodbye."))
            state.terminal_status = "completed"
            if self.media_error:
                state.error_type = "MediaFailure"
            if self.spawn_pending_task:
                task = asyncio.create_task(asyncio.Event().wait())
                state.tasks.add(task)
            state.terminal.set()
            state.media_done.set()

        self.loop.call_soon_threadsafe(finish)
        return "CA" + "2" * 32

    def hangup(self, call_sid: str) -> None:
        self.hung_up.append(call_sid)


class _Runner:
    def __init__(self) -> None:
        self.started = False
        self.stopped = False
        self.runner = SimpleNamespace(add_workers=self.add_workers)
        self.workers: list[object] = []

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.stopped = True

    async def add_workers(self, worker: object) -> None:
        self.workers.append(worker)


class _Tunnel:
    provider = "url"
    public_url = "https://caller.example"
    local_url = "http://127.0.0.1:18765"

    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


class _Server:
    def __init__(self) -> None:
        self.started = True
        self._should_exit = False
        self.exit = asyncio.Event()

    @property
    def should_exit(self) -> bool:
        return self._should_exit

    @should_exit.setter
    def should_exit(self, value: bool) -> None:
        self._should_exit = value
        if value:
            self.exit.set()

    async def serve(self) -> None:
        await self.exit.wait()


class _FailedServer:
    started = False
    should_exit = False

    async def serve(self) -> None:
        return


class _BrokenTunnelManager:
    async def open(self, *_args: object, **_kwargs: object) -> _Tunnel:
        raise RuntimeError("unavailable edge")


class _WorkingTunnelManager:
    def __init__(self, tunnel: _Tunnel) -> None:
        self.tunnel = tunnel
        self.calls: list[tuple[int, dict[str, object]]] = []

    async def open(self, port: int, **kwargs: object) -> _Tunnel:
        self.calls.append((port, kwargs))
        return self.tunnel


async def test_pipecat_callback_services_start_and_stop_as_one_unit(tmp_path: Path) -> None:
    adapter = _TwilioAdapter()
    runner = _Runner()
    tunnel = _Tunnel()
    tunnels = _WorkingTunnelManager(tunnel)
    server = _Server()
    backend = pipecat_live.PipecatTwilioPstnBackend(
        root=tmp_path,
        config=LiveTestingConfig(tunnel="url"),
        live=LiveEnvironment(
            runtime="pipecat",
            target_number="+14155550123",
            max_calls=4,
            twilio_from_number="+14155550124",
            public_url=tunnel.public_url,
        ),
        environment=_base_environment(),
        adapter_factory=lambda _url: adapter,
        tunnel_manager=cast(Any, tunnels),
        runner_host=cast(Any, runner),
        server_factory=lambda _app, _port: server,
    )
    await cast(Any, backend)._ensure_started()
    assert runner.started
    assert tunnels.calls[0][1]["preference"] == "url"
    await backend.aclose()
    assert runner.stopped
    assert tunnel.closed
    assert server.should_exit


async def test_pipecat_startup_failure_rolls_back_before_dial(tmp_path: Path) -> None:
    adapter = _TwilioAdapter()
    runner = _Runner()
    server = _Server()
    backend = pipecat_live.PipecatTwilioPstnBackend(
        root=tmp_path,
        config=LiveTestingConfig(),
        live=LiveEnvironment(
            runtime="pipecat",
            target_number="+14155550123",
            max_calls=4,
            twilio_from_number="+14155550124",
        ),
        environment=_base_environment(),
        adapter_factory=lambda _url: adapter,
        tunnel_manager=cast(Any, _BrokenTunnelManager()),
        runner_host=cast(Any, runner),
        server_factory=lambda _app, _port: server,
    )
    with pytest.raises(VoicekitError, match="no call was placed"):
        await backend.run_call(LiveCallPlan("startup", "case", "prompt", 10, 3))
    assert server.should_exit
    assert not adapter.started
    assert not runner.started
    await backend.aclose()


async def test_pipecat_server_failure_maps_catalog_error_before_dial(tmp_path: Path) -> None:
    backend = pipecat_live.PipecatTwilioPstnBackend(
        root=tmp_path,
        config=LiveTestingConfig(),
        live=LiveEnvironment(
            runtime="pipecat",
            target_number="+14155550123",
            max_calls=4,
            twilio_from_number="+14155550124",
        ),
        environment=_base_environment(),
        runner_host=cast(Any, _Runner()),
        server_factory=lambda _app, _port: _FailedServer(),
    )
    with pytest.raises(VoicekitError, match="callback server did not start"):
        await backend.run_call(LiveCallPlan("server-failed", "case", "prompt", 10, 3))
    await backend.aclose()


async def test_pipecat_twilio_backend_dials_and_returns_evidence(tmp_path: Path) -> None:
    adapter = _TwilioAdapter()
    runner = _Runner()
    tunnel = _Tunnel()
    backend = pipecat_live.PipecatTwilioPstnBackend(
        root=tmp_path,
        config=LiveTestingConfig(),
        live=LiveEnvironment(
            runtime="pipecat",
            target_number="+14155550123",
            max_calls=4,
            twilio_from_number="+14155550124",
            public_url=tunnel.public_url,
        ),
        environment=_base_environment(),
        adapter_factory=lambda _url: adapter,
        runner_host=cast(Any, runner),
    )
    private_backend = cast(Any, backend)
    private_backend._started = True
    private_backend._runner_started = True
    private_backend._adapter = adapter
    private_backend._tunnel = tunnel
    adapter.backend = backend
    adapter.loop = asyncio.get_running_loop()
    result = await backend.run_call(LiveCallPlan("pipecat-case", "case", "prompt", 10, 3))
    assert result.terminal_status == "completed"
    assert result.provider == "twilio"
    assert result.transcript[0] == "agent: Welcome."
    assert adapter.started[0][:2] == ("+14155550124", "+14155550123")
    await backend.aclose()
    assert runner.stopped
    assert tunnel.closed
    with pytest.raises(VoicekitError, match="already closed"):
        await backend.run_call(LiveCallPlan("closed", "case", "prompt", 10, 2))

    with pytest.raises(VoicekitError, match="invalid Pipecat"):
        pipecat_live.PipecatTwilioPstnBackend(
            root=tmp_path,
            config=LiveTestingConfig(),
            live=LiveEnvironment(
                runtime="livekit",
                target_number="+14155550123",
                max_calls=4,
            ),
            environment=_base_environment(),
        )


async def test_pipecat_call_maps_provider_failure_media_error_and_pending_cleanup(
    tmp_path: Path,
) -> None:
    class BrokenAdapter(_TwilioAdapter):
        def start_call(self, *_args: object, **_kwargs: object) -> str:
            raise RuntimeError("provider secret must not escape")

    tunnel = _Tunnel()
    broken = BrokenAdapter()
    broken_backend = pipecat_live.PipecatTwilioPstnBackend(
        root=tmp_path,
        config=LiveTestingConfig(),
        live=LiveEnvironment(
            runtime="pipecat",
            target_number="+14155550123",
            max_calls=4,
            twilio_from_number="+14155550124",
        ),
        environment=_base_environment(),
        adapter_factory=lambda _url: broken,
        runner_host=cast(Any, _Runner()),
    )
    private_broken = cast(Any, broken_backend)
    private_broken._started = True
    private_broken._adapter = broken
    private_broken._tunnel = tunnel
    with pytest.raises(VoicekitError, match="provider error type RuntimeError") as captured:
        await broken_backend.run_call(LiveCallPlan("broken-call", "case", "prompt", 10, 3))
    assert "provider secret must not escape" not in str(captured.value)

    adapter = _TwilioAdapter()
    adapter.media_error = True
    adapter.spawn_pending_task = True
    backend = pipecat_live.PipecatTwilioPstnBackend(
        root=tmp_path,
        config=LiveTestingConfig(),
        live=LiveEnvironment(
            runtime="pipecat",
            target_number="+14155550123",
            max_calls=4,
            twilio_from_number="+14155550124",
        ),
        environment=_base_environment(),
        adapter_factory=lambda _url: adapter,
        runner_host=cast(Any, _Runner()),
    )
    private_backend = cast(Any, backend)
    private_backend._started = True
    private_backend._adapter = adapter
    private_backend._tunnel = tunnel
    adapter.backend = backend
    adapter.loop = asyncio.get_running_loop()
    result = await backend.run_call(LiveCallPlan("media-error", "case", "prompt", 10, 3))
    assert result.terminal_status == "media-error"
    assert not private_backend._states

    active = cast(Any, pipecat_live)._CallState(
        LiveCallPlan("active", "case", "prompt", 10, 3),
        provider_call_id="CA" + "7" * 32,
    )
    private_backend._states["active"] = active
    await backend.aclose()
    assert "CA" + "7" * 32 in adapter.hung_up


async def test_pipecat_callback_routes_fail_closed_and_terminalize(tmp_path: Path) -> None:
    adapter = _TwilioAdapter()
    backend = pipecat_live.PipecatTwilioPstnBackend(
        root=tmp_path,
        config=LiveTestingConfig(),
        live=LiveEnvironment(
            runtime="pipecat",
            target_number="+14155550123",
            max_calls=4,
            twilio_from_number="+14155550124",
        ),
        environment=_base_environment(),
        adapter_factory=lambda _url: adapter,
    )
    private_module = cast(Any, pipecat_live)
    state = private_module._CallState(
        LiveCallPlan("intent-one", "case", "prompt", 10, 2),
        provider_call_id="CA" + "2" * 32,
    )
    private_backend = cast(Any, backend)
    private_backend._states["intent-one"] = state
    private_backend._adapter = adapter
    transport = httpx.ASGITransport(app=backend.app)
    async with httpx.AsyncClient(transport=transport, base_url="https://caller.example") as client:
        response = await client.post("/live/twilio/events/intent-one")
        assert response.status_code == 204
        assert state.terminal.is_set()
        missing = await client.post("/live/twilio/events/missing")
        assert missing.status_code == 404
        adapter.verified = False
        denied = await client.post("/live/twilio/events/intent-one")
        assert denied.status_code == 403
        adapter.verified = True

        class InvalidEventAdapter(_TwilioAdapter):
            def parse_event(self, request: TelephonyRequest) -> CallEvent:
                del request
                raise VoicekitError("VK-TST-003", detail="invalid callback")

        private_backend._adapter = InvalidEventAdapter()
        invalid = await client.post("/live/twilio/events/intent-one")
        assert invalid.status_code == 400

        class ProgressEventAdapter(_TwilioAdapter):
            def parse_event(self, request: TelephonyRequest) -> CallEvent:
                return CallEvent(
                    type="ringing",
                    provider_call_id="CA" + "9" * 32,
                    provider_status="ringing",
                    intent_id=request.route_params.get("intent_id"),
                )

        progress_state = private_module._CallState(
            LiveCallPlan("progress", "case", "prompt", 10, 2)
        )
        private_backend._states["progress"] = progress_state
        private_backend._adapter = ProgressEventAdapter()
        progress = await client.post("/live/twilio/events/progress")
        assert progress.status_code == 204
        assert progress_state.provider_call_id == "CA" + "9" * 32
        assert not progress_state.terminal.is_set()


class _WebSocket:
    url = URL("wss://caller.example/live/twilio/media")
    headers: ClassVar[dict[str, str]] = {}
    client = SimpleNamespace(host="127.0.0.1")

    def __init__(self) -> None:
        self.accepted = False
        self.closed: tuple[int, str] | None = None

    async def accept(self) -> None:
        self.accepted = True

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.closed = (code, reason)


class _Worker:
    def __init__(self) -> None:
        self.waited = False

    async def wait(self) -> None:
        self.waited = True


async def test_pipecat_media_route_binds_reserved_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = _TwilioAdapter()
    runner = _Runner()
    worker = _Worker()
    backend = pipecat_live.PipecatTwilioPstnBackend(
        root=tmp_path,
        config=LiveTestingConfig(),
        live=LiveEnvironment(
            runtime="pipecat",
            target_number="+14155550123",
            max_calls=4,
            twilio_from_number="+14155550124",
        ),
        environment=_base_environment(),
        adapter_factory=lambda _url: adapter,
        runner_host=cast(Any, runner),
        worker_builder=lambda _state, _transport: worker,
    )
    private_module = cast(Any, pipecat_live)
    state = private_module._CallState(
        LiveCallPlan("media-one", "case", "prompt", 10, 2),
        provider_call_id="CA" + "3" * 32,
    )
    private_backend = cast(Any, backend)
    private_backend._states["media-one"] = state
    private_backend._adapter = adapter

    async def parse(_websocket: object) -> tuple[str, CallData]:
        return (
            "twilio",
            CallData(
                call_id="CA" + "3" * 32,
                stream_id="MZ-test",
                body={"run_id": "media-one"},
            ),
        )

    monkeypatch.setattr(pipecat_live, "parse_telephony_websocket", parse)
    route = next(
        item for item in backend.app.routes if getattr(item, "path", None) == "/live/twilio/media"
    )
    websocket = _WebSocket()
    await cast(Any, route).endpoint(websocket)
    assert websocket.accepted
    assert websocket.closed is None
    assert worker.waited
    assert state.media_done.is_set()
    assert runner.workers == [worker]

    adapter.verified = False
    denied = _WebSocket()
    await cast(Any, route).endpoint(denied)
    assert denied.closed == (1008, "VK-TST-003")

    adapter.verified = True

    async def unsupported(_websocket: object) -> tuple[str, CallData]:
        return ("other", CallData(call_id="CA", stream_id="MZ", body={}))

    monkeypatch.setattr(pipecat_live, "parse_telephony_websocket", unsupported)
    wrong_transport = _WebSocket()
    await cast(Any, route).endpoint(wrong_transport)
    assert wrong_transport.closed == (1011, "VK-TST-003")

    async def mismatched(_websocket: object) -> tuple[str, CallData]:
        return (
            "twilio",
            CallData(
                call_id="CA" + "8" * 32,
                stream_id="MZ-test",
                body={"run_id": "media-one"},
            ),
        )

    state.media_done.clear()
    monkeypatch.setattr(pipecat_live, "parse_telephony_websocket", mismatched)
    wrong_call = _WebSocket()
    await cast(Any, route).endpoint(wrong_call)
    assert wrong_call.closed == (1011, "VK-TST-003")
    assert state.error_type == "VoicekitError"
    assert state.media_done.is_set()

    async def unreserved(_websocket: object) -> tuple[str, CallData]:
        return (
            "twilio",
            CallData(
                call_id="CA" + "6" * 32,
                stream_id="MZ-test",
                body={"run_id": "unknown"},
            ),
        )

    monkeypatch.setattr(pipecat_live, "parse_telephony_websocket", unreserved)
    unknown = _WebSocket()
    await cast(Any, route).endpoint(unknown)
    assert unknown.closed == (1011, "VK-TST-003")


class _Processor(FrameProcessor):
    pass


class _PipecatProviders:
    def create_stt(self, *_args: object) -> FrameProcessor:
        return _Processor()

    def create_llm(self, *_args: object) -> Any:
        return _Processor()

    def create_tts(self, *_args: object) -> FrameProcessor:
        return _Processor()

    def language_delta(self, *_args: object) -> object:
        return object()


class _Transport:
    def __init__(self) -> None:
        self.handlers: dict[str, object] = {}
        self.input_processor = _Processor()
        self.output_processor = _Processor()

    def input(self) -> FrameProcessor:
        return self.input_processor

    def output(self) -> FrameProcessor:
        return self.output_processor

    def event_handler(self, event_name: str) -> Any:
        def decorator(function: object) -> object:
            self.handlers[event_name] = function
            return function

        return decorator


class _CapturingAggregator(FrameProcessor):
    def __init__(self) -> None:
        super().__init__()
        self.handlers: dict[str, object] = {}

    def event_handler(self, event_name: str) -> Any:
        def decorator(function: object) -> object:
            self.handlers[event_name] = function
            return function

        return decorator


class _CapturingAggregatorPair:
    last: ClassVar[_CapturingAggregatorPair | None] = None

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        self.user_aggregator = _CapturingAggregator()
        self.assistant_aggregator = _CapturingAggregator()
        _CapturingAggregatorPair.last = self

    def user(self) -> _CapturingAggregator:
        return self.user_aggregator

    def assistant(self) -> _CapturingAggregator:
        return self.assistant_aggregator


async def test_pipecat_worker_and_transport_use_pinned_8khz_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        pipecat_live,
        "LLMContextAggregatorPair",
        _CapturingAggregatorPair,
    )
    backend = pipecat_live.PipecatTwilioPstnBackend(
        root=tmp_path,
        config=LiveTestingConfig(),
        live=LiveEnvironment(
            runtime="pipecat",
            target_number="+14155550123",
            max_calls=4,
            twilio_from_number="+14155550124",
        ),
        environment=_base_environment(),
        provider_factory=cast(Any, _PipecatProviders()),
        runner_host=cast(Any, _Runner()),
    )
    transport = _Transport()
    private_backend = cast(Any, backend)
    private_module = cast(Any, pipecat_live)
    state = private_module._CallState(LiveCallPlan("worker", "case", "prompt", 10, 3))
    worker = private_backend._build_worker(state, cast(Any, transport))
    assert isinstance(worker, PipelineWorker)
    assert worker.params.audio_in_sample_rate == 8000
    assert worker.params.audio_out_sample_rate == 8000
    assert set(transport.handlers) == {"on_client_connected", "on_client_disconnected"}

    state.heard_target.set()
    await cast(Any, transport.handlers["on_client_connected"])(object(), object())
    await asyncio.sleep(0)
    await cast(Any, transport.handlers["on_client_disconnected"])(object(), object())
    pair = _CapturingAggregatorPair.last
    assert pair is not None
    await cast(Any, pair.user_aggregator.handlers["on_user_turn_message_added"])(
        object(), SimpleNamespace(content="Target answer.")
    )
    await cast(Any, pair.assistant_aggregator.handlers["on_assistant_turn_stopped"])(
        object(), SimpleNamespace(content="Caller reply.")
    )
    assert state.transcript[-2:] == ["agent: Target answer.", "caller: Caller reply."]

    with pytest.raises(VoicekitError, match="callback server is not ready"):
        private_backend._require_adapter()
    with pytest.raises(VoicekitError, match="tunnel is not ready"):
        private_backend._require_tunnel()
    default_adapter = private_backend._default_adapter("https://caller.example")
    assert default_adapter.auth_token == _base_environment()["TWILIO_AUTH_TOKEN"]
    assert private_module._uvicorn_server(backend.app, 18765).config.port == 18765

    adapter = _TwilioAdapter()
    params = private_module._transport_params(
        adapter=adapter,
        call_id="CA" + "4" * 32,
        stream_id="MZ-test",
        max_duration_s=10,
    )
    assert params.audio_in_sample_rate == 8000
    assert params.audio_out_sample_rate == 8000
    assert params.session_timeout == 40


def test_request_mapping_preserves_proxy_signature_inputs() -> None:
    request = SimpleNamespace(
        url=SimpleNamespace(
            scheme="https",
            netloc="caller.example",
            path="/events",
            query="",
        ),
        headers={"x-twilio-signature": "signed"},
        client=SimpleNamespace(host="127.0.0.1"),
    )
    private_module = cast(Any, pipecat_live)
    mapped = private_module._http_request(
        cast(Any, request),
        {"CallSid": "CA"},
        route_params={"intent_id": "one"},
    )
    assert mapped.host == "caller.example"
    assert mapped.route_params == {"intent_id": "one"}

    websocket = _WebSocket()
    ws_mapped = private_module._websocket_request(cast(Any, websocket))
    assert ws_mapped.is_websocket
    assert ws_mapped.scheme == "wss"


def test_live_call_evidence_report_is_secret_free() -> None:
    evidence = LiveCallEvidence(
        transcript=("agent: hello",),
        duration_ms=1,
        terminal_status="completed",
        provider="twilio",
        path="path",
        provider_call_id="CA-test",
        runtime_call_id="run-test",
    )
    assert evidence.report_values() == {
        "provider": "twilio",
        "path": "path",
        "provider_call_id": "CA-test",
        "runtime_call_id": "run-test",
        "terminal_status": "completed",
    }
