# pyright: reportPrivateUsage=false

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any, cast

import pytest
from fastapi import FastAPI

from voicekit import Agent, Models, Phone, Results, Web
from voicekit.cli.context import ProjectContext
from voicekit.cli.dev import (
    _load_agent,
    _project_environment,
    _provision_livekit_phone,
    run_dev,
)
from voicekit.config.manifest import ProjectManifest, RecipeSelection
from voicekit.config.models import ModelAxis
from voicekit.errors import VoicekitError
from voicekit.results.signing import encode_secret
from voicekit.telephony.models import PipecatTarget, RollbackToken


def _agent(*, runtime: str = "pipecat") -> Agent:
    return Agent(
        name="dev-agent",
        runtime=runtime,  # type: ignore[arg-type]
        models=Models(
            stt="deepgram/nova-3",
            llm="anthropic/claude-sonnet-5",
            tts="cartesia/sonic-3.5",
        ),
        persona="Help callers.",
        flow="flow:entry",
        tools="tools",
        web=Web(enabled=True, allowed_origins=["http://localhost:5173"]),
        results=Results(
            webhook="https://receiver.example.test/results",
            secret_env="VOICEKIT_WEBHOOK_SECRET",  # pragma: allowlist secret
        ),
    )


def _context(tmp_path: Path, *, phone: bool) -> ProjectContext:
    models: dict[ModelAxis, str] = {
        "stt": "deepgram/nova-3",
        "llm": "anthropic/claude-sonnet-5",
        "tts": "cartesia/sonic-3.5",
    }
    manifest = ProjectManifest(
        project_name="dev-agent",
        runtime="pipecat",
        recipe=RecipeSelection(name="scratch", version="1.0.0"),
        channels=frozenset({"phone", "web"} if phone else {"web"}),
        models=models,
        carriers=["twilio"] if phone else [],
        phone_number="+14155550123" if phone else None,
    )
    return ProjectContext(
        root=tmp_path,
        manifest=manifest,
        checkpoint=False,
        environment={
            "TWILIO_ACCOUNT_SID": "AC" + "1" * 32,
            "TWILIO_AUTH_TOKEN": "token",  # pragma: allowlist secret
            "VOICEKIT_WEBHOOK_SECRET": encode_secret(b"d" * 32),
        },
    )


def _livekit_context(tmp_path: Path) -> ProjectContext:
    context = _context(tmp_path, phone=False)
    assert context.manifest is not None
    return ProjectContext(
        root=context.root,
        manifest=context.manifest.model_copy(update={"runtime": "livekit"}),
        checkpoint=False,
        environment={
            **context.environment,
            "LIVEKIT_URL": "wss://project.livekit.cloud",
            "LIVEKIT_API_KEY": "livekit-key",  # pragma: allowlist secret
            "LIVEKIT_API_SECRET": "livekit-secret",  # pragma: allowlist secret
        },
    )


