# pyright: reportPrivateUsage=false

from __future__ import annotations

import stat
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from voicey.config.manifest import ManifestStore, ProjectManifest
from voicey.deploy.cloud import (
    CloudArtifactGenerator,
    CloudCommandResult,
    CloudResourceState,
    CloudResourceStore,
    LiveKitCloudDeploymentManager,
    LiveKitCloudPlan,
    PipecatCloudDeploymentManager,
    PipecatCloudPlan,
    PlatformCliRunner,
    _current_version,
    _livekit_agent_id,
    _pipecat_agent_exists,
    _pipecat_image,
    _project_requirements,
    _require_livekit_project,
    _require_ready,
    _require_region,
    _require_secret_names,
    _runtime_extras,
    _secret_file,
    _session_id,
    _stage_wheel,
    _wait_relay_call,
)
from voicey.errors import VoiceyError
from voicey.relay.auth import RelayCredential


class FakeRelayClient:
    opens = 0

    def __init__(self, _url: str, _credential: RelayCredential) -> None:
        self.closed = False

    async def __aenter__(self) -> FakeRelayClient:
        type(self).opens += 1
        return self

    async def __aexit__(self, *_exc: object) -> None:
        self.closed = True

    async def open(self) -> FakeRelayClient:
        type(self).opens += 1
        return self

    async def close(self) -> None:
        self.closed = True

    async def get_call(self, _call_id: str) -> object:
        return SimpleNamespace(ended_at=datetime.now(UTC))


class FakePipecatCloudRunner:
    def __init__(self, *, exists: bool = False) -> None:
        self.exists = exists
        self.ready = exists
        self.image = "registry.example.test/voicey/agent:sha-123"
        self.commands: list[tuple[str, ...]] = []
        self.secret_names: set[str] = set()
        self.secret_file_paths: list[Path] = []

    def run(
        self,
        arguments: Sequence[str],
        *,
        cwd: Path,
        check: bool = True,
        timeout_s: float = 1800,
    ) -> CloudCommandResult:
        del check, timeout_s
        command = tuple(arguments)
        self.commands.append(command)
        if command[:3] == ("cloud", "auth", "whoami"):
            return _result("developer@example.test")
        if command[:3] == ("cloud", "regions", "list"):
            return _result("us-west Oregon\nus-east Virginia")
        if command[:3] == ("cloud", "agent", "status"):
            if not self.exists:
                return _result("No deployment data found for agent with name 'voicey-agent'")
            ready = "True" if self.ready else "False"
            phase = "Active" if self.ready else "Failed"
            return _result(
                "Agent: voicey-agent\n"
                f"Ready: {ready}\n"
                f"Deployment Phase: {phase}\n"
                f"Image: {self.image}"
            )
        if command[:3] == ("cloud", "secrets", "set"):
            path = Path(command[command.index("--file") + 1])
            assert stat.S_IMODE(path.stat().st_mode) == 0o600
            self.secret_file_paths.append(path)
            self.secret_names = {
                line.split("=", maxsplit=1)[0]
                for line in path.read_text(encoding="utf-8").splitlines()
            }
            return _result("Secret set updated")
        if command[:3] == ("cloud", "secrets", "list"):
            return _result("\n".join(sorted(self.secret_names)))
        if command[:2] == ("cloud", "deploy"):
            self.exists = True
            self.ready = True
            self.image = command[3]
            return _result("Agent deployment 'voicey-agent' is ready")
        if command[:3] == ("cloud", "agent", "start"):
            return _result("Agent started\nSession ID: session_123")
        if command[:3] == ("cloud", "agent", "stop"):
            return _result("Session stopped")
        if command[:3] == ("cloud", "agent", "delete"):
            self.exists = False
            self.ready = False
            return _result("Agent deleted")
        raise AssertionError(f"unhandled Pipecat command: {command} in {cwd}")


