# pyright: reportPrivateUsage=false

from __future__ import annotations

import json
import stat
import subprocess
from collections.abc import Sequence
from dataclasses import asdict
from pathlib import Path

import httpx
import pytest

from voicey.cli.environment import EnvFileStore
from voicey.deploy.fly import (
    FlyArtifactGenerator,
    FlyCommandResult,
    FlyctlRunner,
    FlyDeploymentManager,
    FlyPlan,
    FlyResourceState,
    FlyResourceStore,
    _check_passing,
    _item_text,
    _json_items,
    _table_contains_name,
)
from voicey.errors import VoiceyError
from voicey.relay.auth import RelayCredential


class FakeFlyRunner:
    def __init__(
        self,
        *,
        app_exists: bool = False,
        postgres_exists: bool = False,
        bucket_exists: bool = False,
        attached: bool = False,
        passing_checks: bool = True,
    ) -> None:
        self.app_exists = app_exists
        self.deployed = False
        self.clusters = [{"id": "mpg_123", "name": "voicey-results-pg"}] if postgres_exists else []
        self.buckets: set[str] = {"voicey-results-objects"} if bucket_exists else set()
        self.secrets: set[str] = (
            {
                "DATABASE_URL",
                "AWS_ACCESS_KEY_ID",
                "AWS_SECRET_ACCESS_KEY",
            }
            if attached
            else set()
        )
        self.passing_checks = passing_checks
        self.commands: list[tuple[str, ...]] = []
        self.secret_payloads: list[str] = []

    def run(
        self,
        arguments: Sequence[str],
        *,
        stdin: str | None = None,
        check: bool = True,
        timeout_s: float = 600,
    ) -> FlyCommandResult:
        del timeout_s
        command = tuple(arguments)
        self.commands.append(command)
        result = self._dispatch(command, stdin)
        if check and result.returncode != 0:
            raise AssertionError(f"unexpected fake fly failure: {command}")
        return result

    def _dispatch(self, command: tuple[str, ...], stdin: str | None) -> FlyCommandResult:
        if command[:2] == ("auth", "whoami"):
            return _result(stdout="developer@example.test\n")
        if command[:2] == ("status", "--app"):
            if not self.app_exists:
                return _result(returncode=1, stderr="app not found")
            return _result(
                stdout=json.dumps(
                    {
                        "Name": "voicey-results",
                        "Status": "running" if self.deployed else "pending",
                    }
                )
            )
        if command[:2] == ("apps", "create"):
            self.app_exists = True
            return _result(stdout='{"name":"voicey-results"}')
        if command[:2] == ("mpg", "list"):
            return _result(stdout=json.dumps({"clusters": self.clusters}))
        if command[:2] == ("mpg", "create"):
            self.clusters.append({"id": "mpg_123", "name": "voicey-results-pg"})
            return _result(stdout="Managed Postgres created")
        if command[:2] == ("mpg", "attach"):
            self.secrets.add("DATABASE_URL")
            return _result(stdout="Attached")
        if command[:2] == ("storage", "list"):
            rows = "\n".join(f"{name} private" for name in sorted(self.buckets))
            return _result(stdout=f"NAME ACCESS\n{rows}\n")
        if command[:2] == ("storage", "create"):
            self.buckets.add("voicey-results-objects")
            self.secrets.update(
                {
                    "AWS_ACCESS_KEY_ID",
                    "AWS_ENDPOINT_URL_S3",
                    "AWS_REGION",
                    "AWS_SECRET_ACCESS_KEY",
                    "BUCKET_NAME",
                }
            )
            return _result(stdout="Tigris bucket created and secrets staged")
        if command[:2] == ("secrets", "list"):
            return _result(stdout=json.dumps([{"name": name} for name in sorted(self.secrets)]))
        if command[:2] == ("secrets", "import"):
            assert stdin is not None
            self.secret_payloads.append(stdin)
            self.secrets.update(
                line.split("=", maxsplit=1)[0] for line in stdin.splitlines() if line
            )
            return _result(stdout="Secrets staged")
        if command[0] == "deploy":
            self.deployed = True
            return _result(stdout="Deployment complete")
        if command[:2] == ("checks", "list"):
            status = "passing" if self.passing_checks else "failing"
            return _result(
                stdout=json.dumps([{"name": "servicecheck-00-http-8080", "status": status}])
            )
        if command[:2] == ("storage", "destroy"):
            self.buckets.discard(command[2])
            return _result(stdout="Bucket destroyed")
        if command[:2] == ("mpg", "destroy"):
            self.clusters.clear()
            return _result(stdout="MPG destroyed")
        if command[:2] == ("apps", "destroy"):
            self.app_exists = False
            return _result(stdout="App destroyed")
        raise AssertionError(f"unhandled fake fly command: {command}")


