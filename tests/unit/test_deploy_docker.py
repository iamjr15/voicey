# pyright: reportPrivateUsage=false

from __future__ import annotations

import asyncio
import signal
import stat
from collections.abc import Callable
from pathlib import Path
from types import FrameType, SimpleNamespace
from typing import Any, cast

import httpx
import pytest
from fastapi import FastAPI

from voicekit import Agent, Limits, Models, Phone, Results, Web
from voicekit.config.manifest import ManifestStore, ProjectManifest, RecipeSelection
from voicekit.config.models import ModelAxis
from voicekit.deploy import runtime as runtime_module
from voicekit.deploy.docker import (
    DockerDeploymentGenerator,
    DockerSmokeVerifier,
)
from voicekit.deploy.persistence import (
    PersistencePreflightReport,
    RollingGenerationReport,
    docker_persistence_preflight,
    rolling_generation_invariant,
)
from voicekit.deploy.runtime import ContainerSettings
from voicekit.errors import VoicekitError


def _wheel(path: Path) -> Path:
    wheel = path / "voicekit-0.0.0.dev0-py3-none-any.whl"
    wheel.write_bytes(b"wheel")
    return wheel


def _docker_project(path: Path) -> Path:
    path.mkdir()
    models: dict[ModelAxis, str] = {
        "stt": "deepgram/nova-3",
        "llm": "anthropic/claude-sonnet-5",
        "tts": "cartesia/sonic-3.5",
    }
    ManifestStore(path / "voicekit.jsonc").save(
        ProjectManifest(
            project_name="docker-test",
            runtime="pipecat",
            recipe=RecipeSelection(name="scratch", version="1.0.0"),
            channels=frozenset({"web"}),
            models=models,
        )
    )
    (path / "pyproject.toml").write_text(
        '[project]\nname = "docker-test"\nversion = "0.0.0"\n'
        'dependencies = ["voicekit[pipecat]", "httpx>=0.28"]\n',
        encoding="utf-8",
    )
    return path


def test_generator_emits_idempotent_secret_free_hardened_artifacts(tmp_path: Path) -> None:
    project = _docker_project(tmp_path / "project")
    wheel = _wheel(tmp_path)
    generator = DockerDeploymentGenerator(project)

    first = generator.generate(engine_wheel=wheel)
    second = generator.generate(engine_wheel=wheel)

    assert first == second
    assert first.engine_wheel is not None
    assert first.project_requirements.read_text(encoding="utf-8") == "httpx>=0.28\n"
    assert stat.S_IMODE(first.engine_wheel.stat().st_mode) == 0o600
    dockerfile = first.dockerfile.read_text(encoding="utf-8")
    compose = first.compose.read_text(encoding="utf-8")
    ignored = first.dockerignore.read_text(encoding="utf-8")
    combined = dockerfile + compose + ignored
    assert "USER 10001:10001" in dockerfile
    assert 'CMD ["python", "-m", "voicekit.deploy.runtime"]' in dockerfile
    assert "HEALTHCHECK" in dockerfile
    assert "init: true" in compose
    assert "read_only: true" in compose
    assert "no-new-privileges:true" in compose
    assert "stop_grace_period" in compose
    assert "14460s" in compose
    assert 'NLTK_DATA="/opt/nltk_data"' in dockerfile
    assert "test -f /opt/nltk_data/tokenizers/punkt_tab/english/abbrev_types.txt" in dockerfile
    assert "driver: local" in compose
    assert ".env*" in ignored
    assert "secret-value" not in combined


def test_generator_rejects_conflicts_bad_wheels_and_unpublished_default(
    tmp_path: Path,
) -> None:
    project = _docker_project(tmp_path / "project")
    generator = DockerDeploymentGenerator(project)

    with pytest.raises(VoicekitError) as missing:
        generator.generate()
    assert missing.value.code == "VK-DEP-003"

    bad = tmp_path / "other.whl"
    bad.write_bytes(b"not voicekit")
    with pytest.raises(VoicekitError) as invalid:
        generator.generate(engine_wheel=bad)
    assert invalid.value.code == "VK-DEP-003"

    (project / "Dockerfile.voicekit").write_text("owned\n", encoding="utf-8")
    with pytest.raises(VoicekitError) as conflict:
        generator.generate(engine_wheel=_wheel(tmp_path))
    assert conflict.value.code == "VK-DEP-001"