class FakeLiveKitCloudRunner:
    def __init__(self) -> None:
        self.agent_id = "agent_123456"
        self.commands: list[tuple[str, ...]] = []
        self.secret_file_paths: list[Path] = []
        self.secret_payloads: list[str] = []
        self.deleted = False
        self.rolled_back = False
        self.deployed_versions = 0

    def run(
        self,
        arguments: Sequence[str],
        *,
        cwd: Path,
        check: bool = True,
        timeout_s: float = 1800,
    ) -> CloudCommandResult:
        del check, timeout_s
        command = tuple(arguments)
        self.commands.append(command)
        if command[:2] == ("project", "list"):
            return _result(
                '[{"Name":"voicey-test","ProjectId":"project_123",'
                '"URL":"wss://voicey-test.livekit.cloud",'
                '"APIKey":"redacted","APISecret":"redacted"}]'
            )
        if command[:2] == ("agent", "config"):
            (cwd / "livekit.toml").write_text(
                f'agent_id = "{self.agent_id}"\n',
                encoding="utf-8",
            )
            return _result("Agent config written")
        if command[:2] == ("agent", "versions"):
            return _result("* v1 deployed current\nv0 deployed")
        if command[:2] in {("agent", "create"), ("agent", "deploy")}:
            path = Path(command[command.index("--secrets-file") + 1])
            assert stat.S_IMODE(path.stat().st_mode) == 0o600
            self.secret_file_paths.append(path)
            self.secret_payloads.append(path.read_text(encoding="utf-8"))
            (cwd / "livekit.toml").write_text(
                f'agent_id = "{self.agent_id}"\n',
                encoding="utf-8",
            )
            self.deployed_versions += 1
            return _result(f"Created {self.agent_id}\nStatus: deployed")
        if command[:2] == ("agent", "status"):
            return _result("Status: running")
        if command[:2] == ("agent", "rollback"):
            self.rolled_back = True
            return _result("Rollback deployed")
        if command[:2] == ("agent", "delete"):
            self.deleted = True
            return _result("Agent deleted")
        raise AssertionError(f"unhandled LiveKit command: {command} in {cwd}")


class FakeLiveKitSessionSmoke:
    def __init__(self) -> None:
        self.to_numbers: list[str | None] = []
        self.environments: list[dict[str, str]] = []

    async def run(self, **values: object) -> bool:
        self.to_numbers.append(cast("str | None", values["to_number"]))
        self.environments.append(cast("dict[str, str]", values["environment"]))
        return True


def _result(stdout: str, *, returncode: int = 0) -> CloudCommandResult:
    return CloudCommandResult(returncode=returncode, stdout=stdout)


def _wheel(path: Path) -> Path:
    wheel = path / "voicey-0.0.0.dev0-py3-none-any.whl"
    wheel.write_bytes(b"wheel")
    return wheel


def _project(path: Path, runtime: str) -> None:
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
    persona="Test cloud deployment.",
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
    (path / "flow.py").write_text("def entry(*_args): return None\n", encoding="utf-8")
    (path / "tools.py").write_text("TOOLS = []\n", encoding="utf-8")
    (path / "pyproject.toml").write_text(
        f"""[project]
name = "cloud-fixture"
version = "0.1.0"
dependencies = ["voicey[{runtime}]", "httpx>=0.28,<1"]
""",
        encoding="utf-8",
    )
    ManifestStore(path / "voicey.jsonc").save(
        ProjectManifest.model_validate(
            {
                "project_name": "voicey-agent",
                "runtime": runtime,
                "recipe": {"name": "scratch", "version": "1.0.0"},
                "carriers": [],
                "channels": ["web"],
                "models": {
                    "stt": "deepgram/nova-3",
                    "llm": "anthropic/claude-sonnet-5",
                    "tts": "cartesia/sonic-3.5",
                },
                "deploy_target": None,
                "agent_module": "agent",
            }
        )
    )
    relay = RelayCredential.issue("cloud-key").reveal()
    (path / ".env").write_text(
        "\n".join(
            (
                f"VOICEY_RELAY_CREDENTIAL={relay}",
                "DEEPGRAM_API_KEY=deepgram-test",
                "ANTHROPIC_API_KEY=anthropic-test",
                "CARTESIA_API_KEY=cartesia-test",
                "CUSTOM_TOOL_TOKEN=tool-test",
                "VOICEY_WEBHOOK_SECRET=whsec_not_for_worker",
                "VOICEY_RESULTS_SECRET=whsec_not_for_worker",
                "",
            )
        ),
        encoding="utf-8",
    )
    (path / ".env").chmod(0o600)