def _result(
    *,
    returncode: int = 0,
    stdout: str = "",
    stderr: str = "",
) -> FlyCommandResult:
    return FlyCommandResult(returncode=returncode, stdout=stdout, stderr=stderr)


def _plan(*, callbacks: tuple[str, ...] = ("twilio",)) -> FlyPlan:
    return FlyPlan(
        app_name="voicey-results",
        organization="voicey-test",
        region="iad",
        postgres_name="voicey-results-pg",
        bucket_name="voicey-results-objects",
        callback_providers=callbacks,
    )


def _environment() -> dict[str, str]:
    return {
        "TWILIO_ACCOUNT_SID": f"AC{'a' * 32}",
        "TWILIO_AUTH_TOKEN": "test-token",
    }


def _wheel(path: Path) -> Path:
    wheel = path / "voicey-0.0.0.dev0-py3-none-any.whl"
    wheel.write_bytes(b"wheel fixture")
    return wheel


def _http_client(*, ready: bool = True) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/healthz":
            return httpx.Response(200, json={"status": "ok"})
        if request.url.path == "/v1/ready":
            assert request.headers["authorization"].startswith("VoiceyRelay ")
            assert request.headers["x-voicey-relay-nonce"]
            if ready:
                return httpx.Response(
                    200,
                    json={
                        "ready": True,
                        "protocol": "voicey-results-relay/v1",
                        "storage_ready": True,
                    },
                )
            return httpx.Response(503, json={"code": "VY-REL-002"})
        return httpx.Response(404)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_fly_cli_output_normalization_accepts_documented_json_and_table_shapes() -> None:
    assert _json_items([{"name": "one"}, "discarded"]) == [{"name": "one"}]
    assert _json_items({"data": [{"name": "two"}, 3]}) == [{"name": "two"}]
    assert _json_items({"Name": "singleton"}) == [{"Name": "singleton"}]
    assert _json_items({}) == []
    assert _json_items("not-a-collection") == []
    assert _item_text({"NAME": "upper"}, "name") == "upper"
    assert _item_text({"clusterId": "mpg_123"}, "cluster") == "mpg_123"
    assert _item_text({"name": 123}, "name") == ""
    assert _table_contains_name(
        "\x1b[32mNAME ACCESS\x1b[0m\nvoicey-results-objects private\n",
        "voicey-results-objects",
    )
    assert _check_passing({"Status": "HEALTHY"})
    assert not _check_passing({"status": "failing"})


def test_fly_plan_and_artifacts_enforce_managed_topology(tmp_path: Path) -> None:
    wheel = _wheel(tmp_path)
    artifacts = FlyArtifactGenerator(tmp_path).generate(_plan(), engine_wheel=wheel)

    config = artifacts.config.read_text(encoding="utf-8")
    dockerfile = artifacts.dockerfile.read_text(encoding="utf-8")
    ignored = artifacts.dockerignore.read_text(encoding="utf-8")
    assert 'app = "voicey-results"' in config
    assert 'VOICEY_DEPLOY_TARGET = "fly"' in config
    assert 'VOICEY_STORAGE_BACKEND = "postgres"' in config
    assert 'VOICEY_ARTIFACT_BACKEND = "s3"' in config
    assert 'VOICEY_CALLBACK_PROVIDERS = "twilio"' in config
    assert 'VOICEY_PROMETHEUS_ENABLED = "1"' in config
    assert 'VOICEY_PROMETHEUS_BIND = "0.0.0.0"' in config
    assert '[metrics]\n  port = 9464\n  path = "/metrics"' in config
    assert "EXPOSE 8080 9464" in dockerfile
    assert "min_machines_running = 2" in config
    assert 'path = "/healthz"' in config
    assert "VOICEY_RELAY_CREDENTIAL" not in config
    assert "VOICEY_RESULTS_SECRET" not in config
    assert "voicey.deploy.results_service" in dockerfile
    assert "python -m venv --without-pip /opt/voicey" in dockerfile
    assert "python -m pip uninstall --yes pip" in dockerfile
    assert "[companion]" in dockerfile
    assert "[pipecat]" not in dockerfile
    assert "[livekit]" not in dockerfile
    assert wheel.name in ignored
    assert len(artifacts.digest) == 64

    with pytest.raises(VoiceyError, match="VY-DEP-003"):
        FlyPlan(
            app_name="Bad_App",
            organization="voicey-test",
            region="iad",
            postgres_name="voicey-results-pg",
            bucket_name="voicey-results-objects",
        )


