# pyright: reportPrivateUsage=false

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI

from voicekit import Agent, Models, Results, Web
from voicekit.cli.context import ProjectContext
from voicekit.cli.dev import _load_agent, _project_environment, run_dev
from voicekit.config.manifest import ProjectManifest, RecipeSelection
from voicekit.config.models import ModelAxis
from voicekit.errors import VoicekitError
from voicekit.telephony.models import PipecatTarget, RollbackToken


def _agent() -> Agent:
    return Agent(
        name="dev-agent",
        runtime="pipecat",
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

        async def open(self, *_args: object, **_kwargs: object) -> FakeTunnel:
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
            self.target = PipecatTarget(settings.public_base)

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
    assert events[-2:] == ["ledger-closed", "tunnel-closed"]


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
