# pyright: reportPrivateUsage=false

from __future__ import annotations

import json
import stat
import subprocess
from collections.abc import Sequence
from dataclasses import asdict, replace
from pathlib import Path

import httpx
import pytest

from voicey.cli.environment import EnvFileStore
from voicey.deploy.railway import (
    RailwayArtifactGenerator,
    RailwayCliRunner,
    RailwayCommandResult,
    RailwayDeploymentManager,
    RailwayPlan,
    RailwayResourceState,
    RailwayResourceStore,
    _domain_text,
    _find_text,
    _item_text,
    _json_items,
    _parse_json,
    _variable_reference,
)
from voicey.errors import VoiceyError
from voicey.relay.auth import RelayCredential


class FakeRailwayRunner:
    def __init__(
        self,
        *,
        existing: bool = False,
        deployment_status: str = "SUCCESS",
        version: str = "railway 5.30.1",
    ) -> None:
        self.version = version
        self.project_id = "project_123"
        self.environment_id = "environment_123"
        self.service_id = "service_123"
        self.postgres_id = "postgres_123"
        self.bucket_id = "bucket_123"
        self.domain_id = "domain_123"
        self.projects: list[dict[str, object]] = (
            [{"id": self.project_id, "name": "voicey-results"}] if existing else []
        )
        self.services: list[dict[str, object]] = (
            [
                {"id": self.service_id, "name": "voicey-results"},
                {"id": self.postgres_id, "name": "Postgres"},
            ]
            if existing
            else []
        )
        self.buckets: list[dict[str, object]] = (
            [{"id": self.bucket_id, "name": "voicey-results-objects"}] if existing else []
        )
        self.domains: list[dict[str, object]] = (
            [
                {
                    "id": self.domain_id,
                    "domain": "voicey-results-production.up.railway.app",
                }
            ]
            if existing
            else []
        )
        self.deployment_status = deployment_status
        self.regions: dict[str, dict[str, int]] = {"ams": {"numReplicas": 2}}
        self.postgres_regions: dict[str, dict[str, int]] = {"us-east4-eqdc4a": {"numReplicas": 1}}
        self.reference_failures_remaining = 0
        self.commands: list[tuple[str, ...]] = []
        self.secret_payloads: list[tuple[str, str]] = []

    def run(
        self,
        arguments: Sequence[str],
        *,
        stdin: str | None = None,
        check: bool = True,
        timeout_s: float = 600,
    ) -> RailwayCommandResult:
        del timeout_s
        command = tuple(arguments)
        self.commands.append(command)
        result = self._dispatch(command, stdin)
        if check and result.returncode:
            raise AssertionError(f"unexpected fake Railway failure: {command}")
        return result

    def _dispatch(
        self,
        command: tuple[str, ...],
        stdin: str | None,
    ) -> RailwayCommandResult:
        if command == ("--version",):
            return _result(stdout=f"{self.version}\n")
        if command == ("whoami",):
            return _result(stdout="developer@example.test\n")
        if command[:2] == ("list", "--json"):
            return _result(stdout=json.dumps(self.projects))
        if command[0] == "init":
            self.projects = [{"id": self.project_id, "name": "voicey-results"}]
            return _result(
                stdout=json.dumps(
                    {
                        "projectId": self.project_id,
                        "environmentId": self.environment_id,
                    }
                )
            )
        if command[0] == "link":
            return _result(
                stdout=json.dumps(
                    {
                        "projectId": self.project_id,
                        "environmentId": self.environment_id,
                    }
                )
            )
        if command[:2] == ("service", "list"):
            return _result(stdout=json.dumps(self.services))
        if command[:2] == ("service", "link"):
            return _result(stdout='{"linked":true}')
        if command[:2] == ("add", "--service"):
            self.services.append({"id": self.service_id, "name": command[2]})
            return _result(stdout=json.dumps({"serviceId": self.service_id}))
        if command[:2] == ("add", "--database"):
            self.services.append({"id": self.postgres_id, "name": "Postgres"})
            return _result(
                stdout=json.dumps(
                    {
                        "serviceId": self.postgres_id,
                        "serviceName": "Postgres",
                    }
                )
            )
        if command[:2] == ("bucket", "list"):
            return _result(stdout=json.dumps(self.buckets))
        if command[:2] == ("bucket", "create"):
            self.buckets.append({"id": self.bucket_id, "name": command[2]})
            return _result(stdout=json.dumps({"bucketId": self.bucket_id}))
        if command[:2] == ("domain", "list"):
            return _result(stdout=json.dumps(self.domains))
        if command[0] == "domain" and command[1] == "--port":
            self.domains.append(
                {
                    "id": self.domain_id,
                    "domain": "voicey-results-production.up.railway.app",
                }
            )
            return _result(
                stdout=json.dumps(
                    {
                        "domainId": self.domain_id,
                        "domain": "voicey-results-production.up.railway.app",
                    }
                )
            )
        if command[:2] == ("variable", "set"):
            if "--stdin" in command:
                assert stdin is not None
                self.secret_payloads.append((command[2], stdin))
            elif self.reference_failures_remaining:
                self.reference_failures_remaining -= 1
                return _result(returncode=1, stderr="resource reference is not ready")
            return _result(stdout='{"updated":true}')
        if command[0] == "up":
            return _result(stdout='{"deploymentId":"deployment_123"}')
        if command[:2] == ("deployment", "list"):
            return _result(
                stdout=json.dumps(
                    [
                        {
                            "id": "deployment_123",
                            "status": self.deployment_status,
                        }
                    ]
                )
            )
        if command[0] == "scale":
            aliases = {"us-east": "us-east4-eqdc4a"}
            for assignment in command[1:]:
                if "=" not in assignment:
                    continue
                region, raw_replicas = assignment.split("=", maxsplit=1)
                resolved = aliases.get(region, region)
                replicas = int(raw_replicas)
                if replicas:
                    self.regions[resolved] = {"numReplicas": replicas}
                else:
                    self.regions.pop(resolved, None)
            return _result(stdout=json.dumps({"regions": self.regions}))
        if command[:2] == ("service", "status"):
            return _result(stdout='{"status":"SUCCESS"}')
        if command[:2] == ("status", "--json"):
            return _result(
                stdout=json.dumps(
                    {
                        "environments": {
                            "edges": [
                                {
                                    "node": {
                                        "serviceInstances": {
                                            "edges": [
                                                {
                                                    "node": {
                                                        "serviceId": self.service_id,
                                                        "latestDeployment": {
                                                            "meta": {
                                                                "serviceManifest": {
                                                                    "deploy": {
                                                                        "multiRegionConfig": (
                                                                            self.regions
                                                                        )
                                                                    }
                                                                }
                                                            }
                                                        },
                                                    }
                                                },
                                                {
                                                    "node": {
                                                        "serviceId": self.postgres_id,
                                                        "latestDeployment": {
                                                            "meta": {
                                                                "serviceManifest": {
                                                                    "deploy": {
                                                                        "multiRegionConfig": (
                                                                            self.postgres_regions
                                                                        )
                                                                    }
                                                                }
                                                            }
                                                        },
                                                    }
                                                },
                                            ]
                                        }
                                    }
                                }
                            ]
                        }
                    }
                )
            )
        if command[:2] == ("domain", "delete"):
            self.domains.clear()
            return _result(stdout='{"deleted":true}')
        if command[:2] == ("bucket", "delete"):
            self.buckets.clear()
            return _result(stdout='{"deleted":true}')
        if command[:2] == ("service", "delete"):
            identity = command[command.index("--service") + 1]
            self.services = [item for item in self.services if item.get("id") != identity]
            return _result(stdout='{"deleted":true}')
        if command[0] == "delete":
            self.projects.clear()
            return _result(stdout='{"deleted":true}')
        raise AssertionError(f"unhandled fake Railway command: {command}")