def _pcc_plan() -> PipecatCloudPlan:
    return PipecatCloudPlan(
        agent_name="voicey-agent",
        organization="voicey-test",
        region="us-west",
        secret_set="voicey-agent-secrets",
        image="registry.example.test/voicey/agent:sha-123",
        relay_url="https://voicey-results.fly.dev",
        min_agents=1,
        max_agents=4,
        profile="agent-1x",
    )


def _lk_plan(*, agent_id: str | None = None) -> LiveKitCloudPlan:
    return LiveKitCloudPlan(
        agent_name="voicey-agent",
        project="voicey-test",
        region="us-west",
        relay_url="https://voicey-results.fly.dev",
        agent_id=agent_id,
    )


def test_cloud_artifacts_are_runtime_native_nonroot_and_secret_free(tmp_path: Path) -> None:
    project = tmp_path / "pipecat"
    _project(project, "pipecat")
    (project / ".env.parley-backup").write_text("REAL=do-not-copy\n", encoding="utf-8")
    (project / ".npmrc").write_text("//registry/:_authToken=do-not-copy\n", encoding="utf-8")
    artifacts = CloudArtifactGenerator(project).generate(
        "pipecat-cloud",
        engine_wheel=_wheel(tmp_path),
        agent_name="voicey-agent",
        secret_set="voicey-agent-secrets",
        image="registry.example.test/voicey/agent:sha-123",
        region="us-west",
        min_agents=1,
        max_agents=4,
        profile="agent-1x",
    )

    dockerfile = artifacts.dockerfile.read_text(encoding="utf-8")
    config = artifacts.platform_config.read_text(encoding="utf-8")  # type: ignore[union-attr]
    bot = artifacts.bot.read_text(encoding="utf-8")  # type: ignore[union-attr]
    assert "USER 10001:10001" in dockerfile
    assert "FROM dailyco/pipecat-base:0.1.0-py3.13" in dockerfile
    assert "VOICEY_PROJECT_ROOT=/voicey/project" in dockerfile
    assert "PORT=8080" in dockerfile
    assert 'HOME="/tmp"' in dockerfile
    assert 'NLTK_DATA="/opt/nltk_data"' in dockerfile
    assert "test -f /opt/nltk_data/tokenizers/punkt_tab/english/abbrev_types.txt" in dockerfile
    assert "CMD" not in dockerfile
    assert "voicey.deploy.cloud_runtime" in bot
    assert "pipecat.runner.run" not in bot
    assert 'agent_name = "voicey-agent"' in config
    assert "CUSTOM_TOOL_TOKEN" not in _context_text(artifacts.context)
    assert "do-not-copy" not in _context_text(artifacts.context)
    assert ".env" not in {path.name for path in artifacts.context.rglob("*")}
    assert "httpx>=0.28,<1" in (artifacts.context / "project-requirements.txt").read_text(
        encoding="utf-8"
    )
    assert "voicey[pipecat]" not in (artifacts.context / "project-requirements.txt").read_text(
        encoding="utf-8"
    )
    assert len(artifacts.digest) == 64

    livekit = tmp_path / "livekit"
    _project(livekit, "livekit")
    lk_artifacts = CloudArtifactGenerator(livekit).generate(
        "livekit-cloud",
        engine_wheel=_wheel(tmp_path),
        agent_name="voicey-agent",
        region="us-west",
    )
    assert lk_artifacts.bot is None
    assert lk_artifacts.platform_config is None
    assert '"voicey.deploy.cloud_runtime", "livekit"' in (
        lk_artifacts.dockerfile.read_text(encoding="utf-8")
    )


