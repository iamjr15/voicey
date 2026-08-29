from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar, Literal

import pytest
from click import unstyle
from typer.testing import CliRunner

from voicey import __version__
from voicey.cli.app import app
from voicey.cli.doctor import DoctorCheck, DoctorReport
from voicey.cli.keys import KeyCheck
from voicey.cli.wizard import InitResult
from voicey.config.catalog import ProviderKind
from voicey.config.manifest import ManifestStore, ProjectManifest, RecipeSelection
from voicey.config.models import ModelAxis
from voicey.deploy.cloud import (
    CloudArtifacts,
    CloudDeploymentReport,
    CloudResourceState,
    CloudSmokeReport,
    LiveKitCloudPlan,
    PipecatCloudPlan,
)
from voicey.deploy.docker import DockerSmokeResult
from voicey.deploy.fly import FlyArtifacts, FlyPlan, FlyResourceState, FlySmokeReport
from voicey.deploy.railway import (
    RailwayArtifacts,
    RailwayPlan,
    RailwayResourceState,
    RailwaySmokeReport,
)
from voicey.errors import VoiceyError
from voicey.obs.records import NewCall
from voicey.recipes.drift import RecipeDriftReport, RecipeFileDrift
from voicey.relay.auth import RelayCredential
from voicey.storage.models import ResultDeliveryConfig, TerminalRequest
from voicey.storage.sqlite import SQLiteRepository
from voicey.telephony.models import NumberInfo, PipecatTarget, RollbackToken
from voicey.upgrade import UpgradeReport

# Rich changes ANSI styling and line wrapping when it detects hosted CI. Keep
# contract assertions independent of the parent process and terminal width.
runner = CliRunner(env={"CI": None, "GITHUB_ACTIONS": None, "NO_COLOR": "1", "COLUMNS": "240"})


def test_bare_command_prints_status_and_next_step() -> None:
    result = runner.invoke(app)

    assert result.exit_code == 0
    assert "voicey is installed" in result.stdout
    assert "Next:" in result.stdout


def test_version_flag() -> None:
    result = runner.invoke(app, ["--version"])

    assert result.exit_code == 0
    assert result.stdout.strip() == __version__


def _project(path: Path) -> ProjectManifest:
    models: dict[ModelAxis, str] = {
        "stt": "deepgram/nova-3",
        "llm": "anthropic/claude-sonnet-5",
        "tts": "cartesia/sonic-3.5",
    }
    manifest = ProjectManifest(
        project_name="test-agent",
        runtime="pipecat",
        recipe=RecipeSelection(name="scratch", version="1.0.0"),
        channels=frozenset({"web"}),
        models=models,
    )
    ManifestStore(path / "voicey.jsonc").save(manifest)
    (path / ".env").write_text(
        'DEEPGRAM_API_KEY="dg"\n'  # pragma: allowlist secret
        'ANTHROPIC_API_KEY="ant"\n'  # pragma: allowlist secret
        'CARTESIA_API_KEY="car"\n'  # pragma: allowlist secret
        'VOICEY_WEBHOOK_SECRET="whsec_dGVzdA=="\n',  # pragma: allowlist secret
        encoding="utf-8",
    )
    (path / ".env").chmod(0o600)
    return manifest


class AlwaysValidKeys:
    async def validate(
        self,
        kind: ProviderKind,
        identifier: str,
        values: Mapping[str, str],
    ) -> KeyCheck:
        del kind, values
        provider = identifier.split("/", maxsplit=1)[0]
        return KeyCheck(
            provider=provider,
            env_names=(f"{provider.upper()}_API_KEY",),
            status="valid",
            detail="Authenticated provider read succeeded.",
            fix="No action required.",
        )


