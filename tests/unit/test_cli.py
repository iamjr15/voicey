from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar

import pytest
from typer.testing import CliRunner

from voicekit import __version__
from voicekit.cli.app import app
from voicekit.cli.doctor import DoctorCheck, DoctorReport
from voicekit.cli.keys import KeyCheck
from voicekit.cli.wizard import InitResult
from voicekit.config.catalog import ProviderKind
from voicekit.config.manifest import ManifestStore, ProjectManifest, RecipeSelection
from voicekit.config.models import ModelAxis
from voicekit.deploy.docker import DockerSmokeResult
from voicekit.obs.records import NewCall
from voicekit.storage.models import ResultDeliveryConfig, TerminalRequest
from voicekit.storage.sqlite import SQLiteRepository
from voicekit.telephony.models import NumberInfo, PipecatTarget, RollbackToken

runner = CliRunner()


def test_bare_command_prints_status_and_next_step() -> None:
    result = runner.invoke(app)

    assert result.exit_code == 0
    assert "voicekit is installed" in result.stdout
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
    ManifestStore(path / "voicekit.jsonc").save(manifest)
    (path / ".env").write_text(
        'DEEPGRAM_API_KEY="dg"\n'  # pragma: allowlist secret
        'ANTHROPIC_API_KEY="ant"\n'  # pragma: allowlist secret
        'CARTESIA_API_KEY="car"\n'  # pragma: allowlist secret
        'VOICEKIT_WEBHOOK_SECRET="whsec_dGVzdA=="\n',  # pragma: allowlist secret
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
    ) -> str:
        self.events.append(f"call:{from_number}:{to_number}:{target.https_base}")
        return "CA" + "1" * 32


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


def _phone_project(path: Path) -> ProjectManifest:
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
        carriers=["twilio"],
        phone_number="+14155550123",
    )
    ManifestStore(path / "voicekit.jsonc").save(manifest)
    return manifest


async def _seed_call(path: Path) -> tuple[str, str]:
    call_id = "call_cli_test"
    async with SQLiteRepository(path / ".voicekit" / "calls.sqlite3") as repository:
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
    assert payload["next_step"] == "voicekit init"


def test_command_tree_and_flag_twins_are_exposed() -> None:
    root = runner.invoke(app, ["--help"])
    assert root.exit_code == 0
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
        assert command in root.stdout

    init_help = runner.invoke(app, ["init", "--help"])
    assert init_help.exit_code == 0
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
        assert flag in init_help.stdout

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
    ):
        help_result = runner.invoke(app, command)
        assert help_result.exit_code == 0
        for flag in expected:
            assert flag in help_result.stdout


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
    assert "VK-CLI-001" in result.stderr
    assert (project / "voicekit.jsonc").exists()
    assert "init-checkpoint" in (project / "voicekit.jsonc").read_text(encoding="utf-8")