def test_flyctl_runner_maps_discovery_process_failure_and_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def missing_executable(_name: str) -> None:
        return None

    monkeypatch.setattr("voicey.deploy.fly.shutil.which", missing_executable)
    with pytest.raises(VoiceyError, match="VY-DEP-006"):
        FlyctlRunner()

    success = subprocess.CompletedProcess(
        args=["fly", "auth", "whoami"],
        returncode=0,
        stdout="operator@example.test\n",
        stderr="",
    )

    def successful_run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return success

    monkeypatch.setattr("voicey.deploy.fly.subprocess.run", successful_run)
    runner = FlyctlRunner("/usr/local/bin/fly")
    assert runner.run(["auth", "whoami"]).stdout == "operator@example.test\n"

    failed = subprocess.CompletedProcess(
        args=["fly", "status"],
        returncode=1,
        stdout="",
        stderr="TWILIO_AUTH_TOKEN=super-secret",
    )

    def failed_run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return failed

    monkeypatch.setattr("voicey.deploy.fly.subprocess.run", failed_run)
    with pytest.raises(VoiceyError, match="VY-DEP-006") as caught:
        runner.run(["status", "--app", "voicey-results"])
    assert "super-secret" not in str(caught.value)
    with pytest.raises(VoiceyError, match="VY-DEP-006") as imported:
        runner.run(["secrets", "import"], stdin="KEY=super-secret\n")
    assert "super-secret" not in str(imported.value)
    assert runner.run(["status"], check=False).returncode == 1

    def timeout(*_args: object, **_kwargs: object) -> object:
        raise subprocess.TimeoutExpired(["fly", "status"], 1)

    monkeypatch.setattr("voicey.deploy.fly.subprocess.run", timeout)
    with pytest.raises(VoiceyError, match="TimeoutExpired"):
        runner.run(["status"])