class AlwaysValidLiveKit:
    async def validate(self, values: Mapping[str, str]) -> KeyCheck:
        del values
        return KeyCheck(
            provider="livekit",
            env_names=("LIVEKIT_URL", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET"),
            status="valid",
            detail="Authenticated LiveKit room-list read succeeded.",
            fix="No action required.",
        )


class FakeLedger:
    def close(self) -> None:
        return


class FakeTwilio:
    ledger = FakeLedger()
    events: ClassVar[list[str]] = []
    call_options: ClassVar[list[dict[str, object]]] = []

    def list_numbers(self) -> list[NumberInfo]:
        return [
            NumberInfo(
                number="+14155550123",
                provider_id="PN123",
                country="US",
                capabilities=frozenset({"voice"}),
            )
        ]

    def buy_number(self, country: str, area: str | None = None) -> NumberInfo:
        self.events.append(f"buy:{country}:{area}")
        return self.list_numbers()[0]

    def release_number(self, number: str) -> None:
        self.events.append(f"release:{number}")

    def point_inbound(self, number: str, target: PipecatTarget) -> RollbackToken:
        self.events.append(f"point:{number}:{target.https_base}")
        return RollbackToken(provider="twilio", token="route_cli")

    def restore(self, token: RollbackToken) -> None:
        self.events.append(f"restore:{token.token}")

    def start_call(
        self,
        from_number: str,
        to_number: str,
        target: PipecatTarget,
        **options: object,
    ) -> str:
        self.call_options.append(options)
        self.events.append(f"call:{from_number}:{to_number}:{target.https_base}")
        return "CA" + "1" * 32


class FakeTelnyx(FakeTwilio):
    def point_inbound(self, number: str, target: PipecatTarget) -> RollbackToken:
        assert target.ws_path == "/telnyx/media"
        assert target.event_path == "/telnyx/events"
        self.events.append(f"point:{number}:{target.stream_url}")
        return RollbackToken(provider="telnyx", token="route_cli")

    def start_call(
        self,
        from_number: str,
        to_number: str,
        target: PipecatTarget,
        **options: object,
    ) -> str:
        assert target.ws_path == "/telnyx/media"
        assert target.event_path == "/telnyx/events"
        self.call_options.append(options)
        self.events.append(f"call:{from_number}:{to_number}:{target.stream_url}")
        return "v3:telnyx-call-id"


class FakeDoctor:
    def __init__(self, *_args: object, **_kwargs: object) -> None:
        return

    def apply_safe_fixes(self) -> tuple[str, ...]:
        return ("safe fix",)

    async def run(self, *, on_check: object = None) -> DoctorReport:
        check = DoctorCheck(
            id="python",
            description="Python",
            ok=True,
            advice=("No action required.",),
        )
        if callable(on_check):
            on_check(check)
        return DoctorReport(checks=(check,))


def _fake_twilio(*_args: object, **_kwargs: object) -> FakeTwilio:
    return FakeTwilio()


def _fake_telnyx(*_args: object, **_kwargs: object) -> FakeTelnyx:
    return FakeTelnyx()


def _phone_project(
    path: Path,
    *,
    carrier: Literal["twilio", "telnyx"] = "twilio",
    record: bool = False,
) -> ProjectManifest:
    models: dict[ModelAxis, str] = {
        "stt": "deepgram/nova-3",
        "llm": "anthropic/claude-sonnet-5",
        "tts": "cartesia/sonic-3.5",
    }
    manifest = ProjectManifest(
        project_name="phone-agent",
        runtime="pipecat",
        recipe=RecipeSelection(name="scratch", version="1.0.0"),
        channels=frozenset({"phone", "web"}),
        models=models,
        carriers=[carrier],
        phone_number="+14155550123",
    )
    ManifestStore(path / "voicey.jsonc").save(manifest)
    (path / "agent.py").write_text(
        "\n".join(
            [
                "from voicey import Agent, Models, Phone, Results, Web",
                "",
                "agent = Agent(",
                "    name='phone-agent',",
                "    runtime='pipecat',",
                "    models=Models(",
                "        stt='deepgram/nova-3',",
                "        llm='anthropic/claude-sonnet-5',",
                "        tts='cartesia/sonic-3.5',",
                "    ),",
                "    persona='Test phone calls.',",
                "    flow='flow:entry',",
                "    tools='tools',",
                (
                    "    phone=Phone(provider="
                    f"'{carrier}', number='+14155550123', record={record!r}),"
                ),
                "    web=Web(enabled=True, allowed_origins=['https://app.example.test']),",
                "    results=Results(",
                "        webhook='https://receiver.example.test/results',",
                "        secret_env='VOICEY_WEBHOOK_SECRET',",  # pragma: allowlist secret
                "    ),",
                ")",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return manifest


async def _seed_call(path: Path) -> tuple[str, str]:
    call_id = "call_cli_test"
    async with SQLiteRepository(path / ".voicey" / "calls.sqlite3") as repository:
        lease = await repository.begin_call(
            NewCall(
                call_id=call_id,
                agent_name="test-agent",
                runtime="pipecat",
                channel="web",
                direction="inbound",
                config_hash="sha256:" + "1" * 64,
                started_at=datetime(2026, 7, 27, tzinfo=UTC),
            ),
            owner_id="worker",
            delivery=ResultDeliveryConfig(endpoint="https://receiver.example.test/results"),
            lease_ttl=timedelta(seconds=30),
        )
        event = await repository.terminalize(
            lease,
            TerminalRequest(
                event_type="call.completed",
                ended_reason="caller_hangup",
                ended_at=datetime(2026, 7, 27, 0, 1, tzinfo=UTC),
            ),
        )
    return call_id, event.event_id


def test_bare_json_status_is_parseable(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["--json"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["project"] is None
    assert payload["next_step"] == "voicey init"


def test_command_tree_and_flag_twins_are_exposed() -> None:
    root = runner.invoke(app, ["--help"])
    assert root.exit_code == 0
    root_output = unstyle(root.stdout)
    for command in (
        "init",
        "dev",
        "call",
        "doctor",
        "test",
        "deploy",
        "numbers",
        "keys",
        "calls",
        "recipes",
        "upgrade",
    ):
        assert command in root_output

    init_help = runner.invoke(app, ["init", "--help"])
    assert init_help.exit_code == 0
    init_output = unstyle(init_help.stdout)
    for flag in (
        "--recipe",
        "--description",
        "--channels",
        "--phone-provider",
        "--phone-number",
        "--runtime",
        "--models",
        "--draft-prompts",
        "--no-draft-prompts",
        "--resume",
        "--yes",
    ):
        assert flag in init_output

    for command, expected in (
        (["dev", "--help"], ("--phone", "--no-phone", "--tunnel", "--no-open")),
        (["call", "--help"], ("--yes", "--url")),
        (["doctor", "--help"], ("--fix", "--no-fix", "--send-test", "--json")),
        (["numbers", "buy", "--help"], ("--yes", "--area")),
        (["numbers", "point", "--help"], ("--yes", "--url")),
        (["calls", "list", "--help"], ("--undelivered", "--all", "--json")),
        (
            ["deploy", "docker", "--help"],
            ("--smoke", "--skip-smoke", "--to", "--engine-wheel", "--yes", "--json"),
        ),
        (
            ["deploy", "fly", "--help"],
            (
                "--app",
                "--org",
                "--region",
                "--postgres-name",
                "--bucket",
                "--adopt",
                "--rotate-credentials",
                "--rollback-created",
                "--skip-smoke",
                "--engine-wheel",
                "--yes",
                "--json",
            ),
        ),
        (
            ["deploy", "railway", "--help"],
            (
                "--project",
                "--workspace",
                "--environment",
                "--service",
                "--bucket",
                "--service-region",
                "--bucket-region",
                "--project-id",
                "--adopt",
                "--rotate-credentials",
                "--rollback-created",
                "--skip-smoke",
                "--engine-wheel",
                "--yes",
                "--json",
            ),
        ),
        (
            ["deploy", "pipecat-cloud", "--help"],
            (
                "--agent",
                "--org",
                "--region",
                "--secret-set",
                "--image",
                "--min-agents",
                "--max-agents",
                "--profile",
                "--relay-url",
                "--prepare-only",
                "--adopt",
                "--cutover",
                "--no-cutover",
                "--rollback-created",
                "--skip-smoke",
                "--engine-wheel",
                "--yes",
                "--json",
            ),
        ),
        (
            ["deploy", "livekit-cloud", "--help"],
            (
                "--agent",
                "--project",
                "--region",
                "--relay-url",
                "--agent-id",
                "--adopt",
                "--smoke-to",
                "--skip-smoke",
                "--rollback",
                "--engine-wheel",
                "--yes",
                "--json",
            ),
        ),
    ):
        help_result = runner.invoke(app, command)
        assert help_result.exit_code == 0
        help_output = unstyle(help_result.stdout)
        for flag in expected:
            assert flag in help_output


def test_noninteractive_init_never_chooses_missing_answer(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)
    project = tmp_path / "partial"

    result = runner.invoke(
        app,
        [
            "init",
            str(project),
            "--name",
            "partial",
            "--recipe",
            "scratch",
            "--description",
            "Help callers.",
            "--yes",
        ],
    )

    assert result.exit_code == 1
    assert "VY-CLI-001" in result.stderr
    assert (project / "voicey.jsonc").exists()
    assert "init-checkpoint" in (project / "voicey.jsonc").read_text(encoding="utf-8")


def test_read_commands_have_json_paths(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _project(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("voicey.cli.app.ProviderKeyValidator", AlwaysValidKeys)
    monkeypatch.setattr("voicey.cli.app._twilio", _fake_twilio)
    monkeypatch.setattr("voicey.cli.app.Doctor", FakeDoctor)

    commands = (
        ["--json"],
        ["recipes", "list", "--json"],
        ["recipes", "update-check", "--json"],
        ["keys", "list", "--json"],
        ["keys", "validate", "--json"],
        ["numbers", "list", "--json"],
        ["calls", "list", "--json"],
        ["doctor", "--json"],
    )
    for command in commands:
        result = runner.invoke(app, command)
        assert result.exit_code == 0, (command, result.stdout, result.stderr)
        assert json.loads(result.stdout)["next_step"]

    missing_call = runner.invoke(app, ["calls", "show", "missing", "--json"])
    assert missing_call.exit_code == 1
    assert json.loads(missing_call.stdout)["error"]["code"] == "VY-OBS-003"


def test_money_and_live_mutations_require_confirmation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _project(tmp_path)
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["numbers", "buy", "US"])

    assert result.exit_code == 1
    assert "VY-CLI-008" in result.stderr


def test_upgrade_requires_confirmation_and_emits_json(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    manifest = _project(tmp_path)
    monkeypatch.chdir(tmp_path)
    calls: list[tuple[ProjectManifest, bool]] = []

    class Manager:
        def __init__(self, root: Path) -> None:
            assert root == tmp_path

        def upgrade(
            self,
            selected: ProjectManifest,
            *,
            prerelease: bool,
        ) -> UpgradeReport:
            calls.append((selected, prerelease))
            return UpgradeReport(
                from_version="0.1.0",
                to_version="0.2.0rc1",
                channel="canary",
                changed=True,
                lockfile=str(tmp_path / "uv.lock"),
                pyproject_unchanged=True,
                recipe_sources_unchanged=True,
                recipe_drift={
                    "status": "current",
                    "conflicts": 0,
                    "next_step": "voicey doctor",
                },
                next_step="voicey doctor",
            )

    monkeypatch.setattr("voicey.cli.app.UpgradeManager", Manager)

    denied = runner.invoke(app, ["upgrade"])
    assert denied.exit_code == 1
    assert "VY-CLI-008" in denied.stderr
    assert calls == []

    result = runner.invoke(app, ["upgrade", "--pre", "--yes", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["to_version"] == "0.2.0rc1"
    assert payload["recipe_drift"]["status"] == "current"
    assert payload["next_step"] == "voicey doctor"
    assert calls == [(manifest, True)]


def test_recipe_update_check_prints_conflicts_and_merge_guidance(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _project(tmp_path)
    monkeypatch.chdir(tmp_path)

    class Analyzer:
        def __init__(self, root: Path) -> None:
            assert root == tmp_path

        def analyze(self, _manifest: ProjectManifest) -> RecipeDriftReport:
            return RecipeDriftReport(
                recipe="appointment-booking",
                runtime="pipecat",
                installed_version="1.0.0",
                upstream_version="1.1.0",
                status="update-available",
                baseline_source="tracked",
                files=(
                    RecipeFileDrift(
                        path="flow.py",
                        status="conflict",
                        base_sha256="base",
                        local_sha256="local",
                        upstream_sha256="upstream",
                    ),
                ),
                local_changes=1,
                upstream_changes=1,
                conflicts=1,
                ai_merge_prompt="Merge each hunk; never overwrite project code.",
                next_step="voicey test",
            )

    monkeypatch.setattr("voicey.cli.app.RecipeDriftAnalyzer", Analyzer)
    result = runner.invoke(app, ["recipes", "update-check"])

    assert result.exit_code == 0
    assert "flow.py" in result.stdout
    assert "conflict" in result.stdout
    assert "AI merge guidance" in result.stdout
    assert "Next: voicey test" in result.stdout


def test_upgrade_human_output_prints_next_step(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _project(tmp_path)
    monkeypatch.chdir(tmp_path)

    class Manager:
        def __init__(self, root: Path) -> None:
            assert root == tmp_path

        def upgrade(
            self,
            _manifest: ProjectManifest,
            *,
            prerelease: bool,
        ) -> UpgradeReport:
            assert prerelease is False
            return UpgradeReport(
                from_version="0.1.0",
                to_version="0.1.0",
                channel="stable",
                changed=False,
                lockfile=str(tmp_path / "uv.lock"),
                pyproject_unchanged=True,
                recipe_sources_unchanged=True,
                recipe_drift={"status": "current"},
                next_step="voicey doctor",
            )

    monkeypatch.setattr("voicey.cli.app.UpgradeManager", Manager)
    result = runner.invoke(app, ["upgrade", "--stable", "--yes"])

    assert result.exit_code == 0
    assert "already current" in result.stdout
    assert "Project source preserved" in result.stdout
    assert "Next: voicey doctor" in result.stdout


def test_docker_deploy_generates_validates_updates_manifest_and_prints_next_step(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _phone_project(tmp_path)
    calls: list[str] = []

    class Generator:
        def __init__(self, root: Path) -> None:
            assert root == tmp_path

        def generate(self, *, engine_wheel: Path | None = None) -> object:
            assert engine_wheel == tmp_path / "voicey.whl"
            calls.append("generate")
            return SimpleNamespace(
                dockerfile=tmp_path / "Dockerfile.voicey",
                compose=tmp_path / "compose.voicey.yaml",
                dockerignore=tmp_path / ".dockerignore",
                environment_example=tmp_path / "docker.env.example",
                engine_wheel=tmp_path / ".voicey" / "deploy" / "voicey.whl",
            )

        def validate(self, _artifacts: object) -> None:
            calls.append("validate")

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("voicey.cli.app.DockerDeploymentGenerator", Generator)

    result = runner.invoke(
        app,
        [
            "deploy",
            "docker",
            "--engine-wheel",
            str(tmp_path / "voicey.whl"),
        ],
    )

    assert result.exit_code == 0, result.stderr
    assert calls == ["generate", "validate"]
    assert "docker compose -f compose.voicey.yaml up -d --build" in result.stdout
    assert "--engine-wheel" in result.stdout
    assert "voicey.whl" in result.stdout
    assert ManifestStore(tmp_path / "voicey.jsonc").load().deploy_target == "docker"


def test_docker_deploy_json_smoke_places_explicit_confirmed_phone_call(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _phone_project(tmp_path)
    (tmp_path / ".env").write_text(
        'TWILIO_ACCOUNT_SID="AC111"\nTWILIO_AUTH_TOKEN="token"\n',
        encoding="utf-8",
    )

    class Generator:
        def __init__(self, _root: Path) -> None:
            return

        def generate(self, *, engine_wheel: Path | None = None) -> object:
            del engine_wheel
            return SimpleNamespace(
                dockerfile=tmp_path / "Dockerfile.voicey",
                compose=tmp_path / "compose.voicey.yaml",
                dockerignore=tmp_path / ".dockerignore",
                environment_example=tmp_path / "docker.env.example",
                engine_wheel=None,
            )

        def validate(self, _artifacts: object) -> None:
            return

    class Smoke:
        async def verify(self, url: str) -> DockerSmokeResult:
            return DockerSmokeResult(
                url=url,
                runtime="pipecat",
                active_calls=0,
                accepting=True,
                storage_ready=True,
            )

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("voicey.cli.app.DockerDeploymentGenerator", Generator)
    monkeypatch.setattr("voicey.cli.app.DockerSmokeVerifier", Smoke)
    monkeypatch.setattr("voicey.cli.app._twilio", _fake_twilio)
    FakeTwilio.events = []
    FakeTwilio.call_options = []

    result = runner.invoke(
        app,
        [
            "deploy",
            "docker",
            "--smoke",
            "https://voice.example",
            "--to",
            "+14155550199",
            "--yes",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["smoke"]["storage_ready"] is True
    assert payload["call_id"].startswith("CA")
    assert FakeTwilio.events == ["call:+14155550123:+14155550199:https://voice.example"]
    assert FakeTwilio.call_options == [{"amd": True, "record": False}]


def test_fly_deploy_maps_phone_callbacks_updates_manifest_and_prints_cloud_next_step(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _phone_project(tmp_path)
    captured_plans: list[FlyPlan] = []
    captured_options: list[dict[str, object]] = []

    class Manager:
        def __init__(self, root: Path) -> None:
            assert root == tmp_path
            self.store = SimpleNamespace(
                path=tmp_path / ".voicey" / "deploy" / "fly-resources.json"
            )

        async def deploy(self, plan: FlyPlan, **options: object) -> object:
            captured_plans.append(plan)
            captured_options.append(options)
            state = FlyResourceState.initial(plan).checkpoint(
                app_created=True,
                postgres_created=True,
                bucket_created=True,
                postgres_id="mpg_123",
                postgres_attached=True,
                bucket_attached=True,
                deployed=True,
                smoke_green=True,
            )
            directory = tmp_path / ".voicey" / "deploy" / "fly"
            return SimpleNamespace(
                state=state,
                artifacts=FlyArtifacts(
                    directory=directory,
                    dockerfile=directory / "Dockerfile.results",
                    config=directory / "fly.results.toml",
                    dockerignore=directory / "dockerignore",
                    engine_wheel=tmp_path / "voicey.whl",
                    digest="a" * 64,
                ),
                smoke=FlySmokeReport(
                    app_name="test-results",
                    public_base="https://test-results.fly.dev",
                    platform_checks=2,
                    liveness=True,
                    signed_readiness=True,
                ),
            )

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("voicey.cli.app.FlyDeploymentManager", Manager)
    result = runner.invoke(
        app,
        [
            "deploy",
            "fly",
            "--app",
            "test-results",
            "--org",
            "test-org",
            "--region",
            "iad",
            "--postgres-name",
            "test-results-pg",
            "--bucket",
            "test-results-objects",
            "--postgres-plan",
            "Basic",
            "--postgres-volume-gb",
            "10",
            "--engine-wheel",
            str(tmp_path / "voicey.whl"),
            "--yes",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.stderr
    payload = json.loads(result.stdout)
    plan = captured_plans[0]
    assert plan.callback_providers == ("twilio",)
    assert plan.postgres_name == "test-results-pg"
    assert plan.bucket_name == "test-results-objects"
    assert captured_options[0]["adopt"] is False
    assert payload["resources"]["postgres_id"] == "mpg_123"
    assert payload["smoke"]["signed_readiness"] is True
    assert payload["next_step"] == (
        "voicey deploy pipecat-cloud --relay-url https://test-results.fly.dev --yes"
    )
    assert "vkr_" not in result.stdout
    assert ManifestStore(tmp_path / "voicey.jsonc").load().deploy_target == "fly"


def test_fly_rollback_requires_confirmation_and_clears_manifest_target(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    manifest = _project(tmp_path)
    ManifestStore(tmp_path / "voicey.jsonc").save(
        manifest.model_copy(update={"deploy_target": "fly"})
    )
    rolled_back: list[str] = []

    class Manager:
        def __init__(self, _root: Path) -> None:
            self.store = SimpleNamespace(
                path=tmp_path / ".voicey" / "deploy" / "fly-resources.json"
            )

        def rollback_created(self, plan: FlyPlan) -> FlyResourceState:
            rolled_back.append(plan.app_name)
            return FlyResourceState.initial(plan).checkpoint(rolled_back=True)

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("voicey.cli.app.FlyDeploymentManager", Manager)
    command = [
        "deploy",
        "fly",
        "--app",
        "test-results",
        "--org",
        "test-org",
        "--region",
        "iad",
        "--postgres-name",
        "test-results-pg",
        "--bucket",
        "test-results-objects",
        "--postgres-plan",
        "Basic",
        "--postgres-volume-gb",
        "10",
        "--rollback-created",
    ]
    denied = runner.invoke(app, command)
    assert denied.exit_code == 1
    assert "VY-CLI-008" in denied.stderr
    assert rolled_back == []

    accepted = runner.invoke(app, [*command, "--yes", "--json"])
    assert accepted.exit_code == 0, accepted.stderr
    assert json.loads(accepted.stdout)["rolled_back"] is True
    assert rolled_back == ["test-results"]
    assert ManifestStore(tmp_path / "voicey.jsonc").load().deploy_target is None


def _railway_args() -> list[str]:
    return [
        "deploy",
        "railway",
        "--project",
        "test-results",
        "--workspace",
        "test-workspace",
        "--environment",
        "production",
        "--service",
        "test-results",
        "--bucket",
        "test-results-objects",
        "--service-region",
        "us-east",
        "--bucket-region",
        "iad",
    ]


def test_railway_deploy_updates_manifest_and_prints_cloud_next_step(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _phone_project(tmp_path)
    captured: list[tuple[RailwayPlan, dict[str, object]]] = []

    class Manager:
        def __init__(self, root: Path) -> None:
            assert root == tmp_path
            self.store = SimpleNamespace(
                path=tmp_path / ".voicey" / "deploy" / "railway-resources.json"
            )

        async def deploy(self, plan: RailwayPlan, **options: object) -> object:
            captured.append((plan, options))
            directory = tmp_path / ".voicey" / "deploy" / "railway"
            state = RailwayResourceState.initial(plan).checkpoint(
                project_id="project_123",
                environment_id="environment_123",
                service_id="service_123",
                postgres_id="postgres_123",
                postgres_name="Postgres",
                bucket_id="bucket_123",
                domain_id="domain_123",
                public_base="https://test-results.up.railway.app",
                deployment_id="deployment_123",
                preflight_green=True,
                smoke_green=True,
            )
            return SimpleNamespace(
                state=state,
                artifacts=RailwayArtifacts(
                    directory=directory,
                    dockerfile=directory / "Dockerfile.results",
                    config=directory / "railway.json",
                    ignore=directory / ".railwayignore",
                    engine_wheel=tmp_path / "voicey.whl",
                    digest="a" * 64,
                ),
                smoke=RailwaySmokeReport(
                    project_id="project_123",
                    service_id="service_123",
                    deployment_id="deployment_123",
                    public_base="https://test-results.up.railway.app",
                    deployment_status="SUCCESS",
                    liveness=True,
                    signed_readiness=True,
                    migration_preflight=True,
                    rolling_generation_preflight=True,
                ),
            )

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("voicey.cli.app.RailwayDeploymentManager", Manager)
    result = runner.invoke(
        app,
        [
            *_railway_args(),
            "--engine-wheel",
            str(tmp_path / "voicey.whl"),
            "--yes",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.stderr
    payload = json.loads(result.stdout)
    plan, options = captured[0]
    assert plan.callback_providers == ("twilio",)
    assert options["rotate_credentials"] is False
    assert payload["smoke"]["migration_preflight"] is True
    assert payload["next_step"] == (
        "voicey deploy pipecat-cloud --relay-url https://test-results.up.railway.app --yes"
    )
    assert ManifestStore(tmp_path / "voicey.jsonc").load().deploy_target == "railway"


def test_railway_adoption_and_rollback_confirmation_contract(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    manifest = _project(tmp_path)
    ManifestStore(tmp_path / "voicey.jsonc").save(
        manifest.model_copy(update={"deploy_target": "railway"})
    )
    rolled_back: list[str] = []

    class Manager:
        def __init__(self, _root: Path) -> None:
            self.store = SimpleNamespace(
                path=tmp_path / ".voicey" / "deploy" / "railway-resources.json"
            )

        def rollback_created(self, plan: RailwayPlan) -> RailwayResourceState:
            rolled_back.append(plan.project_name)
            return RailwayResourceState.initial(plan).checkpoint(rolled_back=True)

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("voicey.cli.app.RailwayDeploymentManager", Manager)
    invalid_adopt = runner.invoke(app, [*_railway_args(), "--adopt", "--yes"])
    assert invalid_adopt.exit_code == 1
    assert "VY-CLI-010" in invalid_adopt.stderr

    command = [*_railway_args(), "--rollback-created"]
    denied = runner.invoke(app, command)
    assert denied.exit_code == 1
    assert "VY-CLI-008" in denied.stderr
    accepted = runner.invoke(app, [*command, "--yes", "--json"])
    assert accepted.exit_code == 0, accepted.stderr
    assert json.loads(accepted.stdout)["rolled_back"] is True
    assert rolled_back == ["test-results"]
    assert ManifestStore(tmp_path / "voicey.jsonc").load().deploy_target is None


def _cloud_state(
    platform: Literal["pipecat-cloud", "livekit-cloud"],
    *,
    agent_name: str = "test-agent",
) -> CloudResourceState:
    return CloudResourceState.initial(
        platform=platform,
        agent_name=agent_name,
        account_scope="test-org" if platform == "pipecat-cloud" else "test-project",
        region="us-west",
        relay_url="https://test-results.fly.dev",
        relay=RelayCredential.issue("cli-cloud-key"),
        relay_fingerprint="a" * 64,
        artifact_digest="b" * 64,
        worker_secrets_fingerprint="c" * 64,
    ).checkpoint(
        agent_created=True,
        agent_id="agent_123456" if platform == "livekit-cloud" else None,
        secrets_synced=True,
        deployed=True,
        platform_ready=True,
        relay_ready=True,
    )


def _cloud_artifacts(
    root: Path,
    platform: Literal["pipecat-cloud", "livekit-cloud"],
) -> CloudArtifacts:
    context = root / ".voicey" / "deploy" / platform / "context"
    return CloudArtifacts(
        platform=platform,
        directory=context.parent,
        context=context,
        dockerfile=context / "Dockerfile",
        platform_config=(context / "pcc-deploy.toml" if platform == "pipecat-cloud" else None),
        bot=context / "bot.py" if platform == "pipecat-cloud" else None,
        engine_wheel=None,
        digest="b" * 64,
    )


def _pipecat_cloud_args(*, agent_name: str = "test-agent") -> list[str]:
    return [
        "deploy",
        "pipecat-cloud",
        "--agent",
        agent_name,
        "--org",
        "test-org",
        "--region",
        "us-west",
        "--secret-set",
        f"{agent_name}-secrets",
        "--image",
        "registry.example.test/voicey/agent:sha-123",
        "--min-agents",
        "1",
        "--max-agents",
        "4",
        "--profile",
        "agent-1x",
        "--relay-url",
        "https://test-results.fly.dev",
    ]


def _livekit_cloud_args() -> list[str]:
    return [
        "deploy",
        "livekit-cloud",
        "--agent",
        "test-agent",
        "--project",
        "test-project",
        "--region",
        "us-west",
        "--relay-url",
        "https://test-results.fly.dev",
    ]


def test_pipecat_cloud_prepare_only_prints_exact_build_and_push(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _project(tmp_path)

    class Manager:
        def __init__(self, root: Path) -> None:
            assert root == tmp_path

        def prepare(
            self,
            plan: PipecatCloudPlan,
            *,
            engine_wheel: Path | None,
        ) -> CloudArtifacts:
            assert plan.image == "registry.example.test/voicey/agent:sha-123"
            assert engine_wheel is None
            return _cloud_artifacts(tmp_path, "pipecat-cloud")

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("voicey.cli.app.PipecatCloudDeploymentManager", Manager)
    result = runner.invoke(
        app,
        [
            "deploy",
            "pipecat-cloud",
            "--agent",
            "test-agent",
            "--org",
            "test-org",
            "--region",
            "us-west",
            "--secret-set",
            "test-agent-secrets",
            "--image",
            "registry.example.test/voicey/agent:sha-123",
            "--min-agents",
            "1",
            "--max-agents",
            "4",
            "--profile",
            "agent-1x",
            "--relay-url",
            "https://test-results.fly.dev",
            "--prepare-only",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["prepared"] is True
    assert (
        "docker build --platform linux/arm64 -t registry.example.test/voicey/agent:sha-123"
    ) in payload["next_step"]
    assert "docker push registry.example.test/voicey/agent:sha-123" in payload["next_step"]

    text_result = runner.invoke(app, [*_pipecat_cloud_args(), "--prepare-only"])
    assert text_result.exit_code == 0, text_result.stderr
    assert "Secret-free Pipecat Cloud build context is ready." in text_result.stdout
    assert "Next: docker build" in text_result.stdout


def test_pipecat_cloud_deploy_cuts_over_and_verifies_paid_phone_smoke(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _phone_project(tmp_path)
    state = _cloud_state("pipecat-cloud", agent_name="phone-agent")
    saved: list[CloudResourceState] = []
    plans: list[PipecatCloudPlan] = []
    deploy_options: list[dict[str, object]] = []
    verified: list[str] = []

    class Store:
        path = tmp_path / ".voicey" / "deploy" / "pipecat-cloud-resources.json"

        def load(self) -> CloudResourceState:
            return saved[-1] if saved else state

        def save(self, value: CloudResourceState) -> None:
            saved.append(value)

    class Manager:
        def __init__(self, root: Path) -> None:
            assert root == tmp_path
            self.store = Store()

        async def deploy(
            self,
            plan: PipecatCloudPlan,
            **options: object,
        ) -> CloudDeploymentReport:
            plans.append(plan)
            deploy_options.append(options)
            return CloudDeploymentReport(
                state=state,
                artifacts=_cloud_artifacts(tmp_path, "pipecat-cloud"),
                smoke=CloudSmokeReport(
                    platform="pipecat-cloud",
                    agent_name=plan.agent_name,
                    platform_ready=True,
                    relay_ready=True,
                    session_smoke=True,
                ),
            )

    async def verify(
        _relay_url: str,
        _environment: Mapping[str, str],
        call_id: str,
    ) -> None:
        verified.append(call_id)

    carrier = FakeTwilio()

    def carrier_factory(*_args: object, **_kwargs: object) -> FakeTwilio:
        return carrier

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("voicey.cli.app.PipecatCloudDeploymentManager", Manager)
    monkeypatch.setattr("voicey.cli.app._carrier", carrier_factory)
    monkeypatch.setattr("voicey.cli.app._verify_cloud_phone_smoke", verify)
    FakeTwilio.events = []
    result = runner.invoke(
        app,
        [
            "deploy",
            "pipecat-cloud",
            "--agent",
            "phone-agent",
            "--org",
            "test-org",
            "--region",
            "us-west",
            "--secret-set",
            "phone-agent-secrets",
            "--image",
            "registry.example.test/voicey/agent:sha-123",
            "--min-agents",
            "1",
            "--max-agents",
            "4",
            "--profile",
            "agent-1x",
            "--relay-url",
            "https://test-results.fly.dev",
            "--smoke-to",
            "+14155550199",
            "--migrate-relay",
            "--yes",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.stderr
    payload = json.loads(result.stdout)
    assert plans[0].agent_name == "phone-agent"
    assert deploy_options[0]["migrate_relay"] is True
    assert payload["answer_url"].endswith("/us-west/test-org/phone-agent/twilio/answer")
    assert verified == ["CA" + "1" * 32]
    assert saved[-1].cutover_provider == "twilio"
    assert saved[-1].cutover_token == "route_cli"
    assert saved[-1].smoke_call_id == "CA" + "1" * 32
    assert ManifestStore(tmp_path / "voicey.jsonc").load().deploy_target == ("pipecat-cloud")


def test_livekit_cloud_deploy_passes_explicit_smoke_and_updates_manifest(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    manifest = _project(tmp_path)
    ManifestStore(tmp_path / "voicey.jsonc").save(
        manifest.model_copy(update={"runtime": "livekit"})
    )
    captured: list[tuple[LiveKitCloudPlan, dict[str, object]]] = []
    state = _cloud_state("livekit-cloud")

    class Manager:
        def __init__(self, root: Path) -> None:
            assert root == tmp_path
            self.store = SimpleNamespace(
                path=tmp_path / ".voicey" / "deploy" / "livekit-cloud-resources.json"
            )

        async def deploy(
            self,
            plan: LiveKitCloudPlan,
            **options: object,
        ) -> CloudDeploymentReport:
            captured.append((plan, options))
            return CloudDeploymentReport(
                state=state,
                artifacts=_cloud_artifacts(tmp_path, "livekit-cloud"),
                smoke=CloudSmokeReport(
                    platform="livekit-cloud",
                    agent_name=plan.agent_name,
                    platform_ready=True,
                    relay_ready=True,
                    session_smoke=False,
                ),
            )

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("voicey.cli.app.LiveKitCloudDeploymentManager", Manager)
    result = runner.invoke(
        app,
        [
            "deploy",
            "livekit-cloud",
            "--agent",
            "test-agent",
            "--project",
            "test-project",
            "--region",
            "us-west",
            "--relay-url",
            "https://test-results.fly.dev",
            "--migrate-relay",
            "--skip-smoke",
            "--yes",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["target"] == "livekit-cloud"
    assert captured[0][1]["skip_session_smoke"] is True
    assert captured[0][1]["migrate_relay"] is True
    assert captured[0][1]["smoke_to"] is None
    assert ManifestStore(tmp_path / "voicey.jsonc").load().deploy_target == ("livekit-cloud")

    text_result = runner.invoke(
        app,
        [*_livekit_cloud_args(), "--skip-smoke", "--yes"],
    )
    assert text_result.exit_code == 0, text_result.stderr
    assert "LiveKit Cloud deployment completed." in text_result.stdout
    assert "Next: voicey calls list" in text_result.stdout


def test_cloud_cli_validation_guards_are_cataloged(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    class Manager:
        def __init__(self, _root: Path) -> None:
            return

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("voicey.cli.app.PipecatCloudDeploymentManager", Manager)
    monkeypatch.setattr("voicey.cli.app.LiveKitCloudDeploymentManager", Manager)

    manifest = _project(tmp_path)
    wrong_livekit = runner.invoke(
        app,
        [*_livekit_cloud_args(), "--skip-smoke", "--yes"],
    )
    assert wrong_livekit.exit_code == 1
    assert "VY-DEP-008" in wrong_livekit.stderr

    prepare_conflict = runner.invoke(
        app,
        [*_pipecat_cloud_args(), "--prepare-only", "--skip-smoke"],
    )
    smoke_conflict = runner.invoke(
        app,
        [
            *_pipecat_cloud_args(),
            "--smoke-to",
            "+14155550199",
            "--skip-smoke",
            "--yes",
        ],
    )
    assert prepare_conflict.exit_code == 1
    assert "VY-CLI-010" in prepare_conflict.stderr
    assert smoke_conflict.exit_code == 1
    assert "VY-CLI-010" in smoke_conflict.stderr

    phone = _phone_project(tmp_path)
    missing_pipecat_smoke = runner.invoke(app, [*_pipecat_cloud_args(agent_name="phone-agent")])
    assert missing_pipecat_smoke.exit_code == 1
    assert "VY-DEP-004" in missing_pipecat_smoke.stderr

    telnyx = _phone_project(tmp_path, carrier="telnyx")
    missing_texml_ack = runner.invoke(
        app,
        [*_pipecat_cloud_args(agent_name="phone-agent"), "--skip-smoke", "--yes"],
    )
    assert missing_texml_ack.exit_code == 1
    assert "--telnyx-texml-ready" in missing_texml_ack.stderr

    ManifestStore(tmp_path / "voicey.jsonc").save(
        manifest.model_copy(update={"runtime": "livekit"})
    )
    wrong_pipecat = runner.invoke(
        app,
        [*_pipecat_cloud_args(), "--skip-smoke", "--yes"],
    )
    assert wrong_pipecat.exit_code == 1
    assert "VY-DEP-008" in wrong_pipecat.stderr

    ManifestStore(tmp_path / "voicey.jsonc").save(phone.model_copy(update={"runtime": "livekit"}))
    missing_livekit_smoke = runner.invoke(app, [*_livekit_cloud_args()])
    livekit_smoke_conflict = runner.invoke(
        app,
        [
            *_livekit_cloud_args(),
            "--smoke-to",
            "+14155550199",
            "--skip-smoke",
            "--yes",
        ],
    )
    assert missing_livekit_smoke.exit_code == 1
    assert "VY-DEP-004" in missing_livekit_smoke.stderr
    assert livekit_smoke_conflict.exit_code == 1
    assert "VY-CLI-010" in livekit_smoke_conflict.stderr

    del telnyx


def test_pipecat_cloud_rollback_restores_ledgered_cutover(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    manifest = _phone_project(tmp_path)
    ManifestStore(tmp_path / "voicey.jsonc").save(
        manifest.model_copy(update={"deploy_target": "pipecat-cloud"})
    )
    initial = _cloud_state("pipecat-cloud", agent_name="phone-agent").checkpoint(
        cutover_provider="twilio",
        cutover_token="route_previous",
    )
    saved: list[CloudResourceState] = []
    carrier = FakeTwilio()

    class Store:
        path = tmp_path / ".voicey" / "deploy" / "pipecat-cloud-resources.json"

        def load(self) -> CloudResourceState:
            return saved[-1] if saved else initial

        def save(self, state: CloudResourceState) -> None:
            saved.append(state)

    class Manager:
        def __init__(self, _root: Path) -> None:
            self.store = Store()

        def rollback_created(self, _plan: PipecatCloudPlan) -> CloudResourceState:
            return (saved[-1] if saved else initial).checkpoint(
                agent_created=False,
                rolled_back=True,
            )

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("voicey.cli.app.PipecatCloudDeploymentManager", Manager)

    def carrier_factory(*_args: object, **_kwargs: object) -> FakeTwilio:
        return carrier

    monkeypatch.setattr("voicey.cli.app._carrier", carrier_factory)
    FakeTwilio.events = []

    result = runner.invoke(
        app,
        [*_pipecat_cloud_args(agent_name="phone-agent"), "--rollback-created", "--yes", "--json"],
    )

    assert result.exit_code == 0, result.stderr
    assert json.loads(result.stdout)["rolled_back"] is True
    assert FakeTwilio.events == ["restore:route_previous"]
    assert saved[-1].cutover_token is None
    assert ManifestStore(tmp_path / "voicey.jsonc").load().deploy_target is None


def test_pipecat_cloud_smoke_failure_restores_new_cutover(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _phone_project(tmp_path)
    state = _cloud_state("pipecat-cloud", agent_name="phone-agent")
    saved: list[CloudResourceState] = []
    carrier = FakeTwilio()

    class Store:
        path = tmp_path / ".voicey" / "deploy" / "pipecat-cloud-resources.json"

        def save(self, value: CloudResourceState) -> None:
            saved.append(value)

    class Manager:
        def __init__(self, _root: Path) -> None:
            self.store = Store()

        async def deploy(
            self,
            plan: PipecatCloudPlan,
            **_options: object,
        ) -> CloudDeploymentReport:
            return CloudDeploymentReport(
                state=state,
                artifacts=_cloud_artifacts(tmp_path, "pipecat-cloud"),
                smoke=CloudSmokeReport(
                    platform="pipecat-cloud",
                    agent_name=plan.agent_name,
                    platform_ready=True,
                    relay_ready=True,
                    session_smoke=True,
                ),
            )

    async def fail_smoke(
        _relay_url: str,
        _environment: Mapping[str, str],
        _call_id: str,
    ) -> None:
        raise VoiceyError("VY-DEP-004", detail="expected smoke failure")

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("voicey.cli.app.PipecatCloudDeploymentManager", Manager)

    def carrier_factory(*_args: object, **_kwargs: object) -> FakeTwilio:
        return carrier

    monkeypatch.setattr("voicey.cli.app._carrier", carrier_factory)
    monkeypatch.setattr("voicey.cli.app._verify_cloud_phone_smoke", fail_smoke)
    FakeTwilio.events = []

    result = runner.invoke(
        app,
        [
            *_pipecat_cloud_args(agent_name="phone-agent"),
            "--smoke-to",
            "+14155550199",
            "--yes",
        ],
    )

    assert result.exit_code == 1
    assert "expected smoke failure" in result.stderr
    assert FakeTwilio.events[-1] == "restore:route_cli"
    assert saved[-1].cutover_token is None


def test_cloud_rollback_and_web_deploy_text_paths(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    manifest = _project(tmp_path)
    pipecat_state = _cloud_state("pipecat-cloud")
    livekit_state = _cloud_state("livekit-cloud")

    class PipecatStore:
        path = tmp_path / ".voicey" / "deploy" / "pipecat-cloud-resources.json"

        def load(self) -> CloudResourceState:
            return pipecat_state

        def save(self, _state: CloudResourceState) -> None:
            return

    class PipecatManager:
        def __init__(self, _root: Path) -> None:
            self.store = PipecatStore()

        async def deploy(
            self,
            plan: PipecatCloudPlan,
            **_options: object,
        ) -> CloudDeploymentReport:
            return CloudDeploymentReport(
                state=pipecat_state,
                artifacts=_cloud_artifacts(tmp_path, "pipecat-cloud"),
                smoke=CloudSmokeReport(
                    platform="pipecat-cloud",
                    agent_name=plan.agent_name,
                    platform_ready=True,
                    relay_ready=True,
                    session_smoke=False,
                ),
            )

    class LiveKitManager:
        def __init__(self, _root: Path) -> None:
            self.store = SimpleNamespace(
                path=tmp_path / ".voicey" / "deploy" / "livekit-cloud-resources.json"
            )

        def rollback(self, _plan: LiveKitCloudPlan) -> CloudResourceState:
            return livekit_state.checkpoint(rolled_back=True)

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("voicey.cli.app.PipecatCloudDeploymentManager", PipecatManager)
    web = runner.invoke(
        app,
        [*_pipecat_cloud_args(), "--skip-smoke", "--yes"],
    )
    assert web.exit_code == 0, web.stderr
    assert "Pipecat Cloud deployment completed." in web.stdout
    assert "Hosted carrier answer" not in web.stdout

    ManifestStore(tmp_path / "voicey.jsonc").save(
        manifest.model_copy(update={"runtime": "livekit", "deploy_target": "livekit-cloud"})
    )
    monkeypatch.setattr("voicey.cli.app.LiveKitCloudDeploymentManager", LiveKitManager)
    rollback = runner.invoke(
        app,
        [*_livekit_cloud_args(), "--rollback", "--yes"],
    )
    assert rollback.exit_code == 0, rollback.stderr
    assert "LiveKit Cloud rollback completed." in rollback.stdout
    assert ManifestStore(tmp_path / "voicey.jsonc").load().deploy_target is None

    invalid_rollback = runner.invoke(
        app,
        [*_livekit_cloud_args(), "--rollback", "--adopt", "--yes"],
    )
    assert invalid_rollback.exit_code == 1
    assert "VY-CLI-010" in invalid_rollback.stderr


def test_project_status_and_non_json_read_tables(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _project(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("voicey.cli.app.ProviderKeyValidator", AlwaysValidKeys)
    monkeypatch.setattr("voicey.cli.app._twilio", _fake_twilio)

    status = runner.invoke(app)
    recipes = runner.invoke(app, ["recipes", "list"])
    keys = runner.invoke(app, ["keys", "list"])
    numbers = runner.invoke(app, ["numbers", "list"])
    calls = runner.invoke(app, ["calls", "list"])

    for result in (status, recipes, keys, numbers, calls):
        assert result.exit_code == 0
        assert "Next:" in result.stdout
    assert "test-agent" in status.stdout
    assert "appointment-booking" in recipes.stdout


def test_mutating_phone_commands_execute_only_with_yes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _phone_project(tmp_path, record=True)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "AC" + "1" * 32)
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "token")
    monkeypatch.setattr("voicey.cli.app._twilio", _fake_twilio)
    FakeTwilio.events.clear()
    FakeTwilio.call_options.clear()

    commands = (
        ["numbers", "buy", "US", "--area", "415", "--yes"],
        ["numbers", "release", "+14155550123", "--yes"],
        [
            "numbers",
            "point",
            "+14155550123",
            "--url",
            "https://public.example.test",
            "--yes",
        ],
        ["numbers", "restore", "route_cli", "--yes"],
        [
            "call",
            "+14155550199",
            "--url",
            "https://public.example.test",
            "--yes",
        ],
    )
    for command in commands:
        result = runner.invoke(app, command)
        assert result.exit_code == 0, (command, result.stdout, result.stderr)
        assert "Next:" in result.stdout

    assert [event.split(":", maxsplit=1)[0] for event in FakeTwilio.events] == [
        "buy",
        "release",
        "point",
        "restore",
        "call",
    ]
    assert FakeTwilio.call_options == [{"amd": True, "record": True}]


def test_telnyx_phone_commands_use_telnyx_media_and_event_routes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _phone_project(tmp_path, carrier="telnyx")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("voicey.cli.app._carrier", _fake_telnyx)
    FakeTelnyx.events.clear()
    FakeTelnyx.call_options.clear()

    point = runner.invoke(
        app,
        [
            "numbers",
            "point",
            "+14155550123",
            "--url",
            "https://public.example.test",
            "--yes",
        ],
    )
    call = runner.invoke(
        app,
        [
            "call",
            "+14155550199",
            "--url",
            "https://public.example.test",
            "--yes",
        ],
    )

    assert point.exit_code == 0, point.stderr
    assert call.exit_code == 0, call.stderr
    assert FakeTelnyx.events == [
        "point:+14155550123:wss://public.example.test/telnyx/media",
        "call:+14155550123:+14155550199:wss://public.example.test/telnyx/media",
    ]
    assert FakeTelnyx.call_options == [{"amd": True, "record": False}]


def test_keys_add_uses_injected_value_and_recipe_add_copies_source(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _project(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DEEPGRAM_API_KEY", "injected")
    monkeypatch.setattr("voicey.cli.app.ProviderKeyValidator", AlwaysValidKeys)

    added = runner.invoke(app, ["keys", "add", "deepgram", "--yes"])
    recipe = runner.invoke(app, ["recipes", "add", "appointment-booking"])
    unknown = runner.invoke(app, ["keys", "add", "unknown", "--yes"])

    assert added.exit_code == 0
    assert "validated" in added.stdout
    assert recipe.exit_code == 0
    assert "Next:" in recipe.stdout
    assert (tmp_path / "flow.py").is_file()
    assert ManifestStore(tmp_path / "voicey.jsonc").load().recipe.name == ("appointment-booking")
    assert unknown.exit_code == 1
    assert "unknown provider" in unknown.stderr


def test_livekit_keys_are_listed_validated_and_runtime_scoped(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    manifest = _project(tmp_path).model_copy(update={"runtime": "livekit"})
    ManifestStore(tmp_path / "voicey.jsonc").save(manifest)
    with (tmp_path / ".env").open("a", encoding="utf-8") as env:
        env.write(
            'LIVEKIT_URL="wss://project.livekit.cloud"\n'
            'LIVEKIT_API_KEY="livekit-key"\n'  # pragma: allowlist secret
            'LIVEKIT_API_SECRET="livekit-secret"\n'  # pragma: allowlist secret
        )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("voicey.cli.app.ProviderKeyValidator", AlwaysValidKeys)
    monkeypatch.setattr("voicey.cli.app.LiveKitKeyValidator", AlwaysValidLiveKit)

    listed = runner.invoke(app, ["keys", "list", "--json"])
    added = runner.invoke(app, ["keys", "add", "livekit", "--yes"])

    assert listed.exit_code == 0
    items = json.loads(listed.stdout)["items"]
    assert items[-1]["provider"] == "livekit"
    assert items[-1]["keys"]["LIVEKIT_API_SECRET"].startswith("••••")
    assert added.exit_code == 0
    assert "livekit credentials validated" in added.stdout

    ManifestStore(tmp_path / "voicey.jsonc").save(
        manifest.model_copy(update={"runtime": "pipecat"})
    )
    wrong_runtime = runner.invoke(app, ["keys", "add", "livekit", "--yes"])
    assert wrong_runtime.exit_code == 1
    assert "apply only to a LiveKit-runtime project" in wrong_runtime.stderr


def test_calls_list_show_and_redeliver_real_repository(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _project(tmp_path)
    monkeypatch.chdir(tmp_path)
    call_id, event_id = asyncio.run(_seed_call(tmp_path))

    listed = runner.invoke(app, ["calls", "list", "--json"])
    shown = runner.invoke(app, ["calls", "show", call_id, "--json"])
    redelivered = runner.invoke(app, ["calls", "redeliver", call_id, "--yes"])
    undelivered = runner.invoke(app, ["calls", "list", "--undelivered", "--json"])

    assert listed.exit_code == 0
    assert json.loads(listed.stdout)["items"][0]["call_id"] == call_id
    assert shown.exit_code == 0
    assert json.loads(shown.stdout)["call"]["call_id"] == call_id
    assert redelivered.exit_code == 0
    assert event_id in redelivered.stdout
    assert json.loads(undelivered.stdout)["items"][0]["event_id"] == event_id


def test_cli_model_assignment_validation_and_cataloged_unmapped_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)
    malformed = runner.invoke(
        app,
        [
            "init",
            str(tmp_path / "bad"),
            "--models",
            "not-an-assignment",
            "--yes",
        ],
    )
    duplicate = runner.invoke(
        app,
        [
            "init",
            str(tmp_path / "duplicate"),
            "--models",
            "stt=one,stt=two",
            "--yes",
        ],
    )

    assert malformed.exit_code == 1
    assert "VY-CLI-010" in malformed.stderr
    assert duplicate.exit_code == 1
    assert "duplicate" in duplicate.stderr


def test_successful_init_and_dev_command_adapters(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    models: dict[ModelAxis, str] = {
        "stt": "deepgram/nova-3",
        "llm": "anthropic/claude-sonnet-5",
        "tts": "cartesia/sonic-3.5",
    }
    manifest = ProjectManifest(
        project_name="created",
        runtime="pipecat",
        recipe=RecipeSelection(name="scratch", version="1.0.0"),
        channels=frozenset({"web"}),
        models=models,
    )
    dev_calls: list[dict[str, object]] = []

    class FakeWizard:
        def __init__(self, *, prompt: object) -> None:
            assert prompt

        async def run(self, path: Path, options: object) -> InitResult:
            assert options
            return InitResult(
                project_dir=path,
                manifest=manifest,
                written=(path / "agent.py",),
                next_step="voicey dev",
            )

    async def fake_dev(*_args: object, **kwargs: object) -> None:
        dev_calls.append(dict(kwargs))

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("voicey.cli.app.InitWizard", FakeWizard)
    monkeypatch.setattr("voicey.cli.dev.run_dev", fake_dev)

    initialized = runner.invoke(
        app,
        [
            "init",
            str(tmp_path / "created"),
            "--name",
            "created",
            "--recipe",
            "scratch",
            "--description",
            "Help.",
            "--channels",
            "web",
            "--runtime",
            "pipecat",
            "--models",
            ("stt=deepgram/nova-3,llm=anthropic/claude-sonnet-5,tts=cartesia/sonic-3.5"),
            "--no-draft-prompts",
            "--yes",
        ],
    )
    developed = runner.invoke(
        app,
        ["dev", "--no-phone", "--tunnel", "auto", "--port", "9000", "--no-open"],
    )

    assert initialized.exit_code == 0
    assert "voicey dev" in initialized.stdout
    assert developed.exit_code == 0
    assert dev_calls[0]["port"] == 9000
    assert dev_calls[0]["open_browser"] is False