def test_read_commands_have_json_paths(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _project(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("voicekit.cli.app.ProviderKeyValidator", AlwaysValidKeys)
    monkeypatch.setattr("voicekit.cli.app._twilio", _fake_twilio)
    monkeypatch.setattr("voicekit.cli.app.Doctor", FakeDoctor)

    commands = (
        ["--json"],
        ["recipes", "list", "--json"],
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
    assert json.loads(missing_call.stdout)["error"]["code"] == "VK-OBS-003"

    future = runner.invoke(app, ["recipes", "update-check", "--json"])
    assert future.exit_code == 1
    assert json.loads(future.stdout)["error"]["code"] == "VK-CLI-005"


def test_money_and_live_mutations_require_confirmation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _project(tmp_path)
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["numbers", "buy", "US"])

    assert result.exit_code == 1
    assert "VK-CLI-008" in result.stderr


def test_future_capability_commands_fail_with_cataloged_error() -> None:
    for command in (["upgrade"],):
        result = runner.invoke(app, command)
        assert result.exit_code == 1
        assert "VK-CLI-005" in result.stderr
        assert "Next:" in result.stderr


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
            assert engine_wheel == tmp_path / "voicekit.whl"
            calls.append("generate")
            return SimpleNamespace(
                dockerfile=tmp_path / "Dockerfile.voicekit",
                compose=tmp_path / "compose.voicekit.yaml",
                dockerignore=tmp_path / ".dockerignore",
                environment_example=tmp_path / "docker.env.example",
                engine_wheel=tmp_path / ".voicekit" / "deploy" / "voicekit.whl",
            )

        def validate(self, _artifacts: object) -> None:
            calls.append("validate")

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("voicekit.cli.app.DockerDeploymentGenerator", Generator)

    result = runner.invoke(
        app,
        [
            "deploy",
            "docker",
            "--engine-wheel",
            str(tmp_path / "voicekit.whl"),
        ],
    )

    assert result.exit_code == 0, result.stderr
    assert calls == ["generate", "validate"]
    assert "docker compose -f compose.voicekit.yaml up -d --build" in result.stdout
    assert "--engine-wheel" in result.stdout
    assert "voicekit.whl" in result.stdout
    assert ManifestStore(tmp_path / "voicekit.jsonc").load().deploy_target == "docker"


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
                dockerfile=tmp_path / "Dockerfile.voicekit",
                compose=tmp_path / "compose.voicekit.yaml",
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
    monkeypatch.setattr("voicekit.cli.app.DockerDeploymentGenerator", Generator)
    monkeypatch.setattr("voicekit.cli.app.DockerSmokeVerifier", Smoke)
    monkeypatch.setattr("voicekit.cli.app._twilio", _fake_twilio)
    FakeTwilio.events = []

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


def test_project_status_and_non_json_read_tables(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _project(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("voicekit.cli.app.ProviderKeyValidator", AlwaysValidKeys)
    monkeypatch.setattr("voicekit.cli.app._twilio", _fake_twilio)

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
    _phone_project(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "AC" + "1" * 32)
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "token")
    monkeypatch.setattr("voicekit.cli.app._twilio", _fake_twilio)
    FakeTwilio.events.clear()

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


def test_keys_add_uses_injected_value_and_recipe_add_copies_source(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _project(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DEEPGRAM_API_KEY", "injected")
    monkeypatch.setattr("voicekit.cli.app.ProviderKeyValidator", AlwaysValidKeys)

    added = runner.invoke(app, ["keys", "add", "deepgram", "--yes"])
    recipe = runner.invoke(app, ["recipes", "add", "appointment-booking"])
    unknown = runner.invoke(app, ["keys", "add", "unknown", "--yes"])

    assert added.exit_code == 0
    assert "validated" in added.stdout
    assert recipe.exit_code == 0
    assert "Next:" in recipe.stdout
    assert (tmp_path / "flow.py").is_file()
    assert ManifestStore(tmp_path / "voicekit.jsonc").load().recipe.name == ("appointment-booking")
    assert unknown.exit_code == 1
    assert "unknown provider" in unknown.stderr


def test_livekit_keys_are_listed_validated_and_runtime_scoped(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    manifest = _project(tmp_path).model_copy(update={"runtime": "livekit"})
    ManifestStore(tmp_path / "voicekit.jsonc").save(manifest)
    with (tmp_path / ".env").open("a", encoding="utf-8") as env:
        env.write(
            'LIVEKIT_URL="wss://project.livekit.cloud"\n'
            'LIVEKIT_API_KEY="livekit-key"\n'  # pragma: allowlist secret
            'LIVEKIT_API_SECRET="livekit-secret"\n'  # pragma: allowlist secret
        )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("voicekit.cli.app.ProviderKeyValidator", AlwaysValidKeys)
    monkeypatch.setattr("voicekit.cli.app.LiveKitKeyValidator", AlwaysValidLiveKit)

    listed = runner.invoke(app, ["keys", "list", "--json"])
    added = runner.invoke(app, ["keys", "add", "livekit", "--yes"])

    assert listed.exit_code == 0
    items = json.loads(listed.stdout)["items"]
    assert items[-1]["provider"] == "livekit"
    assert items[-1]["keys"]["LIVEKIT_API_SECRET"].startswith("••••")
    assert added.exit_code == 0
    assert "livekit credentials validated" in added.stdout

    ManifestStore(tmp_path / "voicekit.jsonc").save(
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
    assert "VK-CLI-010" in malformed.stderr
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
                next_step="voicekit dev",
            )

    async def fake_dev(*_args: object, **kwargs: object) -> None:
        dev_calls.append(dict(kwargs))

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("voicekit.cli.app.InitWizard", FakeWizard)
    monkeypatch.setattr("voicekit.cli.dev.run_dev", fake_dev)

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
    assert "voicekit dev" in initialized.stdout
    assert developed.exit_code == 0
    assert dev_calls[0]["port"] == 9000
    assert dev_calls[0]["open_browser"] is False