def test_fly_artifacts_support_published_install_and_reject_bad_wheels(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    generator = FlyArtifactGenerator(tmp_path)
    monkeypatch.setattr("voicey.deploy.fly.__version__", "1.0.0.dev0")
    with pytest.raises(VoiceyError, match=r"requires.*engine-wheel"):
        generator.generate(_plan(callbacks=()), engine_wheel=None)
    bad = tmp_path / "not-voicey.txt"
    bad.write_text("bad", encoding="utf-8")
    with pytest.raises(VoiceyError, match="engine wheel is invalid"):
        generator.generate(_plan(callbacks=()), engine_wheel=bad)

    monkeypatch.setattr("voicey.deploy.fly.__version__", "1.0.0")
    published = generator.generate(_plan(callbacks=()), engine_wheel=None)
    assert published.engine_wheel is None
    assert '"voicey[companion]==1.0.0"' in published.dockerfile.read_text(encoding="utf-8")


def test_fly_resource_state_rejects_invalid_payload_and_plan_drift(
    tmp_path: Path,
) -> None:
    with pytest.raises(VoiceyError, match="not an object"):
        FlyResourceState.from_payload([])
    with pytest.raises(VoiceyError, match="fields are invalid"):
        FlyResourceState.from_payload({"schema_version": "bad"})
    invalid = {
        **asdict(FlyResourceState.initial(_plan(callbacks=()))),
        "schema_version": 99,
    }
    with pytest.raises(VoiceyError, match="version or identity"):
        FlyResourceState.from_payload(invalid)

    state = FlyResourceState.initial(_plan(callbacks=()))
    changed = FlyPlan(
        app_name="other-results",
        organization="voicey-test",
        region="iad",
        postgres_name="voicey-results-pg",
        bucket_name="voicey-results-objects",
    )
    with pytest.raises(VoiceyError, match="does not match"):
        state.validate_plan(changed)
    with pytest.raises(VoiceyError, match="already rolled back"):
        state.checkpoint(rolled_back=True).validate_plan(_plan(callbacks=()))

    path = tmp_path / "invalid.json"
    path.write_text("{bad json", encoding="utf-8")
    path.chmod(0o600)
    with pytest.raises(VoiceyError, match="cannot be read"):
        FlyResourceStore(path).load()


@pytest.mark.asyncio
async def test_fly_deploy_provisions_checkpoints_smokes_and_resumes(
    tmp_path: Path,
) -> None:
    runner = FakeFlyRunner()
    client = _http_client()
    manager = FlyDeploymentManager(tmp_path, runner=runner, http_client=client)
    wheel = _wheel(tmp_path)
    try:
        first = await manager.deploy(
            _plan(),
            environment=_environment(),
            engine_wheel=wheel,
        )
        second = await manager.deploy(
            _plan(),
            environment=_environment(),
            engine_wheel=wheel,
        )
    finally:
        await client.aclose()

    assert first.state.app_created
    assert first.state.postgres_created
    assert first.state.bucket_created
    assert first.state.postgres_attached
    assert first.state.bucket_attached
    assert first.state.deployed
    assert first.state.smoke_green
    assert first.smoke is not None
    assert first.smoke.platform_checks == 1
    assert first.smoke.signed_readiness
    assert second.state.smoke_green
    assert sum(command[:2] == ("apps", "create") for command in runner.commands) == 1
    assert sum(command[:2] == ("mpg", "create") for command in runner.commands) == 1
    assert sum(command[:2] == ("storage", "create") for command in runner.commands) == 1
    assert any(command[:2] == ("mpg", "attach") for command in runner.commands)
    assert all("vkr_" not in " ".join(command) for command in runner.commands)
    assert runner.secret_payloads

    dotenv = tmp_path / ".env"
    assert stat.S_IMODE(dotenv.stat().st_mode) == 0o600
    values = EnvFileStore(dotenv).read()
    relay = RelayCredential.parse(values["VOICEY_RELAY_CREDENTIAL"])
    assert relay.key_id == first.state.relay_key_id
    assert values["VOICEY_RESULTS_SECRET"].startswith("whsec_")
    ledger = (tmp_path / ".voicey/deploy/fly-resources.json").read_text(encoding="utf-8")
    assert values["VOICEY_RELAY_CREDENTIAL"] not in ledger
    assert values["VOICEY_RESULTS_SECRET"] not in ledger
    assert ".env*" in (tmp_path / ".gitignore").read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_fly_adoption_is_explicit_and_never_claims_delete_ownership(
    tmp_path: Path,
) -> None:
    runner = FakeFlyRunner(
        app_exists=True,
        postgres_exists=True,
        bucket_exists=True,
        attached=True,
    )
    manager = FlyDeploymentManager(tmp_path, runner=runner, http_client=_http_client())
    wheel = _wheel(tmp_path)
    with pytest.raises(VoiceyError, match="VY-DEP-007"):
        await manager.deploy(
            _plan(callbacks=()),
            environment={},
            engine_wheel=wheel,
            skip_smoke=True,
        )

    report = await manager.deploy(
        _plan(callbacks=()),
        environment={},
        engine_wheel=wheel,
        adopt=True,
        skip_smoke=True,
    )
    await manager.http_client.aclose()  # type: ignore[union-attr]
    assert not report.state.app_created
    assert not report.state.postgres_created
    assert not report.state.bucket_created
    assert report.state.postgres_attached
    assert report.state.bucket_attached


@pytest.mark.asyncio
async def test_fly_rotation_preserves_previous_credentials_and_updates_fingerprints(
    tmp_path: Path,
) -> None:
    runner = FakeFlyRunner()
    manager = FlyDeploymentManager(tmp_path, runner=runner, http_client=_http_client())
    wheel = _wheel(tmp_path)
    first = await manager.deploy(
        _plan(callbacks=()),
        environment={},
        engine_wheel=wheel,
        skip_smoke=True,
    )
    store = EnvFileStore(tmp_path / ".env")
    before = store.read()
    second = await manager.deploy(
        _plan(callbacks=()),
        environment={},
        engine_wheel=wheel,
        rotate_credentials=True,
        skip_smoke=True,
    )
    await manager.http_client.aclose()  # type: ignore[union-attr]
    after = store.read()

    assert after["VOICEY_RELAY_CREDENTIAL"] != before["VOICEY_RELAY_CREDENTIAL"]
    assert after["VOICEY_RELAY_PREVIOUS_CREDENTIAL"] == before["VOICEY_RELAY_CREDENTIAL"]
    assert after["VOICEY_RESULTS_SECRET"] != before["VOICEY_RESULTS_SECRET"]
    assert after["VOICEY_RESULTS_PREVIOUS_SECRET"] == before["VOICEY_RESULTS_SECRET"]
    assert second.state.relay_fingerprint != first.state.relay_fingerprint
    assert second.state.results_fingerprint != first.state.results_fingerprint
    assert "VOICEY_RELAY_PREVIOUS_CREDENTIAL=" in runner.secret_payloads[-1]
    assert "VOICEY_RESULTS_PREVIOUS_SECRET=" in runner.secret_payloads[-1]


@pytest.mark.asyncio
async def test_fly_credential_drift_and_missing_callbacks_fail_before_mutation(
    tmp_path: Path,
) -> None:
    runner = FakeFlyRunner()
    manager = FlyDeploymentManager(tmp_path, runner=runner, http_client=_http_client())
    wheel = _wheel(tmp_path)
    await manager.deploy(
        _plan(callbacks=()),
        environment={},
        engine_wheel=wheel,
        skip_smoke=True,
    )
    prior_commands = len(runner.commands)
    other = RelayCredential.issue("other-key").reveal()
    with pytest.raises(VoiceyError, match="VY-DEP-007"):
        await manager.deploy(
            _plan(callbacks=()),
            environment={"VOICEY_RELAY_CREDENTIAL": other},
            engine_wheel=wheel,
            skip_smoke=True,
        )
    assert len(runner.commands) == prior_commands

    empty_runner = FakeFlyRunner()
    missing = FlyDeploymentManager(
        tmp_path / "missing",
        runner=empty_runner,
        http_client=_http_client(),
    )
    with pytest.raises(VoiceyError, match="TWILIO_ACCOUNT_SID"):
        await missing.deploy(
            _plan(),
            environment={},
            engine_wheel=wheel,
            skip_smoke=True,
        )
    assert empty_runner.commands == []
    await manager.http_client.aclose()  # type: ignore[union-attr]
    await missing.http_client.aclose()  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_fly_rejects_unattached_bucket_and_failed_platform_check(
    tmp_path: Path,
) -> None:
    unattached_runner = FakeFlyRunner(
        app_exists=True,
        postgres_exists=True,
        bucket_exists=True,
        attached=False,
    )
    unattached = FlyDeploymentManager(
        tmp_path / "unattached",
        runner=unattached_runner,
        http_client=_http_client(),
    )
    with pytest.raises(VoiceyError, match="Tigris bucket is not attached"):
        await unattached.deploy(
            _plan(callbacks=()),
            environment={},
            engine_wheel=_wheel(tmp_path),
            adopt=True,
            skip_smoke=True,
        )

    failing_runner = FakeFlyRunner(passing_checks=False)
    failing = FlyDeploymentManager(
        tmp_path / "failing",
        runner=failing_runner,
        http_client=_http_client(),
    )
    with pytest.raises(VoiceyError, match="VY-DEP-004"):
        await failing.deploy(
            _plan(callbacks=()),
            environment={},
            engine_wheel=_wheel(tmp_path),
        )
    await unattached.http_client.aclose()  # type: ignore[union-attr]
    await failing.http_client.aclose()  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_fly_rollback_deletes_only_created_resources_in_reverse_order(
    tmp_path: Path,
) -> None:
    runner = FakeFlyRunner()
    manager = FlyDeploymentManager(tmp_path, runner=runner, http_client=_http_client())
    await manager.deploy(
        _plan(callbacks=()),
        environment={},
        engine_wheel=_wheel(tmp_path),
        skip_smoke=True,
    )
    state = manager.rollback_created(_plan(callbacks=()))
    await manager.http_client.aclose()  # type: ignore[union-attr]

    destructive = [
        command[:2] for command in runner.commands if len(command) > 1 and command[1] == "destroy"
    ]
    assert destructive == [
        ("storage", "destroy"),
        ("mpg", "destroy"),
        ("apps", "destroy"),
    ]
    assert state.rolled_back
    assert not state.app_created
    assert not state.postgres_created
    assert not state.bucket_created


def test_fly_resource_store_rejects_open_permissions_and_symlinks(
    tmp_path: Path,
) -> None:
    plan = _plan(callbacks=())
    path = tmp_path / "resources.json"
    store = FlyResourceStore(path)
    store.save(FlyResourceState.initial(plan))
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    path.chmod(0o644)
    with pytest.raises(VoiceyError, match="VY-SEC-001"):
        store.load()

    path.unlink()
    target = tmp_path / "target.json"
    target.write_text("{}\n", encoding="utf-8")
    path.symlink_to(target)
    with pytest.raises(VoiceyError, match="VY-SEC-002"):
        store.load()


@pytest.mark.asyncio
async def test_fly_validate_existing_runs_same_signed_smoke(tmp_path: Path) -> None:
    runner = FakeFlyRunner(app_exists=True, passing_checks=True)
    client = _http_client()
    manager = FlyDeploymentManager(tmp_path, runner=runner, http_client=client)
    credential = RelayCredential.issue("existing-key").reveal()
    try:
        report = await manager.validate_existing(
            _plan(callbacks=()),
            relay_credential=credential,
        )
    finally:
        await client.aclose()
    assert report.public_base == "https://voicey-results.fly.dev"
    assert report.liveness
    assert report.signed_readiness


@pytest.mark.asyncio
async def test_fly_smoke_maps_liveness_transport_and_json_failures(
    tmp_path: Path,
) -> None:
    credential = RelayCredential.issue("smoke-key").reveal()
    runner = FakeFlyRunner(app_exists=True)

    bad_status_client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _request: httpx.Response(503))
    )
    bad_status = FlyDeploymentManager(
        tmp_path / "status",
        runner=runner,
        http_client=bad_status_client,
    )
    with pytest.raises(VoiceyError, match="liveness returned HTTP 503"):
        await bad_status.validate_existing(
            _plan(callbacks=()),
            relay_credential=credential,
        )
    await bad_status_client.aclose()

    def disconnect(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline", request=request)

    offline_client = httpx.AsyncClient(transport=httpx.MockTransport(disconnect))
    offline = FlyDeploymentManager(
        tmp_path / "offline",
        runner=runner,
        http_client=offline_client,
    )
    with pytest.raises(VoiceyError, match="ConnectError"):
        await offline.validate_existing(
            _plan(callbacks=()),
            relay_credential=credential,
        )
    await offline_client.aclose()

    class InvalidChecks(FakeFlyRunner):
        def run(
            self,
            arguments: Sequence[str],
            *,
            stdin: str | None = None,
            check: bool = True,
            timeout_s: float = 600,
        ) -> FlyCommandResult:
            if tuple(arguments)[:2] == ("checks", "list"):
                return _result(stdout="{not json")
            return super().run(
                arguments,
                stdin=stdin,
                check=check,
                timeout_s=timeout_s,
            )

    invalid_client = _http_client()
    invalid = FlyDeploymentManager(
        tmp_path / "invalid",
        runner=InvalidChecks(app_exists=True),
        http_client=invalid_client,
    )
    with pytest.raises(VoiceyError, match="not valid JSON"):
        await invalid.validate_existing(
            _plan(callbacks=()),
            relay_credential=credential,
        )
    await invalid_client.aclose()


def test_fly_rollback_requires_ledger_and_store_save_rejects_symlink(
    tmp_path: Path,
) -> None:
    manager = FlyDeploymentManager(tmp_path, runner=FakeFlyRunner())
    with pytest.raises(VoiceyError, match="no Fly resource ledger"):
        manager.rollback_created(_plan(callbacks=()))

    target = tmp_path / "real-ledger.json"
    target.write_text("{}\n", encoding="utf-8")
    link = tmp_path / "linked-ledger.json"
    link.symlink_to(target)
    with pytest.raises(VoiceyError, match="VY-SEC-002"):
        FlyResourceStore(link).save(FlyResourceState.initial(_plan(callbacks=())))
