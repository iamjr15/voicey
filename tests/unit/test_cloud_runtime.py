# pyright: reportPrivateUsage=false

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from typing import Any, cast

import pytest

from voicey import Agent, Models, Results, Web
from voicey.config.manifest import ManifestStore, ProjectManifest
from voicey.deploy import cloud_runtime
from voicey.deploy.cloud_runtime import (
    CloudWorkerSettings,
    RelayRepositoryFactory,
    run_livekit_cloud_worker,
    run_pipecat_cloud_session,
    validate_cloud_worker_startup,
)
from voicey.errors import VoiceyError
from voicey.relay.auth import RelayCredential
from voicey.runtimes.pipecat.lifecycle import PipecatCall


def _agent(runtime: str = "pipecat") -> Agent:
    return Agent(
        name="voicey-agent",
        runtime=cast(Any, runtime),
        models=Models(
            stt="deepgram/nova-3",
            llm="anthropic/claude-sonnet-5",
            tts="cartesia/sonic-3.5",
        ),
        persona="Test cloud runtime.",
        flow="flow:entry",
        tools="tools",
        web=Web(enabled=True, allowed_origins=["https://app.example.test"]),
        results=Results(
            webhook="https://receiver.example.test/results",
            secret_env="VOICEY_WEBHOOK_SECRET",
        ),
    )


def _manifest(runtime: str = "pipecat", *, carriers: list[str] | None = None) -> ProjectManifest:
    return ProjectManifest.model_validate(
        {
            "project_name": "voicey-agent",
            "runtime": runtime,
            "recipe": {"name": "scratch", "version": "1.0.0"},
            "carriers": carriers or [],
            "channels": ["web", "phone"] if carriers else ["web"],
            "phone_number": "+14155550100" if carriers else None,
            "models": {
                "stt": "deepgram/nova-3",
                "llm": "anthropic/claude-sonnet-5",
                "tts": "cartesia/sonic-3.5",
            },
            "deploy_target": None,
            "agent_module": "agent",
        }
    )


def _environment(tmp_path: Path, runtime: str) -> dict[str, str]:
    return {
        "VOICEY_RUNTIME": runtime,
        "VOICEY_PROJECT_ROOT": str(tmp_path),
        "VOICEY_RELAY_URL": "https://relay.example.test",
        "VOICEY_RELAY_CREDENTIAL": RelayCredential.issue("runtime-key").reveal(),
    }


def _write_project(path: Path, runtime: str) -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / "agent.py").write_text(
        f"""from voicey import Agent, Models, Results, Web

agent = Agent(
    name="voicey-agent",
    runtime={runtime!r},
    models=Models(
        stt="deepgram/nova-3",
        llm="anthropic/claude-sonnet-5",
        tts="cartesia/sonic-3.5",
    ),
    persona="Cloud worker fixture.",
    flow="flow:entry",
    tools="tools",
    web=Web(enabled=True, allowed_origins=["https://app.example.test"]),
    results=Results(
        webhook="https://receiver.example.test/results",
        secret_env="VOICEY_WEBHOOK_SECRET",
    ),
)
""",
        encoding="utf-8",
    )
    ManifestStore(path / "voicey.jsonc").save(_manifest(runtime))
    (path / "tools.py").write_text('IMPORT_MARKER = "project-tools"\n', encoding="utf-8")


def test_cloud_worker_settings_are_fail_closed(tmp_path: Path) -> None:
    values = _environment(tmp_path, "pipecat")
    settings = CloudWorkerSettings.from_environment(values, expected_runtime="pipecat")
    assert settings.runtime == "pipecat"
    assert settings.project_root == tmp_path.resolve()
    assert settings.relay_url == "https://relay.example.test"

    with pytest.raises(VoiceyError, match="expects 'livekit'"):
        CloudWorkerSettings.from_environment(values, expected_runtime="livekit")
    with pytest.raises(VoiceyError, match="absolute path"):
        CloudWorkerSettings.from_environment(
            values | {"VOICEY_PROJECT_ROOT": "relative"},
            expected_runtime="pipecat",
        )
    with pytest.raises(VoiceyError, match="RELAY_URL is invalid"):
        CloudWorkerSettings.from_environment(
            values | {"VOICEY_RELAY_URL": "http://relay.example.test"},
            expected_runtime="pipecat",
        )
    with pytest.raises(VoiceyError, match="VOICEY_RELAY_CREDENTIAL"):
        CloudWorkerSettings.from_environment(
            values | {"VOICEY_RELAY_CREDENTIAL": ""},
            expected_runtime="pipecat",
        )