def test_generator_validation_maps_missing_docker_and_compose_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    project = _docker_project(tmp_path / "project")
    (project / ".env").write_text("VOICEKIT_PUBLIC_BASE=https://voice.example\n")
    generator = DockerDeploymentGenerator(project)
    artifacts = generator.generate(engine_wheel=_wheel(tmp_path))

    def missing_docker(_name: str) -> None:
        return None

    monkeypatch.setattr("voicekit.deploy.docker.shutil.which", missing_docker)
    with pytest.raises(VoicekitError) as missing:
        generator.validate(artifacts)
    assert missing.value.code == "VK-DEP-005"

    class Completed:
        returncode = 1
        stderr = "invalid compose"
        stdout = ""

    def docker_path(_name: str) -> str:
        return "/bin/docker"

    def failed_compose(*_args: object, **_kwargs: object) -> Completed:
        return Completed()

    monkeypatch.setattr("voicekit.deploy.docker.shutil.which", docker_path)
    monkeypatch.setattr(
        "voicekit.deploy.docker.subprocess.run",
        failed_compose,
    )
    with pytest.raises(VoicekitError, match="invalid compose"):
        generator.validate(artifacts)


async def test_local_persistence_preflight_and_rolling_generation(tmp_path: Path) -> None:
    data = tmp_path / "data"
    mountinfo = tmp_path / "mountinfo"
    mountinfo.write_text(
        f"1 0 0:1 / {tmp_path} rw,relatime - ext4 /dev/test rw\n",
        encoding="utf-8",
    )

    report = await docker_persistence_preflight(
        data,
        deploy_target="docker",
        storage_backend="sqlite",
        sqlite_local_only=True,
        replica_count=1,
        mountinfo_path=mountinfo,
    )
    rolling = await rolling_generation_invariant(data)

    assert report.filesystem_type == "ext4"
    assert report.journal_mode == "wal"
    assert report.synchronous == 2
    assert report.schema_ready
    assert report.artifact_round_trip
    assert rolling.new_generation == rolling.old_generation + 1
    assert rolling.stale_writer_rejected
    assert rolling.terminal_event_count == 1
    assert not tuple(data.glob(".rolling-*.sqlite3*"))


@pytest.mark.parametrize(
    ("target", "backend", "local_only", "replicas"),
    [
        ("fly", "sqlite", True, 1),
        ("docker", "postgres", True, 1),
        ("docker", "sqlite", False, 1),
        ("docker", "sqlite", True, 2),
    ],
)
async def test_persistence_preflight_rejects_invalid_storage_matrix(
    tmp_path: Path,
    target: str,
    backend: str,
    local_only: bool,
    replicas: int,
) -> None:
    with pytest.raises(VoicekitError) as caught:
        await docker_persistence_preflight(
            tmp_path / "data",
            deploy_target=target,
            storage_backend=backend,
            sqlite_local_only=local_only,
            replica_count=replicas,
        )
    assert caught.value.code == "VK-DEP-002"


async def test_persistence_preflight_rejects_remote_mount_and_symlink(tmp_path: Path) -> None:
    data = tmp_path / "data"
    mountinfo = tmp_path / "mountinfo"
    mountinfo.write_text(
        f"1 0 0:1 / {tmp_path} rw,relatime - nfs4 server:/data rw\n",
        encoding="utf-8",
    )
    with pytest.raises(VoicekitError, match="remote filesystem"):
        await docker_persistence_preflight(
            data,
            deploy_target="docker",
            storage_backend="sqlite",
            sqlite_local_only=True,
            replica_count=1,
            mountinfo_path=mountinfo,
        )

    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "link"
    link.symlink_to(target, target_is_directory=True)
    with pytest.raises(VoicekitError, match="symbolic link"):
        await docker_persistence_preflight(
            link,
            deploy_target="docker",
            storage_backend="sqlite",
            sqlite_local_only=True,
            replica_count=1,
        )