def _result(
    *,
    returncode: int = 0,
    stdout: str = "",
    stderr: str = "",
) -> RailwayCommandResult:
    return RailwayCommandResult(
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
    )


def _plan(*, project_id: str | None = None) -> RailwayPlan:
    return RailwayPlan(
        project_name="voicey-results",
        workspace="workspace_123",
        environment="production",
        service_name="voicey-results",
        bucket_name="voicey-results-objects",
        service_region="us-east",
        bucket_region="iad",
        callback_providers=("twilio",),
        project_id=project_id,
    )


def _wheel(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    wheel = path / "voicey-0.0.0.dev0-py3-none-any.whl"
    wheel.write_bytes(b"wheel fixture")
    return wheel


def _environment() -> dict[str, str]:
    return {
        "TWILIO_ACCOUNT_SID": f"AC{'a' * 32}",
        "TWILIO_AUTH_TOKEN": "test-token",  # pragma: allowlist secret
        "VOICEY_OTLP_ENDPOINT": "https://collector.example.test/v1/traces",
        "VOICEY_OTLP_HEADERS": "authorization=test-only",
    }


def _http_client() -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/healthz":
            return httpx.Response(200, json={"status": "ok"})
        if request.url.path == "/v1/ready":
            assert request.headers["authorization"].startswith("VoiceyRelay ")
            return httpx.Response(
                200,
                json={
                    "ready": True,
                    "protocol": "voicey-results-relay/v1",
                    "storage_ready": True,
                },
            )
        return httpx.Response(404)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_railway_plan_artifacts_and_helpers_are_strict_and_secret_free(
    tmp_path: Path,
) -> None:
    artifacts = RailwayArtifactGenerator(tmp_path).generate(engine_wheel=_wheel(tmp_path))
    config = json.loads(artifacts.config.read_text(encoding="utf-8"))
    dockerfile = artifacts.dockerfile.read_text(encoding="utf-8")

    assert config["build"] == {
        "builder": "DOCKERFILE",
        "dockerfilePath": "Dockerfile.results",
    }
    assert config["deploy"]["numReplicas"] == 2
    assert config["deploy"]["healthcheckPath"] == "/healthz"
    assert "--preflight-only" in config["deploy"]["preDeployCommand"]
    assert "voicey.deploy.results_service" in dockerfile
    assert "USER 10001:10001" in dockerfile
    assert "VOICEY_RELAY_CREDENTIAL" not in artifacts.config.read_text()
    assert _variable_reference("Postgres", "DATABASE_URL") == ("${{Postgres.DATABASE_URL}}")
    assert _json_items({"services": [{"id": "one"}, 2]}) == [{"id": "one"}]
    assert _find_text({"data": {"projectId": "project"}}) == ""
    assert _find_text({"data": {"projectId": "project"}}, "projectId") == "project"
    assert _parse_json('{"ok":true}', label="test") == {"ok": True}

    with pytest.raises(VoiceyError, match="VY-DEP-003"):
        RailwayPlan(
            project_name="Bad_Project",
            workspace="workspace",
            environment="production",
            service_name="voicey-results",
            bucket_name="voicey-results-objects",
            service_region="us-east",
            bucket_region="iad",
        )
    with pytest.raises(VoiceyError, match="VY-DEP-003"):
        replace(_plan(), bucket_region="ams")
    with pytest.raises(VoiceyError, match="VY-DEP-007"):
        _variable_reference("unsafe}}", "DATABASE_URL")
    with pytest.raises(VoiceyError, match="VY-DEP-007"):
        _parse_json("not-json", label="test")


def test_railway_output_normalization_and_state_validation(tmp_path: Path) -> None:
    assert _json_items([{"name": "one"}, "discard"]) == [{"name": "one"}]
    assert _json_items({"data": [{"name": "two"}, 3]}) == [{"name": "two"}]
    assert _json_items({"name": "singleton"}) == [{"name": "singleton"}]
    assert _json_items({}) == []
    assert _json_items("invalid") == []
    assert _item_text({"NAME": "upper"}, "name") == "upper"
    assert _item_text({"serviceId": "service_1"}, "service") == "service_1"
    assert _item_text({"name": 3}, "name") == ""
    assert _domain_text({"url": "https://result.up.railway.app"}) == ("result.up.railway.app")

    with pytest.raises(VoiceyError, match="not an object"):
        RailwayResourceState.from_payload([])
    with pytest.raises(VoiceyError, match="fields are invalid"):
        RailwayResourceState.from_payload({"schema_version": "bad"})
    invalid = {
        **asdict(RailwayResourceState.initial(_plan())),
        "schema_version": 99,
    }
    with pytest.raises(VoiceyError, match="version or identity"):
        RailwayResourceState.from_payload(invalid)


def test_railway_scale_maps_missing_ledgered_service_to_catalog_error(tmp_path: Path) -> None:
    runner = FakeRailwayRunner()
    manager = RailwayDeploymentManager(tmp_path, runner=runner)
    state = replace(RailwayResourceState.initial(_plan()), service_id="missing-service")

    with pytest.raises(VoiceyError, match="VY-DEP-007"):
        manager._scale_service(_plan(), state)

    state = RailwayResourceState.initial(_plan())
    with pytest.raises(VoiceyError, match="does not match"):
        state.validate_plan(
            RailwayPlan(
                project_name="other-project",
                workspace="workspace_123",
                environment="production",
                service_name="voicey-results",
                bucket_name="voicey-results-objects",
                service_region="us-east",
                bucket_region="iad",
            )
        )
    with pytest.raises(VoiceyError, match="rolled back"):
        state.checkpoint(rolled_back=True).validate_plan(_plan())

    invalid_json = tmp_path / "invalid.json"
    invalid_json.write_text("{bad json", encoding="utf-8")
    invalid_json.chmod(0o600)
    with pytest.raises(VoiceyError, match="cannot be read"):
        RailwayResourceStore(invalid_json).load()

    target = tmp_path / "target.json"
    target.write_text("{}\n", encoding="utf-8")
    link = tmp_path / "linked.json"
    link.symlink_to(target)
    with pytest.raises(VoiceyError, match="VY-SEC-002"):
        RailwayResourceStore(link).load()
    with pytest.raises(VoiceyError, match="VY-SEC-002"):
        RailwayResourceStore(link).save(state)


def test_railway_rejects_cross_region_postgres_before_release(tmp_path: Path) -> None:
    runner = FakeRailwayRunner()
    runner.postgres_regions = {"ams": {"numReplicas": 1}}
    manager = RailwayDeploymentManager(tmp_path, runner=runner)
    state = replace(
        RailwayResourceState.initial(_plan()),
        postgres_id=runner.postgres_id,
        postgres_name="Postgres",
    )

    with pytest.raises(VoiceyError, match="outside the selected companion region"):
        manager._verify_postgres_placement(_plan(), state)

    runner.postgres_regions = {"us-east4-eqdc4a": {"numReplicas": 2}}
    with pytest.raises(VoiceyError, match="exactly one volume-backed replica"):
        manager._verify_postgres_placement(_plan(), state)


def test_railway_retries_eventually_consistent_resource_references(tmp_path: Path) -> None:
    runner = FakeRailwayRunner()
    runner.reference_failures_remaining = 2
    manager = RailwayDeploymentManager(tmp_path, runner=runner, poll_interval_s=0)
    state = replace(
        RailwayResourceState.initial(_plan()),
        project_id=runner.project_id,
        environment_id=runner.environment_id,
        service_id=runner.service_id,
    )

    manager._sync_reference_variables(state, ["DATABASE_URL=${{Postgres.DATABASE_URL}}"])

    reference_commands = [
        command
        for command in runner.commands
        if command[:3] == ("variable", "set", "DATABASE_URL=${{Postgres.DATABASE_URL}}")
    ]
    assert len(reference_commands) == 3

    runner.reference_failures_remaining = 5
    with pytest.raises(VoiceyError, match="did not resolve newly created"):
        manager._sync_reference_variables(state, ["AWS_REGION=${{bucket.REGION}}"])
    failed_commands = [
        command
        for command in runner.commands
        if command[:3] == ("variable", "set", "AWS_REGION=${{bucket.REGION}}")
    ]
    assert len(failed_commands) == 5


def test_railway_artifacts_support_published_install_and_reject_bad_wheels(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    generator = RailwayArtifactGenerator(tmp_path)
    with pytest.raises(VoiceyError, match=r"requires.*engine-wheel"):
        generator.generate(engine_wheel=None)
    bad = tmp_path / "not-voicey.txt"
    bad.write_text("bad", encoding="utf-8")
    with pytest.raises(VoiceyError, match="engine wheel is invalid"):
        generator.generate(engine_wheel=bad)

    monkeypatch.setattr("voicey.deploy.railway.__version__", "1.0.0")
    published = generator.generate(engine_wheel=None)
    assert published.engine_wheel is None
    assert '"voicey[companion]==1.0.0"' in published.dockerfile.read_text(encoding="utf-8")

    monkeypatch.setattr("voicey.deploy.railway._railway_config", lambda: "{}\n")
    with pytest.raises(VoiceyError, match="topology invariant"):
        generator.generate(engine_wheel=None)


def test_railway_cli_runner_maps_missing_process_failure_and_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def missing_executable(_name: str) -> None:
        return None

    monkeypatch.setattr("voicey.deploy.railway.shutil.which", missing_executable)
    with pytest.raises(VoiceyError, match="VY-DEP-006"):
        RailwayCliRunner(tmp_path)

    success = subprocess.CompletedProcess(
        args=["railway", "--version"],
        returncode=0,
        stdout="railway 5.30.1\n",
        stderr="",
    )

    def successful_run(
        *_args: object,
        **_kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        return success

    monkeypatch.setattr("voicey.deploy.railway.subprocess.run", successful_run)
    runner = RailwayCliRunner(tmp_path, "/usr/local/bin/railway")
    assert runner.run(["--version"]).stdout == "railway 5.30.1\n"

    failed = subprocess.CompletedProcess(
        args=["railway", "whoami"],
        returncode=1,
        stdout="",
        stderr="unauthorized",
    )

    def failed_run(
        *_args: object,
        **_kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        return failed

    monkeypatch.setattr("voicey.deploy.railway.subprocess.run", failed_run)
    with pytest.raises(VoiceyError, match="VY-DEP-006"):
        runner.run(["whoami"])

    def timeout(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(["railway"], timeout=1)

    monkeypatch.setattr("voicey.deploy.railway.subprocess.run", timeout)
    with pytest.raises(VoiceyError, match="VY-DEP-006"):
        runner.run(["whoami"])


def test_railway_version_and_identity_drift_guards(tmp_path: Path) -> None:
    malformed = RailwayDeploymentManager(
        tmp_path / "version",
        runner=FakeRailwayRunner(version="railway current"),
    )
    with pytest.raises(VoiceyError, match="semantic version"):
        malformed._authenticate()

    runner = FakeRailwayRunner()
    manager = RailwayDeploymentManager(tmp_path / "identity", runner=runner)
    project_state = RailwayResourceState.initial(_plan()).checkpoint(project_id=runner.project_id)
    with pytest.raises(VoiceyError, match="project is missing"):
        manager._ensure_project(_plan(), project_state, False)

    runner.projects = [{"id": runner.project_id, "name": "renamed-project"}]
    with pytest.raises(VoiceyError, match="project name drifted"):
        manager._ensure_project(_plan(), project_state, False)

    linked = project_state.checkpoint(environment_id=runner.environment_id)
    runner.services = [
        {"id": "duplicate_one", "name": "voicey-results"},
        {"id": "duplicate_two", "name": "voicey-results"},
    ]
    with pytest.raises(VoiceyError, match="service identity is ambiguous"):
        manager._ensure_service(_plan(), linked, False)

    service_state = linked.checkpoint(service_id=runner.service_id)
    runner.services = []
    with pytest.raises(VoiceyError, match="service is missing"):
        manager._ensure_service(_plan(), service_state, False)

    postgres_state = service_state.checkpoint(
        postgres_id=runner.postgres_id,
        postgres_name="Postgres",
    )
    with pytest.raises(VoiceyError, match="Postgres is missing"):
        manager._ensure_postgres(_plan(), postgres_state, False)

    bucket_state = service_state.checkpoint(bucket_id=runner.bucket_id)
    runner.buckets = []
    with pytest.raises(VoiceyError, match="bucket is missing"):
        manager._ensure_bucket(_plan(), bucket_state, False)

    domain_state = service_state.checkpoint(
        domain_id=runner.domain_id,
        public_base="https://missing.up.railway.app",
    )
    runner.domains = []
    with pytest.raises(VoiceyError, match="domain is missing"):
        manager._ensure_domain(_plan(), domain_state, False)

    with pytest.raises(VoiceyError, match="context is incomplete"):
        manager._context_args(RailwayResourceState.initial(_plan()))
    with pytest.raises(VoiceyError, match="another project"):
        manager._checkpoint_link(
            project_state,
            {
                "projectId": "another_project",
                "environmentId": runner.environment_id,
            },
        )
    with pytest.raises(VoiceyError, match="omitted the environment"):
        manager._checkpoint_link(project_state, {"projectId": runner.project_id})


@pytest.mark.asyncio
async def test_railway_fallback_resolution_rotation_and_credential_drift(
    tmp_path: Path,
) -> None:
    class SparseCreateRunner(FakeRailwayRunner):
        def _dispatch(
            self,
            command: tuple[str, ...],
            stdin: str | None,
        ) -> RailwayCommandResult:
            result = super()._dispatch(command, stdin)
            if (
                command[0] == "init"
                or command[:2] == ("add", "--service")
                or command[:2] == ("add", "--database")
                or command[:2] == ("bucket", "create")
                or (command[0] == "domain" and command[1] == "--port")
            ):
                return _result(stdout="{}")
            return result

    runner = SparseCreateRunner()
    manager = RailwayDeploymentManager(
        tmp_path,
        runner=runner,
        http_client=_http_client(),
        poll_interval_s=0,
    )
    wheel = _wheel(tmp_path)
    first = await manager.deploy(
        replace(_plan(), callback_providers=()),
        environment={},
        engine_wheel=wheel,
        skip_smoke=True,
    )
    before = EnvFileStore(tmp_path / ".env").read()
    second = await manager.deploy(
        replace(_plan(), callback_providers=()),
        environment={},
        engine_wheel=wheel,
        rotate_credentials=True,
        skip_smoke=True,
    )
    after = EnvFileStore(tmp_path / ".env").read()

    assert first.state.project_id == runner.project_id
    assert first.state.postgres_name == "Postgres"
    assert after["VOICEY_RELAY_PREVIOUS_CREDENTIAL"] == (before["VOICEY_RELAY_CREDENTIAL"])
    assert after["VOICEY_RESULTS_PREVIOUS_SECRET"] == (before["VOICEY_RESULTS_SECRET"])
    assert second.state.relay_fingerprint != first.state.relay_fingerprint
    assert second.state.results_fingerprint != first.state.results_fingerprint

    command_count = len(runner.commands)
    with pytest.raises(VoiceyError, match="VY-DEP-007"):
        await manager.deploy(
            replace(_plan(), callback_providers=()),
            environment={"VOICEY_RELAY_CREDENTIAL": RelayCredential.issue("drift").reveal()},
            engine_wheel=wheel,
            skip_smoke=True,
        )
    assert len(runner.commands) == command_count
    await manager.http_client.aclose()  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_railway_deploy_resumes_and_keeps_secrets_out_of_arguments(
    tmp_path: Path,
) -> None:
    runner = FakeRailwayRunner()
    manager = RailwayDeploymentManager(
        tmp_path,
        runner=runner,
        http_client=_http_client(),
        poll_interval_s=0,
    )
    report = await manager.deploy(
        _plan(),
        environment=_environment(),
        engine_wheel=_wheel(tmp_path),
    )

    assert report.smoke is not None
    assert report.smoke.migration_preflight
    assert report.smoke.rolling_generation_preflight
    assert report.state.project_created
    assert report.state.service_created
    assert report.state.postgres_created
    assert report.state.bucket_created
    assert report.state.domain_created
    assert report.state.preflight_green
    assert report.state.smoke_green
    assert stat.S_IMODE(manager.store.path.stat().st_mode) == 0o600

    flattened = "\n".join(" ".join(command) for command in runner.commands)
    assert "test-token" not in flattened
    assert "authorization=test-only" not in flattened
    assert "${{Postgres.DATABASE_URL}}" in flattened
    assert "${{voicey-results-objects.SECRET_ACCESS_KEY}}" in flattened
    assert "RAILWAY_DEPLOYMENT_OVERLAP_SECONDS=30" in flattened
    assert "VOICEY_PROMETHEUS_ENABLED=1" in flattened
    assert "VOICEY_PROMETHEUS_BIND=0.0.0.0" in flattened
    assert "VOICEY_PROMETHEUS_PORT=9464" in flattened
    scales = [command for command in runner.commands if command[0] == "scale"]
    assert scales == [
        ("scale", "--json", "us-east=2"),
        ("scale", "--json", "ams=0", "us-east4-eqdc4a=2"),
    ]
    assert runner.regions == {"us-east4-eqdc4a": {"numReplicas": 2}}
    assert ("service", "link", runner.service_id) in runner.commands
    assert {name for name, _value in runner.secret_payloads} >= {
        "TWILIO_ACCOUNT_SID",
        "TWILIO_AUTH_TOKEN",
        "VOICEY_RELAY_CREDENTIAL",
        "VOICEY_RESULTS_SECRET",
        "VOICEY_OTLP_HEADERS",
    }

    create_count = sum(
        command[0] in {"init", "add"}
        or command[:2] == ("bucket", "create")
        or (command[0] == "domain" and command[1] == "--port")
        for command in runner.commands
    )
    resumed = await manager.deploy(
        _plan(),
        environment=_environment(),
        engine_wheel=_wheel(tmp_path),
    )
    resumed_create_count = sum(
        command[0] in {"init", "add"}
        or command[:2] == ("bucket", "create")
        or (command[0] == "domain" and command[1] == "--port")
        for command in runner.commands
    )
    assert resumed.state.project_id == report.state.project_id
    assert resumed_create_count == create_count


@pytest.mark.asyncio
async def test_railway_missing_callbacks_and_smoke_failures_are_bounded(
    tmp_path: Path,
) -> None:
    missing_runner = FakeRailwayRunner()
    missing = RailwayDeploymentManager(
        tmp_path / "missing",
        runner=missing_runner,
        poll_interval_s=0,
    )
    with pytest.raises(VoiceyError, match="TWILIO_ACCOUNT_SID"):
        await missing.deploy(
            _plan(),
            environment={},
            engine_wheel=_wheel(tmp_path),
            skip_smoke=True,
        )
    assert missing_runner.commands == []

    bad_client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _request: httpx.Response(503))
    )
    bad = RailwayDeploymentManager(
        tmp_path / "bad-status",
        runner=FakeRailwayRunner(),
        http_client=bad_client,
        poll_interval_s=0,
    )
    with pytest.raises(VoiceyError, match="liveness returned HTTP 503"):
        await bad.deploy(
            _plan(project_id=None),
            environment=_environment(),
            engine_wheel=_wheel(tmp_path / "bad-wheel"),
        )
    await bad_client.aclose()

    def disconnect(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline", request=request)

    offline_client = httpx.AsyncClient(transport=httpx.MockTransport(disconnect))
    offline = RailwayDeploymentManager(
        tmp_path / "offline",
        runner=FakeRailwayRunner(),
        http_client=offline_client,
        poll_interval_s=0,
    )
    with pytest.raises(VoiceyError, match="ConnectError"):
        await offline.deploy(
            _plan(),
            environment=_environment(),
            engine_wheel=_wheel(tmp_path / "offline-wheel"),
        )
    await offline_client.aclose()


@pytest.mark.asyncio
async def test_railway_smoke_rejects_incomplete_deployment_identity(
    tmp_path: Path,
) -> None:
    manager = RailwayDeploymentManager(tmp_path, runner=FakeRailwayRunner())
    with pytest.raises(VoiceyError, match="incomplete before smoke"):
        await manager._smoke(
            RailwayResourceState.initial(_plan()),
            RelayCredential.issue("smoke"),
        )
    with pytest.raises(VoiceyError, match="project id is missing"):
        manager._services(RailwayResourceState.initial(_plan()))


@pytest.mark.asyncio
async def test_railway_requires_exact_adoption_and_never_deletes_adopted_resources(
    tmp_path: Path,
) -> None:
    runner = FakeRailwayRunner(existing=True)
    manager = RailwayDeploymentManager(
        tmp_path,
        runner=runner,
        http_client=_http_client(),
        poll_interval_s=0,
    )
    with pytest.raises(VoiceyError, match="ownership"):
        await manager.deploy(
            _plan(),
            environment=_environment(),
            engine_wheel=_wheel(tmp_path),
            adopt=True,
            skip_smoke=True,
        )

    adopted_root = tmp_path / "adopted"
    manager = RailwayDeploymentManager(
        adopted_root,
        runner=runner,
        http_client=_http_client(),
        poll_interval_s=0,
    )
    adopted = await manager.deploy(
        _plan(project_id=runner.project_id),
        environment=_environment(),
        engine_wheel=_wheel(adopted_root),
        adopt=True,
        skip_smoke=True,
    )
    assert not adopted.state.project_created
    assert not adopted.state.service_created
    assert not adopted.state.postgres_created
    assert not adopted.state.bucket_created
    assert not adopted.state.domain_created

    rolled_back = manager.rollback_created(_plan(project_id=runner.project_id))
    assert rolled_back.rolled_back
    assert runner.projects
    assert runner.services
    assert runner.buckets
    assert runner.domains
    assert not any("delete" in command for command in runner.commands)


@pytest.mark.asyncio
async def test_railway_failure_version_and_created_only_rollback(
    tmp_path: Path,
) -> None:
    wrong_version = FakeRailwayRunner(version="railway 6.0.0")
    wrong_manager = RailwayDeploymentManager(
        tmp_path / "wrong",
        runner=wrong_version,
        poll_interval_s=0,
    )
    with pytest.raises(VoiceyError, match="VY-DEP-006"):
        await wrong_manager.deploy(
            _plan(),
            environment=_environment(),
            engine_wheel=_wheel(tmp_path),
            skip_smoke=True,
        )

    runner = FakeRailwayRunner(deployment_status="FAILED")
    manager = RailwayDeploymentManager(
        tmp_path / "failed",
        runner=runner,
        poll_interval_s=0,
    )
    with pytest.raises(VoiceyError, match="FAILED"):
        await manager.deploy(
            _plan(),
            environment=_environment(),
            engine_wheel=_wheel(tmp_path),
            skip_smoke=True,
        )

    runner.deployment_status = "SUCCESS"
    report = await manager.deploy(
        _plan(),
        environment=_environment(),
        engine_wheel=_wheel(tmp_path),
        skip_smoke=True,
    )
    assert report.state.preflight_green
    rolled_back = manager.rollback_created(_plan())
    assert rolled_back.rolled_back
    destructive = [command[:2] for command in runner.commands if "delete" in command]
    assert destructive[-5:] == [
        ("domain", "delete"),
        ("bucket", "delete"),
        ("service", "delete"),
        ("service", "delete"),
        ("delete", "--project"),
    ]
    service_deletes = [
        command for command in runner.commands if command[:2] == ("service", "delete")
    ]
    assert [command.count("--service") for command in service_deletes] == [1, 1]
    assert service_deletes[0][service_deletes[0].index("--service") + 1] == runner.postgres_id
    assert service_deletes[1][service_deletes[1].index("--service") + 1] == runner.service_id


@pytest.mark.asyncio
async def test_railway_rejects_ambiguous_identity_and_nonterminal_timeout(
    tmp_path: Path,
) -> None:
    ambiguous = FakeRailwayRunner(existing=True)
    ambiguous.projects.append({"id": "project_456", "name": "voicey-results"})
    manager = RailwayDeploymentManager(
        tmp_path / "ambiguous",
        runner=ambiguous,
        poll_interval_s=0,
    )
    with pytest.raises(VoiceyError, match="ambiguous"):
        await manager.deploy(
            _plan(project_id=ambiguous.project_id),
            environment=_environment(),
            engine_wheel=_wheel(tmp_path / "ambiguous-wheel"),
            adopt=True,
            skip_smoke=True,
        )

    waiting = FakeRailwayRunner(deployment_status="BUILDING")
    timeout_manager = RailwayDeploymentManager(
        tmp_path / "timeout",
        runner=waiting,
        poll_interval_s=0,
        deployment_timeout_s=0,
    )
    with pytest.raises(VoiceyError, match="timed out"):
        await timeout_manager.deploy(
            _plan(),
            environment=_environment(),
            engine_wheel=_wheel(tmp_path / "timeout-wheel"),
            skip_smoke=True,
        )


def test_railway_resource_store_rejects_public_or_drifted_ledgers(
    tmp_path: Path,
) -> None:
    path = tmp_path / "railway-resources.json"
    store = RailwayResourceStore(path)
    state = RailwayResourceState.initial(_plan())
    store.save(state)
    path.chmod(0o644)
    with pytest.raises(VoiceyError, match="VY-SEC-001"):
        store.load()
    path.chmod(0o600)
    loaded = store.load()
    assert loaded is not None
    with pytest.raises(VoiceyError, match="VY-DEP-007"):
        loaded.validate_plan(
            RailwayPlan(
                project_name="other-project",
                workspace="workspace_123",
                environment="production",
                service_name="voicey-results",
                bucket_name="voicey-results-objects",
                service_region="us-east",
                bucket_region="iad",
            )
        )


def test_railway_rollback_requires_a_ledger(tmp_path: Path) -> None:
    manager = RailwayDeploymentManager(tmp_path, runner=FakeRailwayRunner())
    with pytest.raises(VoiceyError, match="no Railway resource ledger"):
        manager.rollback_created(_plan())