def test_cloud_artifact_rejects_symlink_and_bad_plan(tmp_path: Path) -> None:
    project = tmp_path / "project"
    _project(project, "pipecat")
    (project / "outside.txt").write_text("outside", encoding="utf-8")
    (project / "linked.py").symlink_to(project / "outside.txt")
    with pytest.raises(VoiceyError, match="rejects symlink"):
        CloudArtifactGenerator(project).generate(
            "pipecat-cloud",
            engine_wheel=_wheel(tmp_path),
            agent_name="voicey-agent",
            secret_set="voicey-agent-secrets",
            image="registry.example.test/voicey/agent:sha-123",
            region="us-west",
            min_agents=1,
            max_agents=4,
            profile="agent-1x",
        )
    with pytest.raises(VoiceyError, match="VY-DEP-008"):
        PipecatCloudPlan(
            agent_name="Bad_Name",
            organization="voicey-test",
            region="us-west",
            secret_set="voicey-agent-secrets",
            image="latest",
            relay_url="http://public.example.test",
            min_agents=5,
            max_agents=1,
            profile="agent-1x",
        )


def test_platform_runner_maps_missing_failure_and_timeout_without_output_leak(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    def missing_executable(_name: str) -> None:
        return None

    monkeypatch.setattr("voicey.deploy.cloud.shutil.which", missing_executable)
    with pytest.raises(VoiceyError, match="VY-DEP-009"):
        PlatformCliRunner("pipecat")

    runner = PlatformCliRunner.__new__(PlatformCliRunner)
    runner.executable = "/usr/local/bin/pipecat"
    runner.executable_name = "pipecat"
    failed = subprocess.CompletedProcess(
        args=["pipecat"],
        returncode=1,
        stdout="VOICEY_RELAY_CREDENTIAL=vkr_secret",
        stderr="ANTHROPIC_API_KEY=secret",
    )

    def failed_run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return failed

    monkeypatch.setattr("voicey.deploy.cloud.subprocess.run", failed_run)
    with pytest.raises(VoiceyError, match="exit 1") as caught:
        runner.run(["cloud", "auth", "whoami"], cwd=tmp_path)
    assert "vkr_secret" not in str(caught.value)
    assert "ANTHROPIC_API_KEY" not in str(caught.value)

    def timeout(*_args: object, **_kwargs: object) -> object:
        raise subprocess.TimeoutExpired(["pipecat"], 1)

    monkeypatch.setattr("voicey.deploy.cloud.subprocess.run", timeout)
    with pytest.raises(VoiceyError, match="TimeoutExpired"):
        runner.run(["cloud"], cwd=tmp_path)

    def unchecked_run(
        *_args: object,
        **_kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=["pipecat"], returncode=7, stdout="diagnostic", stderr=""
        )

    monkeypatch.setattr("voicey.deploy.cloud.subprocess.run", unchecked_run)
    result = runner.run(["cloud"], cwd=tmp_path, check=False)
    assert result.returncode == 7
    assert result.stdout == "diagnostic"


@pytest.mark.asyncio
async def test_pipecat_cloud_deploy_syncs_without_argv_secrets_smokes_and_resumes(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    _project(project, "pipecat")
    runner = FakePipecatCloudRunner()
    FakeRelayClient.opens = 0
    manager = PipecatCloudDeploymentManager(
        project,
        runner=runner,
        relay_client_factory=cast("object", FakeRelayClient),  # type: ignore[arg-type]
    )
    sys.modules.pop("agent", None)
    first = await manager.deploy(
        _pcc_plan(),
        environment={},
        engine_wheel=_wheel(tmp_path),
    )
    sys.modules.pop("agent", None)
    second = await manager.deploy(
        _pcc_plan(),
        environment={},
        engine_wheel=_wheel(tmp_path),
        skip_session_smoke=True,
    )

    assert first.state.agent_created
    assert first.state.secrets_synced
    assert first.state.platform_ready
    assert first.state.relay_ready
    assert first.smoke.session_smoke
    assert not second.smoke.session_smoke
    assert FakeRelayClient.opens == 3
    assert all(not path.exists() for path in runner.secret_file_paths)
    assert all("vkr_" not in " ".join(command) for command in runner.commands)
    assert "VOICEY_RELAY_CREDENTIAL" in runner.secret_names
    assert "CUSTOM_TOOL_TOKEN" in runner.secret_names
    assert "VOICEY_WEBHOOK_SECRET" not in runner.secret_names
    assert "VOICEY_RESULTS_SECRET" not in runner.secret_names
    assert sum(command[:2] == ("cloud", "deploy") for command in runner.commands) == 1
    ledger = manager.store.path.read_text(encoding="utf-8")
    dotenv = (project / ".env").read_text(encoding="utf-8")
    relay_value = next(
        line.split("=", maxsplit=1)[1]
        for line in dotenv.splitlines()
        if line.startswith("VOICEY_RELAY_CREDENTIAL=")
    )
    assert relay_value not in ledger

    sys.modules.pop("agent", None)
    replacement = await manager.deploy(
        replace(_pcc_plan(), image="registry.example.test/voicey/agent:sha-124"),
        environment={},
        engine_wheel=_wheel(tmp_path),
        skip_session_smoke=True,
    )
    assert replacement.state.platform_ready
    assert runner.image == "registry.example.test/voicey/agent:sha-124"
    assert sum(command[:2] == ("cloud", "deploy") for command in runner.commands) == 2


@pytest.mark.asyncio
async def test_pipecat_cloud_requires_adoption_and_never_deletes_adopted_agent(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    _project(project, "pipecat")
    runner = FakePipecatCloudRunner(exists=True)
    manager = PipecatCloudDeploymentManager(
        project,
        runner=runner,
        relay_client_factory=cast("object", FakeRelayClient),  # type: ignore[arg-type]
    )
    sys.modules.pop("agent", None)
    with pytest.raises(VoiceyError, match="use --adopt"):
        await manager.deploy(
            _pcc_plan(),
            environment={},
            engine_wheel=_wheel(tmp_path),
            skip_session_smoke=True,
        )
    sys.modules.pop("agent", None)
    report = await manager.deploy(
        _pcc_plan(),
        environment={},
        engine_wheel=_wheel(tmp_path),
        adopt=True,
        skip_session_smoke=True,
    )
    assert report.state.agent_adopted
    with pytest.raises(VoiceyError, match="cannot be deleted"):
        manager.rollback_created(_pcc_plan())


@pytest.mark.asyncio
async def test_pipecat_created_agent_rollback_is_owner_scoped(tmp_path: Path) -> None:
    project = tmp_path / "project"
    _project(project, "pipecat")
    runner = FakePipecatCloudRunner()
    manager = PipecatCloudDeploymentManager(
        project,
        runner=runner,
        relay_client_factory=cast("object", FakeRelayClient),  # type: ignore[arg-type]
    )
    sys.modules.pop("agent", None)
    await manager.deploy(
        _pcc_plan(),
        environment={},
        engine_wheel=_wheel(tmp_path),
        skip_session_smoke=True,
    )
    state = manager.rollback_created(_pcc_plan())
    assert state.rolled_back
    assert not state.agent_created
    assert not runner.exists


@pytest.mark.asyncio
async def test_livekit_cloud_create_resume_and_previous_version_rollback(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    _project(project, "livekit")
    runner = FakeLiveKitCloudRunner()
    session_smoke = FakeLiveKitSessionSmoke()
    manager = LiveKitCloudDeploymentManager(
        project,
        runner=runner,
        relay_client_factory=cast("object", FakeRelayClient),  # type: ignore[arg-type]
        session_smoke_runner=cast("object", session_smoke),  # type: ignore[arg-type]
    )
    sys.modules.pop("agent", None)
    first = await manager.deploy(
        _lk_plan(),
        environment={
            "LIVEKIT_URL": "wss://voicey-test.livekit.cloud",
            "LIVEKIT_API_KEY": "livekit-key",
            "LIVEKIT_API_SECRET": "livekit-secret",
        },
        engine_wheel=_wheel(tmp_path),
        smoke_to="+14155550199",
    )
    sys.modules.pop("agent", None)
    second = await manager.deploy(
        _lk_plan(),
        environment={},
        engine_wheel=_wheel(tmp_path),
        skip_session_smoke=True,
    )
    assert first.state.agent_created
    assert first.state.agent_id == runner.agent_id
    assert second.state.previous_version == "v1"
    assert second.state.platform_ready
    assert first.smoke.session_smoke
    assert session_smoke.to_numbers == ["+14155550199"]
    assert session_smoke.environments[0]["LIVEKIT_URL"] == ("wss://voicey-test.livekit.cloud")
    assert session_smoke.environments[0]["LIVEKIT_API_KEY"] == "livekit-key"
    secret_payload = runner.secret_payloads[0]
    assert "LIVEKIT_API_KEY" not in secret_payload
    assert "LIVEKIT_API_SECRET" not in secret_payload
    assert all(not path.exists() for path in runner.secret_file_paths)
    state = manager.rollback(_lk_plan())
    assert state.rolled_back
    assert runner.rolled_back
    assert not runner.deleted


@pytest.mark.asyncio
async def test_livekit_adoption_is_explicit_and_created_first_version_deletes(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    _project(project, "livekit")
    runner = FakeLiveKitCloudRunner()
    adopted = LiveKitCloudDeploymentManager(
        project,
        runner=runner,
        relay_client_factory=cast("object", FakeRelayClient),  # type: ignore[arg-type]
    )
    sys.modules.pop("agent", None)
    with pytest.raises(VoiceyError, match="explicit --adopt"):
        await adopted.deploy(
            _lk_plan(agent_id=runner.agent_id),
            environment={},
            engine_wheel=_wheel(tmp_path),
            skip_session_smoke=True,
        )

    fresh_project = tmp_path / "fresh"
    _project(fresh_project, "livekit")
    fresh_runner = FakeLiveKitCloudRunner()
    fresh = LiveKitCloudDeploymentManager(
        fresh_project,
        runner=fresh_runner,
        relay_client_factory=cast("object", FakeRelayClient),  # type: ignore[arg-type]
    )
    sys.modules.pop("agent", None)
    await fresh.deploy(
        _lk_plan(),
        environment={},
        engine_wheel=_wheel(tmp_path),
        skip_session_smoke=True,
    )
    state = fresh.rollback(_lk_plan())
    assert state.rolled_back
    assert fresh_runner.deleted


def test_cloud_resource_store_rejects_permissions_symlink_and_drift(
    tmp_path: Path,
) -> None:
    credential = RelayCredential.issue("ledger-key")
    state = CloudResourceState.initial(
        platform="pipecat-cloud",
        agent_name="voicey-agent",
        account_scope="voicey-test",
        region="us-west",
        relay_url="https://relay.example.test",
        relay=credential,
        relay_fingerprint="a" * 64,
        artifact_digest="b" * 64,
    )
    store = CloudResourceStore(tmp_path, "pipecat-cloud")
    store.save(state)
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600
    store.path.chmod(0o644)
    with pytest.raises(VoiceyError, match="VY-SEC-001"):
        store.load()
    store.path.unlink()
    target = tmp_path / "target.json"
    target.write_text("{}\n", encoding="utf-8")
    store.path.symlink_to(target)
    with pytest.raises(VoiceyError, match="VY-SEC-002"):
        store.load()
    with pytest.raises(VoiceyError, match="not an object"):
        CloudResourceState.from_payload([])
    with pytest.raises(VoiceyError, match="fields are invalid"):
        CloudResourceState.from_payload({"schema_version": 1})
    with pytest.raises(VoiceyError, match="drifted"):
        state.validate(
            platform="pipecat-cloud",
            agent_name="other-agent",
            account_scope="voicey-test",
            region="us-west",
            relay_url="https://relay.example.test",
            relay_fingerprint="a" * 64,
        )


def test_cloud_validation_and_parsing_fail_closed(tmp_path: Path) -> None:
    with pytest.raises(VoiceyError, match="LiveKit Cloud project"):
        LiveKitCloudPlan(
            agent_name="voicey-agent",
            project="Bad_Project",
            region="us-west",
            relay_url="https://relay.example.test",
            agent_id="bad",
        )
    with pytest.raises(VoiceyError, match="relay URL"):
        LiveKitCloudPlan(
            agent_name="voicey-agent",
            project="voicey-test",
            region="us-west",
            relay_url="https://user:pass@relay.example.test?token=secret",
        )

    store = CloudResourceStore(tmp_path, "livekit-cloud")
    store.path.parent.mkdir(parents=True)
    store.path.write_text("{broken", encoding="utf-8")
    store.path.chmod(0o600)
    with pytest.raises(VoiceyError, match="cannot be read"):
        store.load()

    credential = RelayCredential.issue("rolled-key")
    rolled = CloudResourceState.initial(
        platform="livekit-cloud",
        agent_name="voicey-agent",
        account_scope="voicey-test",
        region="us-west",
        relay_url="https://relay.example.test",
        relay=credential,
        relay_fingerprint="a" * 64,
        artifact_digest="b" * 64,
    ).checkpoint(rolled_back=True)
    with pytest.raises(VoiceyError, match="already rolled back"):
        rolled.validate(
            platform="livekit-cloud",
            agent_name="voicey-agent",
            account_scope="voicey-test",
            region="us-west",
            relay_url="https://relay.example.test",
            relay_fingerprint="a" * 64,
        )


def test_cloud_artifact_error_and_replacement_paths(tmp_path: Path) -> None:
    pipecat = tmp_path / "pipecat"
    _project(pipecat, "pipecat")
    generator = CloudArtifactGenerator(pipecat)
    wheel = _wheel(tmp_path)

    with pytest.raises(VoiceyError, match="inputs are incomplete"):
        generator.generate(
            "pipecat-cloud",
            engine_wheel=wheel,
            agent_name="voicey-agent",
            region="us-west",
        )

    first = generator.generate(
        "pipecat-cloud",
        engine_wheel=wheel,
        agent_name="voicey-agent",
        secret_set="voicey-agent-secrets",
        image="registry.example.test/voicey/agent:sha-123",
        region="us-west",
        min_agents=1,
        max_agents=4,
        profile="agent-1x",
    )
    marker = first.context / "old-marker"
    marker.write_text("old", encoding="utf-8")
    second = generator.generate(
        "pipecat-cloud",
        engine_wheel=wheel,
        agent_name="voicey-agent",
        secret_set="voicey-agent-secrets",
        image="registry.example.test/voicey/agent:sha-124",
        region="us-west",
        min_agents=0,
        max_agents=2,
        profile="agent-2x",
    )
    assert not (second.context / "old-marker").exists()

    livekit = tmp_path / "livekit"
    _project(livekit, "livekit")
    with pytest.raises(VoiceyError, match="requires a pipecat project"):
        CloudArtifactGenerator(livekit).generate(
            "pipecat-cloud",
            engine_wheel=wheel,
            agent_name="voicey-agent",
            secret_set="voicey-agent-secrets",
            image="registry.example.test/voicey/agent:sha-123",
            region="us-west",
            min_agents=1,
            max_agents=4,
            profile="agent-1x",
        )


def test_cloud_helper_contracts_cover_all_supported_platform_shapes(tmp_path: Path) -> None:
    assert _session_id("Session ID: session_abc-123") == "session_abc-123"
    assert _session_id("started without an identifier") is None
    assert _current_version("> v7 deployed current") == "v7"
    assert _current_version("current version: release_8") == "release_8"
    assert _current_version("no versions") is None
    assert _pipecat_agent_exists(_result("\x1b[32mStatus for agent demo\x1b[0m"))
    assert _pipecat_agent_exists(_result("Agent: demo\nReady: True\nDeployment Phase: Active"))
    assert not _pipecat_agent_exists(_result("No deployment data found for agent demo"))

    _require_ready("Status: RUNNING", platform="test")
    _require_ready("Agent: demo\nReady: True\nDeployment Phase: Active", platform="test")
    with pytest.raises(VoiceyError, match="did not report ready"):
        _require_ready("Agent: demo\nReady: False\nDeployment Phase: Validating", platform="test")
    with pytest.raises(VoiceyError, match="did not report ready"):
        _require_ready("Status: stopped", platform="test")
    assert (
        _pipecat_image("Image: registry.example.test/voicey/agent:sha-123")
        == "registry.example.test/voicey/agent:sha-123"
    )
    assert _pipecat_image("Ready: True") is None
    _require_region("us-west Oregon", "us-west", platform="test")
    with pytest.raises(VoiceyError, match="does not expose region"):
        _require_region("us-east", "us-west", platform="test")
    _require_secret_names("A\nB\n", {"A", "B"})
    with pytest.raises(VoiceyError, match="lacks C"):
        _require_secret_names("A\nB\n", {"A", "C"})

    _require_livekit_project('["voicey-test"]', "voicey-test")
    _require_livekit_project('{"projects":[{"name":"voicey-test"}]}', "voicey-test")
    _require_livekit_project(
        '[{"Name":"voicey-test","ProjectId":"project_123"}]',
        "voicey-test",
    )
    _require_livekit_project('{"Projects":[{"Name":"voicey-test"}]}', "voicey-test")
    with pytest.raises(VoiceyError, match="did not return JSON"):
        _require_livekit_project("not json", "voicey-test")
    with pytest.raises(VoiceyError, match="does not contain project"):
        _require_livekit_project('{"unexpected":[]}', "voicey-test")
    with pytest.raises(VoiceyError, match="does not contain project"):
        _require_livekit_project("null", "voicey-test")

    config = tmp_path / "livekit.toml"
    config.write_text('[agent]\nid = "agent_nested123"\n', encoding="utf-8")
    assert _livekit_agent_id(config, "") == "agent_nested123"
    config.write_text("{broken", encoding="utf-8")
    with pytest.raises(VoiceyError, match=r"livekit\.toml is invalid"):
        _livekit_agent_id(config, "")
    config.unlink()
    assert _livekit_agent_id(config, "Created agent_output123456") == "agent_output123456"
    with pytest.raises(VoiceyError, match="did not persist or report"):
        _livekit_agent_id(config, "created")

    manifest = SimpleNamespace(runtime="livekit", carriers=["sip", "twilio"])
    assert _runtime_extras(cast("ProjectManifest", manifest)) == "livekit,twilio"


def test_cloud_wheel_requirements_and_secret_file_validation(tmp_path: Path) -> None:
    destination = tmp_path / "stage"
    destination.mkdir()
    with pytest.raises(VoiceyError, match="unpublished builds require"):
        _stage_wheel(None, destination)
    wrong = tmp_path / "package.whl"
    wrong.write_bytes(b"wrong")
    with pytest.raises(VoiceyError, match="engine wheel is invalid"):
        _stage_wheel(wrong, destination)

    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text("[project]\ndependencies = 'bad'\n", encoding="utf-8")
    with pytest.raises(VoiceyError, match="dependencies are invalid"):
        _project_requirements(pyproject)
    pyproject.write_text("[project\ndependencies=[]\n", encoding="utf-8")
    with pytest.raises(VoiceyError, match=r"pyproject\.toml is invalid"):
        _project_requirements(pyproject)
    pyproject.write_text("[project]\ndependencies = [1]\n", encoding="utf-8")
    with pytest.raises(VoiceyError, match="dependencies are invalid"):
        _project_requirements(pyproject)

    holder = _secret_file({"GOOD": "value", "BAD": "two\nlines"})
    with pytest.raises(VoiceyError, match="contains a line break"), holder:
        pass
    assert holder.path is not None
    assert not holder.path.exists()


@pytest.mark.asyncio
async def test_wait_relay_call_retries_absence_and_propagates_other_errors() -> None:
    class Client:
        def __init__(self) -> None:
            self.calls = 0

        async def get_call(self, _call_id: str) -> object:
            self.calls += 1
            if self.calls == 1:
                raise VoiceyError("VY-OBS-003")
            return SimpleNamespace(ended_at=datetime.now(UTC))

    client = Client()
    await _wait_relay_call(
        cast("object", client),  # type: ignore[arg-type]
        "call_test",
        timeout_s=1,
        poll_interval_s=0,
        terminal=True,
        failure="timeout",
    )
    assert client.calls == 2

    class Broken:
        async def get_call(self, _call_id: str) -> object:
            raise VoiceyError("VY-DEP-004", detail="relay rejected")

    with pytest.raises(VoiceyError, match="relay rejected"):
        await _wait_relay_call(
            cast("object", Broken()),  # type: ignore[arg-type]
            "call_test",
            timeout_s=1,
            poll_interval_s=0,
            terminal=False,
            failure="timeout",
        )


def _context_text(path: Path) -> str:
    return "\n".join(
        item.read_text(encoding="utf-8", errors="ignore")
        for item in path.rglob("*")
        if item.is_file()
    )