async def test_smoke_verifier_requires_ready_contract_and_secure_remote_url() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/health"
        return httpx.Response(
            200,
            json={
                "ok": True,
                "runtime": "pipecat",
                "active_calls": 2,
                "accepting": True,
                "storage_ready": True,
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await DockerSmokeVerifier(client).verify("https://voice.example/")
    assert result.url == "https://voice.example"
    assert result.active_calls == 2

    async def not_ready(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": False})

    async with httpx.AsyncClient(transport=httpx.MockTransport(not_ready)) as client:
        with pytest.raises(VoicekitError) as caught:
            await DockerSmokeVerifier(client).verify("http://localhost:7860")
    assert caught.value.code == "VK-DEP-004"
    with pytest.raises(VoicekitError):
        await DockerSmokeVerifier().verify("http://voice.example")


def test_container_settings_validate_environment_only_topology(tmp_path: Path) -> None:
    values = {
        "VOICEKIT_PUBLIC_BASE": "https://voice.example/base",
        "VOICEKIT_DATA_DIR": "/app/data",
        "VOICEKIT_PORT": "7860",
        "VOICEKIT_ADMIN_PORT": "7861",
        "VOICEKIT_ADMIN_ORIGIN": "http://agent:7861",
        "VOICEKIT_DEPLOY_TARGET": "docker",
        "VOICEKIT_STORAGE_BACKEND": "sqlite",
        "VOICEKIT_SQLITE_LOCAL_ONLY": "1",
        "VOICEKIT_REPLICA_COUNT": "1",
        "VOICEKIT_TRUSTED_PROXY_IPS": "127.0.0.1,::1",
        "VOICEKIT_TRUSTED_PROXY_CIDRS": "127.0.0.0/8,::1/128",
        "VOICEKIT_INTEGRATOR_SECRET": "integrator",  # pragma: allowlist secret
    }
    settings = ContainerSettings.from_environment(values, project_root=tmp_path)

    assert settings.public_port == 7860
    assert settings.sqlite_local_only
    assert settings.trusted_proxy_ips == frozenset({"127.0.0.1", "::1"})
    assert settings.trusted_proxy_cidrs == ("127.0.0.0/8", "::1/128")
    assert runtime_module._origin(settings.public_base) == "https://voice.example"
    assert runtime_module._is_origin(settings.admin_origin)


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("VOICEKIT_PUBLIC_BASE", "http://voice.example"),
        ("VOICEKIT_DATA_DIR", "relative"),
        ("VOICEKIT_PORT", "invalid"),
        ("VOICEKIT_ADMIN_ORIGIN", "https://agent/path"),
        ("VOICEKIT_TRUSTED_PROXY_IPS", "not-an-ip"),
        ("VOICEKIT_TRUSTED_PROXY_CIDRS", "not-a-network"),
    ],
)
def test_container_settings_reject_each_invalid_shape(
    tmp_path: Path,
    name: str,
    value: str,
) -> None:
    values = {
        "VOICEKIT_PUBLIC_BASE": "https://voice.example",
        "VOICEKIT_DATA_DIR": "/app/data",
        "VOICEKIT_DEPLOY_TARGET": "docker",
        "VOICEKIT_STORAGE_BACKEND": "sqlite",
        "VOICEKIT_SQLITE_LOCAL_ONLY": "1",
        "VOICEKIT_REPLICA_COUNT": "1",
    }
    values[name] = value
    with pytest.raises(VoicekitError) as caught:
        ContainerSettings.from_environment(values, project_root=tmp_path)
    assert caught.value.code == "VK-DEP-003"


def test_runtime_helpers_scope_import_path_environment_and_agent(tmp_path: Path) -> None:
    module = tmp_path / "runtime_agent.py"
    module.write_text(
        "from voicekit import Agent, Models, Results, Web\n"
        "agent = Agent(name='runtime-test', runtime='pipecat', "
        "models=Models(stt='deepgram/nova-3', llm='anthropic/claude-sonnet-5', "
        "tts='cartesia/sonic-3.5'), persona='test', flow='flow:entry', tools='tools', "
        "web=Web(enabled=True, allowed_origins=['https://app.example']), "
        "results=Results(webhook='https://receiver.example', secret_env='RESULT_SECRET'))\n",
        encoding="utf-8",
    )
    original: dict[str, str] = {}
    with (
        runtime_module._project_import_path(tmp_path),
        runtime_module._project_environment({"RUNTIME_SCOPED": "yes"}),
    ):
        loaded = runtime_module._load_agent("runtime_agent")
        original["loaded"] = loaded.name
        assert __import__("os").environ["RUNTIME_SCOPED"] == "yes"
    assert original == {"loaded": "runtime-test"}
    assert "RUNTIME_SCOPED" not in __import__("os").environ


def test_runtime_trust_helpers_reject_bad_values() -> None:
    assert runtime_module._trusted_ips("") == frozenset()
    assert runtime_module._trusted_cidrs("") == ()
    with pytest.raises(VoicekitError):
        runtime_module._trusted_ips("invalid")
    with pytest.raises(VoicekitError):
        runtime_module._trusted_cidrs("invalid")
    assert not runtime_module._is_origin("https://example.com/path")
    with pytest.raises(VoicekitError, match="has no origin"):
        runtime_module._origin("relative")


def test_runtime_environment_restores_existing_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RUNTIME_SCOPED", "before")
    with runtime_module._project_environment({"RUNTIME_SCOPED": "during"}):
        assert __import__("os").environ["RUNTIME_SCOPED"] == "during"
    assert __import__("os").environ["RUNTIME_SCOPED"] == "before"


def test_runtime_agent_loader_rejects_missing_and_invalid_exports(tmp_path: Path) -> None:
    (tmp_path / "missing_agent_export.py").write_text("value = 1\n", encoding="utf-8")
    (tmp_path / "invalid_agent_export.py").write_text("agent = object()\n", encoding="utf-8")

    with runtime_module._project_import_path(tmp_path):
        with pytest.raises(VoicekitError, match="export an Agent"):
            runtime_module._load_agent("missing_agent_export")
        with pytest.raises(VoicekitError, match="is not a voicekit Agent"):
            runtime_module._load_agent("invalid_agent_export")


async def test_wait_started_maps_early_exit_and_timeout() -> None:
    exited = asyncio.create_task(asyncio.sleep(0))
    await asyncio.sleep(0)
    with pytest.raises(VoicekitError, match="exited early"):
        await runtime_module._wait_started(cast("Any", SimpleNamespace(started=False)), exited)

    pending = asyncio.create_task(asyncio.sleep(60))
    with pytest.raises(VoicekitError, match="did not become ready"):
        await runtime_module._wait_started(
            cast("Any", SimpleNamespace(started=False)),
            pending,
            timeout_s=0.001,
        )
    pending.cancel()
    await asyncio.gather(pending, return_exceptions=True)


async def test_runtime_signal_handlers_set_event_and_restore(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    installed: dict[object, object] = {}

    def get_handler(_signum: object) -> object:
        return signal.SIG_DFL

    def set_handler(signum: object, handler: object) -> object:
        installed[signum] = handler
        return signal.SIG_DFL

    monkeypatch.setattr(runtime_module.signal, "getsignal", get_handler)
    monkeypatch.setattr(runtime_module.signal, "signal", set_handler)
    shutdown = asyncio.Event()

    restore = runtime_module._install_signal_handlers(shutdown)
    handler = cast(
        "Callable[[int, FrameType | None], None]",
        installed[signal.SIGTERM],
    )
    handler(signal.SIGTERM, None)
    await asyncio.sleep(0)
    assert shutdown.is_set()

    restore()
    assert installed == {
        signal.SIGINT: signal.SIG_DFL,
        signal.SIGTERM: signal.SIG_DFL,
    }


def test_runtime_main_maps_voicekit_startup_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[object] = []

    def configure(*, format: str) -> None:
        events.append(format)

    async def fail() -> None:
        raise VoicekitError("VK-DEP-003", detail="fixture failure")

    class Logger:
        def error(self, event: str, **values: object) -> None:
            events.append((event, values))

    monkeypatch.setattr(runtime_module, "configure_logging", configure)
    monkeypatch.setattr(runtime_module, "run_container", fail)
    monkeypatch.setattr(runtime_module, "_LOG", Logger())

    with pytest.raises(SystemExit) as caught:
        runtime_module.main()
    assert caught.value.code == 1
    assert events == [
        "json",
        (
            "container_start_failed",
            {"error_code": "VK-DEP-003", "detail": "fixture failure"},
        ),
    ]


async def test_delivery_loop_runs_until_stop_and_flushes_once() -> None:
    class Worker:
        def __init__(self) -> None:
            self.calls = 0

        async def run_once(self) -> object:
            self.calls += 1
            return object()

    worker = Worker()
    stop = asyncio.Event()
    task = asyncio.create_task(runtime_module._delivery_loop(cast("Any", worker), stop))
    await asyncio.sleep(0)
    stop.set()
    await task
    assert worker.calls == 2


def _runtime_agent(*, phone: Phone | None = None, web: bool = True) -> Agent:
    return Agent(
        name="docker-runtime-test",
        runtime="pipecat",
        models=Models(
            stt="deepgram/nova-3",
            llm="anthropic/claude-sonnet-5",
            tts="cartesia/sonic-3.5",
        ),
        persona="Test.",
        flow="flow:entry",
        tools="tools",
        phone=phone,
        web=(Web(enabled=True, allowed_origins=["https://app.example"]) if web else Web()),
        results=Results(
            webhook="https://receiver.example/results",
            secret_env="VOICEKIT_WEBHOOK_SECRET",  # pragma: allowlist secret
        ),
        limits=Limits(max_duration_s=10, silence_hangup_s=5),
    )


def _container_settings(tmp_path: Path, *, integrator: bool = True) -> ContainerSettings:
    return ContainerSettings(
        project_root=tmp_path,
        data_dir=tmp_path / "data",
        public_base="https://voice.example",
        public_port=7860,
        admin_port=7861,
        admin_origin="http://agent:7861",
        deploy_target="docker",
        storage_backend="sqlite",
        sqlite_local_only=True,
        replica_count=1,
        trusted_proxy_ips=frozenset({"127.0.0.1"}),
        trusted_proxy_cidrs=("127.0.0.0/8",),
        integrator_secret=("integrator" if integrator else None),
    )


def _preflight(tmp_path: Path) -> PersistencePreflightReport:
    data = tmp_path / "data"
    data.mkdir(mode=0o700, exist_ok=True)
    return PersistencePreflightReport(
        data_dir=data,
        database_path=data / "calls.sqlite3",
        artifact_root=data / "artifacts",
        filesystem_type="ext4",
        journal_mode="wal",
        synchronous=2,
        schema_ready=True,
        artifact_round_trip=True,
    )


async def test_run_container_orders_preflight_invariant_load_and_serve(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _docker_project(tmp_path / "project")
    project = tmp_path / "project"
    events: list[str] = []
    report = _preflight(tmp_path)

    async def preflight(*_args: object, **_kwargs: object) -> PersistencePreflightReport:
        events.append("preflight")
        return report

    async def rolling(_data: Path) -> RollingGenerationReport:
        events.append("rolling")
        return RollingGenerationReport(1, 2, True, 1)

    async def serve(**kwargs: object) -> None:
        assert kwargs["agent"] == _runtime_agent()
        events.append("serve")

    def load_agent(_name: str) -> Agent:
        return _runtime_agent()

    monkeypatch.setattr(runtime_module, "docker_persistence_preflight", preflight)
    monkeypatch.setattr(runtime_module, "rolling_generation_invariant", rolling)
    monkeypatch.setattr(runtime_module, "_load_agent", load_agent)
    monkeypatch.setattr(runtime_module, "_serve", serve)

    await runtime_module.run_container(
        environment={
            "VOICEKIT_PUBLIC_BASE": "https://voice.example",
            "VOICEKIT_DEPLOY_TARGET": "docker",
            "VOICEKIT_STORAGE_BACKEND": "sqlite",
            "VOICEKIT_SQLITE_LOCAL_ONLY": "1",
            "VOICEKIT_REPLICA_COUNT": "1",
            "VOICEKIT_DATA_DIR": str(report.data_dir),
        },
        project_root=project,
    )
    assert events == ["preflight", "rolling", "serve"]


async def test_run_container_rejects_livekit_before_runtime_load(tmp_path: Path) -> None:
    project = _docker_project(tmp_path / "project")
    manifest = ManifestStore(project / "voicekit.jsonc").load()
    ManifestStore(project / "voicekit.jsonc").save(
        manifest.model_copy(update={"runtime": "livekit"})
    )
    with pytest.raises(VoicekitError) as caught:
        await runtime_module.run_container(
            environment={
                "VOICEKIT_PUBLIC_BASE": "https://voice.example",
                "VOICEKIT_DEPLOY_TARGET": "docker",
                "VOICEKIT_STORAGE_BACKEND": "sqlite",
                "VOICEKIT_SQLITE_LOCAL_ONLY": "1",
                "VOICEKIT_REPLICA_COUNT": "1",
                "VOICEKIT_DATA_DIR": str(tmp_path / "data"),
            },
            project_root=project,
        )
    assert caught.value.code == "VK-DEP-003"


async def test_runtime_serve_builds_secure_web_boundary_and_closes_delivery(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    observed: dict[str, object] = {}

    async def supervise(**kwargs: object) -> None:
        observed.update(kwargs)

    monkeypatch.setattr(runtime_module, "_supervise", supervise)
    settings = _container_settings(tmp_path)
    environment = {
        "VOICEKIT_WEBHOOK_SECRET": "whsec_Zml4dHVyZS1zZWNyZXQ=",  # pragma: allowlist secret
    }

    await runtime_module._serve(
        settings=settings,
        agent=_runtime_agent(),
        preflight=_preflight(tmp_path),
        environment=environment,
    )

    assert observed["admin_app"] is not None
    host = cast("Any", observed["host"])
    assert host.settings.storage_ready
    assert host.web_sessions is not None


async def test_runtime_serve_phone_builds_supported_carriers_and_requires_credentials(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    observed: dict[str, object] = {}

    async def supervise(**kwargs: object) -> None:
        observed.update(kwargs)

    monkeypatch.setattr(runtime_module, "_supervise", supervise)
    settings = _container_settings(tmp_path, integrator=False)
    environment = {
        "VOICEKIT_WEBHOOK_SECRET": "whsec_Zml4dHVyZS1zZWNyZXQ=",  # pragma: allowlist secret
        "TWILIO_ACCOUNT_SID": "AC" + ("1" * 32),
        "TWILIO_AUTH_TOKEN": "fixture",  # pragma: allowlist secret
    }
    await runtime_module._serve(
        settings=settings,
        agent=_runtime_agent(
            phone=Phone(provider="twilio", number="+14155550123"),
            web=False,
        ),
        preflight=_preflight(tmp_path),
        environment=environment,
    )
    assert observed["admin_app"] is None
    assert cast("Any", observed["host"]).twilio is not None

    telnyx_environment = {
        "VOICEKIT_WEBHOOK_SECRET": "whsec_Zml4dHVyZS1zZWNyZXQ=",  # pragma: allowlist secret
        "TELNYX_API_KEY": "fixture",  # pragma: allowlist secret
        "TELNYX_PUBLIC_KEY": "00" * 32,
        "TELNYX_CONNECTION_ID": "connection-fixture",
    }
    observed.clear()
    await runtime_module._serve(
        settings=settings,
        agent=_runtime_agent(
            phone=Phone(provider="telnyx", number="+14155550123"),
            web=False,
        ),
        preflight=_preflight(tmp_path),
        environment=telnyx_environment,
    )
    telnyx_host = cast("Any", observed["host"])
    assert telnyx_host.twilio is None
    assert telnyx_host.telnyx is not None

    vobiz_environment = {
        "VOICEKIT_WEBHOOK_SECRET": "whsec_Zml4dHVyZS1zZWNyZXQ=",  # pragma: allowlist secret
        "VOBIZ_AUTH_ID": "MA_VOBIZTEST",
        "VOBIZ_AUTH_TOKEN": "fixture",  # pragma: allowlist secret
    }
    observed.clear()
    await runtime_module._serve(
        settings=settings,
        agent=_runtime_agent(
            phone=Phone(provider="vobiz", number="+14155550123"),
            web=False,
        ),
        preflight=_preflight(tmp_path),
        environment=vobiz_environment,
    )
    vobiz_host = cast("Any", observed["host"])
    assert vobiz_host.twilio is None
    assert vobiz_host.telnyx is None
    assert vobiz_host.vobiz is not None

    with pytest.raises(VoicekitError) as carrier:
        await runtime_module._serve(
            settings=settings,
            agent=_runtime_agent(
                phone=Phone(provider="telnyx", number="+14155550123"),
                web=False,
            ),
            preflight=_preflight(tmp_path),
            environment=environment,
        )
    assert carrier.value.code == "VK-DEP-003"


async def test_runtime_serve_requires_web_integrator_and_result_secret(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    async def supervise(**_kwargs: object) -> None:
        return

    monkeypatch.setattr(runtime_module, "_supervise", supervise)
    with pytest.raises(VoicekitError, match="VOICEKIT_WEBHOOK_SECRET"):
        await runtime_module._serve(
            settings=_container_settings(tmp_path),
            agent=_runtime_agent(),
            preflight=_preflight(tmp_path),
            environment={},
        )
    with pytest.raises(VoicekitError, match="INTEGRATOR"):
        await runtime_module._serve(
            settings=_container_settings(tmp_path, integrator=False),
            agent=_runtime_agent(),
            preflight=_preflight(tmp_path),
            environment={
                "VOICEKIT_WEBHOOK_SECRET": (
                    "whsec_Zml4dHVyZS1zZWNyZXQ="  # pragma: allowlist secret
                )
            },
        )


async def test_supervisor_owns_signal_drains_then_stops_both_services(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Server:
        def __init__(self, port: int) -> None:
            self.config = SimpleNamespace(port=port)
            self.started = False
            self._should_exit = False
            self.stopped = asyncio.Event()

        @property
        def should_exit(self) -> bool:
            return self._should_exit

        @should_exit.setter
        def should_exit(self, value: bool) -> None:
            self._should_exit = value
            if value:
                self.stopped.set()

        async def serve(self) -> None:
            self.started = True
            await self.stopped.wait()

    class Host:
        def __init__(self) -> None:
            self.app = FastAPI()
            self.agent = SimpleNamespace(limits=SimpleNamespace(max_duration_s=10))
            self.calls: list[str] = []

        async def begin_drain(self) -> None:
            self.calls.append("begin")

        async def drain(self, *, timeout_s: float) -> object:
            assert timeout_s == 10
            self.calls.append("drain")
            return SimpleNamespace(
                active_at_start=0,
                pending_at_start=0,
                forced_sessions=0,
                remaining_calls=0,
            )

    class Worker:
        def __init__(self) -> None:
            self.calls = 0

        async def run_once(self) -> object:
            self.calls += 1
            return object()

    servers: list[Server] = []

    def server(_app: FastAPI, *, port: int) -> Server:
        created = Server(port)
        servers.append(created)
        return created

    def signals(event: asyncio.Event) -> object:
        event.set()
        return lambda: None

    monkeypatch.setattr(runtime_module, "_server", server)
    monkeypatch.setattr(runtime_module, "_install_signal_handlers", signals)
    host = Host()
    worker = Worker()
    await runtime_module._supervise(
        host=cast("Any", host),
        admin_app=FastAPI(),
        delivery=cast("Any", worker),
        settings=_container_settings(Path("/tmp")),
    )

    assert host.calls == ["begin", "drain"]
    assert [server.config.port for server in servers] == [7860, 7861]
    assert all(server.should_exit for server in servers)
    assert worker.calls >= 1