def test_project_environment_restores_process_values(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VOICEKIT_EXISTING", "before")
    monkeypatch.delenv("VOICEKIT_NEW", raising=False)

    with _project_environment({"VOICEKIT_EXISTING": "during", "VOICEKIT_NEW": "temporary"}):
        import os

        assert os.environ["VOICEKIT_EXISTING"] == "during"
        assert os.environ["VOICEKIT_NEW"] == "temporary"

    assert os.environ["VOICEKIT_EXISTING"] == "before"
    assert "VOICEKIT_NEW" not in os.environ


def test_load_agent_requires_exported_typed_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    good = ModuleType("good_agent")
    good.agent = _agent()  # type: ignore[attr-defined]
    bad = ModuleType("bad_agent")
    bad.agent = object()  # type: ignore[attr-defined]

    def import_module(name: str) -> ModuleType:
        return good if name == "good_agent" else bad

    monkeypatch.setattr("voicekit.cli.dev.importlib.import_module", import_module)

    assert _load_agent("good_agent").name == "dev-agent"
    with pytest.raises(VoicekitError) as caught:
        _load_agent("bad_agent")
    assert caught.value.code == "VK-CLI-007"


@pytest.mark.asyncio
async def test_phone_dev_supervisor_points_probes_and_restores(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    events: list[str] = []

    class FakeRepository:
        def __init__(self, path: Path) -> None:
            assert path.name == "calls.sqlite3"

        async def __aenter__(self) -> FakeRepository:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return

    class FakeTunnel:
        public_url = "https://public.example.test"

        async def close(self) -> None:
            events.append("tunnel-closed")

    class FakeTunnelManager:
        def __init__(self, *, environment: Mapping[str, str]) -> None:
            assert environment["TWILIO_AUTH_TOKEN"]

        async def open(self, port: int, **_kwargs: object) -> FakeTunnel:
            assert port == 7860
            events.append("tunnel-opened")
            return FakeTunnel()

    class FakeProbe:
        def install(self, app: FastAPI) -> None:
            assert isinstance(app, FastAPI)
            events.append("probe-installed")

        async def verify(self, url: str, *, timeout_s: float) -> None:
            assert url == "https://public.example.test"
            assert timeout_s == 15
            events.append("probe-verified")

    class FakeLedger:
        def close(self) -> None:
            events.append("ledger-closed")

    class FakeAdapter:
        ledger = FakeLedger()

        def __init__(self, **_kwargs: object) -> None:
            return

        def point_inbound(
            self,
            number: str,
            target: PipecatTarget,
        ) -> RollbackToken:
            assert number == "+14155550123"
            assert target.https_base == "https://public.example.test"
            events.append("pointed")
            return RollbackToken(provider="twilio", token="route_test")

        def restore(self, token: RollbackToken) -> None:
            assert token.token == "route_test"
            events.append("restored")

    class FakeHost:
        def __init__(self, **kwargs: Any) -> None:
            self.app = FastAPI()
            settings = kwargs["settings"]
            assert settings.pending_media_timeout_s == 120
            self.target = PipecatTarget(settings.public_base)

        async def reserve_web_call(self) -> str:
            return "call_web_test"

    class FakeServer:
        def __init__(self, _config: object) -> None:
            self.started = False
            self.should_exit = False

        async def serve(self) -> None:
            self.started = True
            await asyncio.sleep(0)
            events.append("served")

    def load_agent(_name: str) -> Agent:
        return _agent()

    def config(*_args: object, **_kwargs: object) -> SimpleNamespace:
        events.append(f"config:{_kwargs['port']}")
        return SimpleNamespace()

    monkeypatch.setattr("voicekit.cli.dev._load_agent", load_agent)
    monkeypatch.setattr("voicekit.cli.dev.SQLiteRepository", FakeRepository)
    monkeypatch.setattr("voicekit.cli.dev.TunnelManager", FakeTunnelManager)
    monkeypatch.setattr("voicekit.cli.dev.TunnelProbe", FakeProbe)
    monkeypatch.setattr("voicekit.cli.dev.TwilioAdapter", FakeAdapter)
    monkeypatch.setattr("voicekit.cli.dev.PipecatHost", FakeHost)
    monkeypatch.setattr("voicekit.cli.dev.uvicorn.Server", FakeServer)
    monkeypatch.setattr("voicekit.cli.dev.uvicorn.Config", config)

    await run_dev(
        _context(tmp_path, phone=True),
        phone=True,
        tunnel="cloudflared",
        public_url=None,
        port=7860,
        notice=lambda message: events.append(f"notice:{message}"),
    )

    assert events.index("pointed") < events.index("restored")
    assert events.index("probe-verified") < events.index("pointed")
    assert "config:7860" in events
    assert "config:7861" in events
    assert "notice:Playground: http://127.0.0.1:7861" in events
    assert "notice:Tunnel scope: public listener only; admin routes stay local." in events
    assert events[-2:] == ["ledger-closed", "tunnel-closed"]


@pytest.mark.asyncio
async def test_livekit_dev_supervises_worker_token_and_admin_listeners(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    events: list[str] = []

    class FakeRepository:
        def __init__(self, path: Path) -> None:
            assert path.name == "calls.sqlite3"

        async def open(self) -> FakeRepository:
            return self

        async def __aenter__(self) -> FakeRepository:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return

    class FakeLiveKitHost:
        def __init__(self, **kwargs: Any) -> None:
            settings = kwargs["settings"]
            assert settings.health_port == 7862
            assert settings.num_idle_processes == 0
            events.append("host-created")

        async def run(self, *, devmode: bool) -> None:
            assert devmode is True
            events.append("worker-ran")

        async def drain(self) -> None:
            events.append("worker-drained")

        async def reserve_web_call(self) -> str:
            return "call_web_livekit"

        async def fail_web_reservation(self, call_id: str) -> None:
            assert call_id == "call_web_livekit"

        async def reload_agent(self, agent: Agent, *, restart_runner: bool) -> bool:
            del agent, restart_runner
            return True

    class FakeTokenIssuer:
        def __init__(self, **kwargs: object) -> None:
            assert kwargs["server_url"] == "wss://project.livekit.cloud"
            assert kwargs["ttl_s"] == 120
            events.append("issuer-created")

    class FakePlayground:
        def __init__(self, **kwargs: Any) -> None:
            assert kwargs["settings"].connect_origins == ("wss://project.livekit.cloud",)
            assert kwargs["room_token_issuer"].__class__ is FakeTokenIssuer
            self.admin_app = FastAPI()
            events.append("playground-created")

        def update_agent(self, _agent: Agent) -> None:
            return

    class FakeReloads:
        def __init__(self, **_kwargs: object) -> None:
            self.on_loaded = None

        def snapshot(self) -> dict[str, object]:
            return {"revision": 0, "state": "ready", "message": None}

        async def watch(self, stop: asyncio.Event) -> None:
            await stop.wait()

    class FakeServer:
        def __init__(self, _config: object) -> None:
            self.started = False
            self.should_exit = False

        async def serve(self) -> None:
            self.started = True
            await asyncio.sleep(0)
            events.append("served")

    def config(*_args: object, **kwargs: object) -> SimpleNamespace:
        events.append(f"config:{kwargs['port']}")
        return SimpleNamespace()

    import voicekit.runtimes.livekit as livekit_runtime

    def load_livekit_agent(_name: str) -> Agent:
        return _agent(runtime="livekit")

    monkeypatch.setattr("voicekit.cli.dev._load_agent", load_livekit_agent)
    monkeypatch.setattr("voicekit.cli.dev.SQLiteRepository", FakeRepository)
    monkeypatch.setattr("voicekit.cli.dev.PlaygroundService", FakePlayground)
    monkeypatch.setattr("voicekit.cli.dev.ReloadController", FakeReloads)
    monkeypatch.setattr("voicekit.cli.dev.uvicorn.Server", FakeServer)
    monkeypatch.setattr("voicekit.cli.dev.uvicorn.Config", config)
    monkeypatch.setattr(livekit_runtime, "LiveKitHost", FakeLiveKitHost)
    monkeypatch.setattr(livekit_runtime, "LiveKitTokenIssuer", FakeTokenIssuer)

    await run_dev(
        _livekit_context(tmp_path),
        phone=False,
        tunnel="auto",
        public_url=None,
        port=7860,
        notice=lambda message: events.append(f"notice:{message}"),
    )

    assert "host-created" in events
    assert "issuer-created" in events
    assert "playground-created" in events
    assert "worker-ran" in events
    assert "worker-drained" in events
    assert "config:7860" in events
    assert "config:7861" in events
    assert "notice:LiveKit worker health: http://127.0.0.1:7862" in events


@pytest.mark.asyncio
async def test_livekit_phone_provisioning_builds_native_sip_resources_and_cleans_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    events: list[object] = []

    class FakeLiveKitAPI:
        sip = "livekit-sip-service"

        def __init__(self, url: str, api_key: str, api_secret: str) -> None:
            events.append(("livekit", url, api_key, api_secret))

        async def aclose(self) -> None:
            events.append("livekit-close")

    class FakeTwilioClient:
        def __init__(self, account_sid: str, auth_token: str) -> None:
            events.append(("twilio", account_sid, auth_token))

    class FakeBackend:
        def __init__(self, client: object) -> None:
            events.append(("backend", client.__class__.__name__))

    class FakeLedger:
        def __init__(self, path: Path) -> None:
            events.append(("ledger", path.name))

        def close(self) -> None:
            events.append("ledger-close")

    class FakeConfig:
        def __init__(self, **values: object) -> None:
            self.values = values
            events.append(("config", values))

    class FakeProvisioner:
        fail = False

        def __init__(self, *, livekit: object, twilio: object, ledger: object) -> None:
            events.append(
                ("provisioner", livekit, twilio.__class__.__name__, ledger.__class__.__name__)
            )

        async def provision(self, config: FakeConfig) -> object:
            events.append(("provision", config.values))
            if self.fail:
                raise RuntimeError("provisioning failed")
            return SimpleNamespace(operation_id="sip-operation")

    monkeypatch.setattr("livekit.api.LiveKitAPI", FakeLiveKitAPI)
    monkeypatch.setattr("twilio.rest.Client", FakeTwilioClient)
    monkeypatch.setattr(
        "voicekit.runtimes.livekit.sip.TwilioElasticSipBackend",
        FakeBackend,
    )
    monkeypatch.setattr(
        "voicekit.runtimes.livekit.sip.TwilioLiveKitSipConfig",
        FakeConfig,
    )
    monkeypatch.setattr(
        "voicekit.runtimes.livekit.sip.TwilioLiveKitSipProvisioner",
        FakeProvisioner,
    )
    monkeypatch.setattr("voicekit.telephony.ledger.TelephonyLedger", FakeLedger)

    base = _context(tmp_path, phone=True)
    assert base.manifest is not None
    context = ProjectContext(
        root=base.root,
        manifest=base.manifest.model_copy(update={"runtime": "livekit"}),
        checkpoint=False,
        environment={
            **base.environment,
            "VOICEKIT_LIVEKIT_SIP_URI": "sip:project.sip.livekit.cloud",
            "VOICEKIT_TWILIO_SIP_DOMAIN": "voicekit.pstn.twilio.com",
            "VOICEKIT_TWILIO_SIP_USERNAME": "voicekit-user",
            "VOICEKIT_TWILIO_SIP_PASSWORD": "voicekit-password",  # pragma: allowlist secret
        },
    )
    agent = _agent(runtime="livekit").model_copy(
        update={
            "phone": Phone(
                provider="twilio",
                number="+14155550123",
                record=True,
            )
        }
    )

    provisioner, operation_id, livekit_client, ledger = await _provision_livekit_phone(
        context,
        agent=agent,
        api_key="api-key",  # pragma: allowlist secret
        api_secret="api-secret",  # pragma: allowlist secret
        server_url="wss://project.livekit.cloud",
    )

    assert isinstance(provisioner, FakeProvisioner)
    assert operation_id == "sip-operation"
    assert isinstance(livekit_client, FakeLiveKitAPI)
    assert isinstance(ledger, FakeLedger)
    config_event = next(
        cast("tuple[str, dict[str, object]]", event)
        for event in events
        if isinstance(event, tuple) and event[0] == "config"
    )
    assert config_event[1]["record"] is True
    assert config_event[1]["number"] == "+14155550123"

    FakeProvisioner.fail = True
    with pytest.raises(RuntimeError, match="provisioning failed"):
        await _provision_livekit_phone(
            context,
            agent=agent,
            api_key="api-key",  # pragma: allowlist secret
            api_secret="api-secret",  # pragma: allowlist secret
            server_url="wss://project.livekit.cloud",
        )
    assert events[-2:] == ["livekit-close", "ledger-close"]


@pytest.mark.asyncio
async def test_dev_rejects_phone_flag_for_web_only_project(tmp_path: Path) -> None:
    with pytest.raises(VoicekitError) as caught:
        await run_dev(
            _context(tmp_path, phone=False),
            phone=True,
            tunnel="auto",
            public_url=None,
            port=7860,
            notice=lambda _message: None,
        )
    assert caught.value.code == "VK-CLI-007"


@pytest.mark.asyncio
async def test_dev_reserves_adjacent_loopback_admin_port(tmp_path: Path) -> None:
    with pytest.raises(VoicekitError, match="admin listener") as caught:
        await run_dev(
            _context(tmp_path, phone=False),
            phone=False,
            tunnel="auto",
            public_url=None,
            port=65535,
            notice=lambda _message: None,
        )
    assert caught.value.code == "VK-CLI-010"