@pytest.mark.asyncio
async def test_relay_factory_and_startup_probe_open_signed_readiness(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    opened = 0
    closed = 0

    class FakeRelay:
        def __init__(self, _url: str, _credential: RelayCredential) -> None:
            pass

        async def open(self) -> FakeRelay:
            nonlocal opened
            opened += 1
            return self

        async def close(self) -> None:
            nonlocal closed
            closed += 1

        async def __aenter__(self) -> FakeRelay:
            return await self.open()

        async def __aexit__(self, *_exc: object) -> None:
            await self.close()

    monkeypatch.setattr(cloud_runtime, "RelayClient", FakeRelay)
    settings = CloudWorkerSettings.from_environment(
        _environment(tmp_path, "livekit"),
        expected_runtime="livekit",
    )
    repository = await RelayRepositoryFactory(
        settings.relay_url,
        settings.relay_credential,
    )()
    assert isinstance(repository, FakeRelay)
    await repository.close()
    await validate_cloud_worker_startup(settings)
    assert opened == 2
    assert closed == 2


@pytest.mark.asyncio
async def test_livekit_cloud_worker_uses_native_host_and_per_job_relay(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _write_project(tmp_path, "livekit")
    environment = _environment(tmp_path, "livekit")
    sys.modules.pop("agent", None)
    startup: list[CloudWorkerSettings] = []
    hosts: list[FakeHost] = []

    async def validate(settings: CloudWorkerSettings) -> None:
        startup.append(settings)

    class FakeHost:
        def __init__(self, **kwargs: object) -> None:
            self.kwargs = kwargs
            self.devmode: bool | None = None
            hosts.append(self)

        async def run(self, *, devmode: bool) -> None:
            assert str(tmp_path) in sys.path
            self.devmode = devmode

    import voicey.runtimes.livekit as livekit_runtime

    monkeypatch.setattr(cloud_runtime, "validate_cloud_worker_startup", validate)
    monkeypatch.setattr(livekit_runtime, "LiveKitHost", FakeHost)
    await run_livekit_cloud_worker(environment=environment)

    assert len(startup) == 1
    assert len(hosts) == 1
    assert hosts[0].devmode is False
    assert hosts[0].kwargs["agent"].runtime == "livekit"  # type: ignore[union-attr]
    settings = hosts[0].kwargs["settings"]
    assert settings.health_port == 8081  # type: ignore[union-attr]
    assert isinstance(hosts[0].kwargs["repository_factory"], RelayRepositoryFactory)
    assert str(tmp_path) not in sys.path


@pytest.mark.asyncio
async def test_pipecat_cloud_session_runs_native_worker_and_closes_relay(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    events: list[str] = []
    agent = _agent("pipecat")
    manifest = _manifest("pipecat")

    class FakeRepository:
        async def open(self) -> FakeRepository:
            events.append("relay.open")
            return self

        async def close(self) -> None:
            events.append("relay.close")

    repository = FakeRepository()

    class FakeAdmission:
        def __init__(self, capacity: int) -> None:
            assert capacity == 1

        async def acquire(self, call_id: str) -> object:
            events.append(f"admit:{call_id}")
            return object()

    class FakeLifecycle:
        terminal_event: object | None = None

        async def finish(self, reason: str, *, provider_state: str) -> None:
            events.append(f"finish:{reason}:{provider_state}")
            self.terminal_event = object()

    lifecycle = FakeLifecycle()

    class FakeLifecycleManager:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        async def begin(
            self,
            selected_agent: Agent,
            call: PipecatCall,
            _lease: object,
        ) -> FakeLifecycle:
            assert selected_agent is agent
            events.append(f"begin:{call.call_id}")
            return lifecycle

    class FakeSession:
        async def start(self, _runner: object) -> None:
            events.append("session.start")

        async def wait(self) -> object:
            events.append("session.wait")
            lifecycle.terminal_event = object()
            return object()

        async def end(self, reason: str) -> None:
            events.append(f"session.end:{reason}")

    class FakeBuilder:
        def __init__(self, selected_repository: object, **_kwargs: object) -> None:
            assert isinstance(selected_repository, cloud_runtime.InstrumentedRepository)
            assert selected_repository.repository is repository

        def build(self, **kwargs: object) -> FakeSession:
            assert str(tmp_path) in sys.path
            assert kwargs["sample_rate"] == 16000
            return FakeSession()

    class FakeRunner:
        def __init__(self, **_kwargs: object) -> None:
            pass

        async def run(self) -> None:
            events.append("runner.run")

    async def transport_and_call(
        _runner_args: object,
        **_kwargs: object,
    ) -> tuple[object, PipecatCall]:
        return object(), PipecatCall(
            call_id="cloud-call",
            channel="web",
            direction="inbound",
            provider="daily",
        )

    import pipecat.workers.runner as worker_runner

    import voicey.runtimes.pipecat.admission as admission
    import voicey.runtimes.pipecat.lifecycle as lifecycle_module
    import voicey.runtimes.pipecat.session as session_module

    def repository_factory(_url: str, _credential: RelayCredential) -> FakeRepository:
        return repository

    def load_project(_settings: CloudWorkerSettings) -> tuple[ProjectManifest, Agent]:
        return manifest, agent

    def no_transfer(**_kwargs: object) -> None:
        return None

    monkeypatch.setattr(cloud_runtime, "RelayClient", repository_factory)
    monkeypatch.setattr(cloud_runtime, "_load_project", load_project)
    monkeypatch.setattr(cloud_runtime, "_pipecat_transport_and_call", transport_and_call)
    monkeypatch.setattr(cloud_runtime, "_cloud_transfer", no_transfer)
    monkeypatch.setattr(admission, "AdmissionController", FakeAdmission)
    monkeypatch.setattr(lifecycle_module, "PipecatLifecycleManager", FakeLifecycleManager)
    monkeypatch.setattr(session_module, "PipecatSessionBuilder", FakeBuilder)
    monkeypatch.setattr(worker_runner, "WorkerRunner", FakeRunner)

    await run_pipecat_cloud_session(
        object(),
        environment=_environment(tmp_path, "pipecat"),
    )

    assert events[0] == "relay.open"
    assert "begin:cloud-call" in events
    assert "session.start" in events
    assert "runner.run" in events
    assert "session.wait" in events
    assert events[-1] == "relay.close"
    assert str(tmp_path) not in sys.path


@pytest.mark.asyncio
async def test_pipecat_transport_maps_pinned_daily_webrtc_and_telephony(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pipecat.runner import utils
    from pipecat.runner.types import (
        DailyRunnerArguments,
        SmallWebRTCRunnerArguments,
        WebSocketRunnerArguments,
    )

    transports: list[object] = []

    async def create_transport(_args: object, mappings: object) -> object:
        transports.append(mappings)
        return object()

    monkeypatch.setattr(utils, "create_transport", create_transport)
    daily_transport, daily_call = await cloud_runtime._pipecat_transport_and_call(
        DailyRunnerArguments(room_url="https://daily.example.test/room", token="token"),
        manifest=_manifest(),
        agent=_agent(),
        environment={},
    )
    webrtc_transport, webrtc_call = await cloud_runtime._pipecat_transport_and_call(
        SmallWebRTCRunnerArguments(webrtc_connection=object()),
        manifest=_manifest(),
        agent=_agent(),
        environment={},
    )
    assert daily_transport is not None
    assert daily_call.provider == "daily"
    assert webrtc_transport is not None
    assert webrtc_call.provider == "smallwebrtc"
    assert len(transports) == 2

    async def parse(_websocket: object) -> tuple[str, dict[str, str]]:
        return (
            "twilio",
            {
                "call_id": "CA123",
                "stream_id": "MZ123",
                "from": "+14155550100",
                "to": "+14155550101",
            },
        )

    monkeypatch.setattr(utils, "parse_telephony_websocket", parse)
    args = WebSocketRunnerArguments(websocket=cast(Any, object()))
    transport, call = await cloud_runtime._pipecat_transport_and_call(
        args,
        manifest=_manifest(carriers=["twilio"]),
        agent=_agent(),
        environment={
            "TWILIO_ACCOUNT_SID": "AC123",
            "TWILIO_AUTH_TOKEN": "token",
        },
    )
    assert transport is not None
    assert args.transport_type == "twilio"
    assert call.call_id == "CA123"
    assert call.from_number == "+14155550100"
    assert call.to_number == "+14155550101"


def test_pipecat_cloud_base_arguments_are_strictly_normalized() -> None:
    daily_type = type(
        "DailySessionArguments",
        (),
        {"__module__": "pipecatcloud.agent"},
    )
    daily = cast(Any, daily_type())
    daily.room_url = "https://daily.example.test/room"
    daily.token = "token"
    daily.body = {"transport": "daily"}
    daily.session_id = "session-123"

    normalized = cast(Any, cloud_runtime._normalize_pipecat_cloud_arguments(daily))
    assert type(normalized).__name__ == "DailyRunnerArguments"
    assert normalized.room_url == daily.room_url
    assert normalized.token == daily.token
    assert normalized.body == daily.body
    assert normalized.session_id == daily.session_id

    websocket_type = type(
        "WebSocketSessionArguments",
        (),
        {"__module__": "pipecatcloud.agent"},
    )
    websocket = cast(Any, websocket_type())
    websocket.websocket = object()
    websocket.body = None
    websocket.session_id = "session-456"
    normalized_websocket = cast(
        Any,
        cloud_runtime._normalize_pipecat_cloud_arguments(websocket),
    )
    assert type(normalized_websocket).__name__ == "WebSocketRunnerArguments"
    assert normalized_websocket.websocket is websocket.websocket
    assert normalized_websocket.session_id == websocket.session_id


def test_pipecat_cloud_base_arguments_fail_closed() -> None:
    generic_type = type(
        "PipecatSessionArguments",
        (),
        {"__module__": "pipecatcloud.agent"},
    )
    with pytest.raises(VoiceyError, match="do not identify a supported transport"):
        cloud_runtime._normalize_pipecat_cloud_arguments(generic_type())

    daily_type = type(
        "DailySessionArguments",
        (),
        {"__module__": "pipecatcloud.agent"},
    )
    with pytest.raises(VoiceyError, match="Daily session arguments are incomplete"):
        cloud_runtime._normalize_pipecat_cloud_arguments(daily_type())

    lookalike_type = type(
        "DailySessionArguments",
        (),
        {"__module__": "untrusted.module"},
    )
    lookalike = lookalike_type()
    assert cloud_runtime._normalize_pipecat_cloud_arguments(lookalike) is lookalike


@pytest.mark.asyncio
async def test_pipecat_transport_rejects_wire_mismatch_and_unsupported_args(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pipecat.runner import utils
    from pipecat.runner.types import WebSocketRunnerArguments

    async def parse(_websocket: object) -> tuple[str, dict[str, str]]:
        return "twilio", {"call_id": "CA123", "stream_id": "MZ123"}

    monkeypatch.setattr(utils, "parse_telephony_websocket", parse)
    with pytest.raises(VoiceyError, match="required 'plivo' wire format"):
        await cloud_runtime._pipecat_transport_and_call(
            WebSocketRunnerArguments(websocket=cast(Any, object())),
            manifest=_manifest(carriers=["vobiz"]),
            agent=_agent(),
            environment={},
        )
    with pytest.raises(VoiceyError, match="unsupported Pipecat Cloud runner"):
        await cloud_runtime._pipecat_transport_and_call(
            object(),
            manifest=_manifest(),
            agent=_agent(),
            environment={},
        )

    async def parse_unknown(_websocket: object) -> tuple[str, dict[str, str]]:
        return "exotel", {"call_id": "call-123", "stream_id": "stream-123"}

    monkeypatch.setattr(utils, "parse_telephony_websocket", parse_unknown)
    with pytest.raises(VoiceyError, match="provider 'exotel' is unsupported"):
        await cloud_runtime._pipecat_transport_and_call(
            WebSocketRunnerArguments(websocket=cast(Any, object())),
            manifest=_manifest(),
            agent=_agent(),
            environment={},
        )


def test_telephony_params_pin_8khz_serializers_and_credentials() -> None:
    cases = (
        (
            "twilio",
            {"TWILIO_ACCOUNT_SID": "AC123", "TWILIO_AUTH_TOKEN": "secret"},
            "TwilioFrameSerializer",
        ),
        (
            "telnyx",
            {"TELNYX_API_KEY": "secret"},
            "TelnyxFrameSerializer",
        ),
        (
            "vobiz",
            {"VOBIZ_AUTH_ID": "id", "VOBIZ_AUTH_TOKEN": "secret"},
            "PlivoFrameSerializer",
        ),
        (
            "plivo",
            {"PLIVO_AUTH_ID": "id", "PLIVO_AUTH_TOKEN": "secret"},
            "PlivoFrameSerializer",
        ),
    )
    for provider, environment, serializer_name in cases:
        params = cloud_runtime._telephony_params(
            provider,
            {
                "call_id": "call-123",
                "stream_id": "stream-123",
                "outbound_encoding": "PCMU",
            },
            environment=environment,
            max_duration_s=600,
        )
        assert params.audio_in_sample_rate == 8000
        assert params.audio_out_sample_rate == 8000
        assert params.session_timeout == 630
        assert type(params.serializer).__name__ == serializer_name

    with pytest.raises(VoiceyError, match="unsupported encoding"):
        cloud_runtime._telephony_params(
            "telnyx",
            {
                "call_id": "call-123",
                "stream_id": "stream-123",
                "outbound_encoding": "OPUS",
            },
            environment={"TELNYX_API_KEY": "secret"},
            max_duration_s=600,
        )
    with pytest.raises(VoiceyError, match="omitted call or stream"):
        cloud_runtime._telephony_params(
            "twilio",
            {},
            environment={},
            max_duration_s=600,
        )


def test_cloud_project_loading_and_helpers(tmp_path: Path) -> None:
    _write_project(tmp_path, "pipecat")
    sys.modules.pop("agent", None)
    settings = CloudWorkerSettings.from_environment(
        _environment(tmp_path, "pipecat"),
        expected_runtime="pipecat",
    )
    manifest, agent = cloud_runtime._load_project(settings)
    assert manifest.runtime == "pipecat"
    assert agent.name == "voicey-agent"
    sys.modules.pop("tools", None)
    with cloud_runtime._project_imports(settings.project_root):
        lazy_tools = importlib.import_module("tools")
        assert lazy_tools.IMPORT_MARKER == "project-tools"
        assert str(settings.project_root) in sys.path
    sys.modules.pop("tools", None)
    assert str(settings.project_root) not in sys.path
    assert cloud_runtime._call_data_text({"from": "+14155550100"}, "from") == "+14155550100"
    assert cloud_runtime._integer({}, "VALUE", default=2) == 2
    with pytest.raises(VoiceyError, match="must be an integer"):
        cloud_runtime._integer({"VALUE": "two"}, "VALUE", default=2)
    with pytest.raises(VoiceyError, match="cloud project directory"):
        cloud_runtime._load_project(
            CloudWorkerSettings(
                runtime="pipecat",
                project_root=tmp_path / "missing",
                relay_url=settings.relay_url,
                relay_credential=settings.relay_credential,
            )
        )


@pytest.mark.asyncio
async def test_cloud_transfer_selects_provider_and_closes_resources(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    adapters: list[FakeAdapter] = []

    class FakeClose:
        def __init__(self) -> None:
            self.closed = False

        def close(self) -> None:
            self.closed = True

    class FakeAdapter:
        def __init__(self, **kwargs: object) -> None:
            self.kwargs = kwargs
            self.ledger = FakeClose()
            self._client = FakeClose()
            self.transfers: list[tuple[str, str]] = []
            adapters.append(self)

        def cold_transfer(self, call_id: str, number: str) -> None:
            self.transfers.append((call_id, number))

    import voicey.telephony.plivo as plivo
    import voicey.telephony.telnyx as telnyx
    import voicey.telephony.twilio as twilio
    import voicey.telephony.vobiz as vobiz

    monkeypatch.setattr(twilio, "TwilioAdapter", FakeAdapter)
    monkeypatch.setattr(telnyx, "TelnyxAdapter", FakeAdapter)
    monkeypatch.setattr(vobiz, "VobizAdapter", FakeAdapter)
    monkeypatch.setattr(plivo, "PlivoAdapter", FakeAdapter)
    environments = {
        "twilio": {"TWILIO_ACCOUNT_SID": "AC123", "TWILIO_AUTH_TOKEN": "token"},
        "telnyx": {
            "TELNYX_API_KEY": "token",
            "TELNYX_PUBLIC_KEY": "public",
            "TELNYX_CONNECTION_ID": "connection",
        },
        "vobiz": {"VOBIZ_AUTH_ID": "id", "VOBIZ_AUTH_TOKEN": "token"},
        "plivo": {"PLIVO_AUTH_ID": "id", "PLIVO_AUTH_TOKEN": "token"},
    }
    for provider, environment in environments.items():
        transfer = cloud_runtime._cloud_transfer(
            provider=provider,
            environment=environment,
            scratch_root=tmp_path / provider,
        )
        assert transfer is not None
        await transfer("call-123", "+14155550100")
        transfer.close()
        adapter = adapters[-1]
        assert adapter.transfers == [("call-123", "+14155550100")]
        assert adapter.ledger.closed
        assert adapter._client.closed
        assert adapter.kwargs["ledger_path"] == tmp_path / provider / "telephony.sqlite3"

    assert (
        cloud_runtime._cloud_transfer(
            provider="daily",
            environment={},
            scratch_root=tmp_path,
        )
        is None
    )


@pytest.mark.asyncio
async def test_cloud_workers_reject_project_runtime_mismatch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    def livekit_project(_settings: CloudWorkerSettings) -> tuple[ProjectManifest, Agent]:
        return _manifest("livekit"), _agent("livekit")

    def pipecat_project(_settings: CloudWorkerSettings) -> tuple[ProjectManifest, Agent]:
        return _manifest("pipecat"), _agent("pipecat")

    monkeypatch.setattr(cloud_runtime, "_load_project", livekit_project)
    with pytest.raises(VoiceyError, match="not a Pipecat agent"):
        await run_pipecat_cloud_session(
            object(),
            environment=_environment(tmp_path, "pipecat"),
        )
    monkeypatch.setattr(cloud_runtime, "_load_project", pipecat_project)
    with pytest.raises(VoiceyError, match="not a LiveKit agent"):
        await run_livekit_cloud_worker(
            environment=_environment(tmp_path, "livekit"),
        )


def test_cloud_runtime_main_maps_errors_and_starts_livekit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configured: list[str] = []
    run_calls = 0

    def configure(*, format: str) -> None:
        configured.append(format)

    def run(coroutine: Any) -> None:
        nonlocal run_calls
        run_calls += 1
        coroutine.close()

    monkeypatch.setattr(cloud_runtime, "configure_logging", configure)
    monkeypatch.setattr(cloud_runtime.asyncio, "run", run)
    monkeypatch.setattr(sys, "argv", ["cloud_runtime", "livekit"])
    cloud_runtime.main()
    assert configured == ["json"]
    assert run_calls == 1

    monkeypatch.setattr(sys, "argv", ["cloud_runtime"])
    with pytest.raises(SystemExit) as caught:
        cloud_runtime.main()
    assert caught.value.code == 1
