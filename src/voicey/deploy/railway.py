"""Resumable Railway companion provisioning, deployment, rollback, and smoke."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import time
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol, cast

import httpx
from pydantic import TypeAdapter, ValidationError

from voicey._version import __version__
from voicey.cli.environment import ensure_env_ignored
from voicey.deploy.managed_secrets import (
    ManagedSecretBundle,
    fingerprint,
    prepare_managed_secrets,
    validate_secret_continuity,
)
from voicey.errors import VoiceyError
from voicey.relay.auth import RelayCredential
from voicey.relay.client import RelayClient

_RESOURCE_SCHEMA = 1
_RAILWAY_CLI_MIN = (5, 30, 1)
_RAILWAY_CLI_MAX = (6, 0, 0)
_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}[a-z0-9]$")
_ENVIRONMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.-]{0,62}$")
_SERVICE_REGIONS = frozenset({"us-west", "us-east", "eu-west", "southeast-asia"})
_BUCKET_REGIONS = frozenset({"sjc", "iad", "ams", "sin"})
_CALLBACK_PROVIDERS = frozenset({"twilio", "telnyx", "vobiz", "plivo"})
_SUCCESS_STATUSES = frozenset({"SUCCESS", "HEALTHY", "RUNNING"})
_FAILURE_STATUSES = frozenset({"CRASHED", "FAILED", "REMOVED", "CANCELLED", "CANCELED", "SKIPPED"})


@dataclass(frozen=True, slots=True)
class RailwayPlan:
    """Every identity and placement choice for one Railway results companion."""

    project_name: str
    workspace: str
    environment: str
    service_name: str
    bucket_name: str
    service_region: str
    bucket_region: str
    callback_providers: tuple[str, ...] = ()
    project_id: str | None = None

    def __post_init__(self) -> None:
        if (
            any(
                not _NAME.fullmatch(value)
                for value in (self.project_name, self.service_name, self.bucket_name)
            )
            or not self.workspace.strip()
            or not _ENVIRONMENT.fullmatch(self.environment)
            or self.service_region not in _SERVICE_REGIONS
            or self.bucket_region not in _BUCKET_REGIONS
            or (self.project_id is not None and not self.project_id.strip())
            or any(provider not in _CALLBACK_PROVIDERS for provider in self.callback_providers)
            or len(set(self.callback_providers)) != len(self.callback_providers)
        ):
            raise VoiceyError(
                "VY-DEP-003",
                detail=(
                    "Railway names, workspace, environment, service/bucket region, "
                    "project id, or callback providers are invalid."
                ),
            )


@dataclass(frozen=True, slots=True)
class RailwayArtifacts:
    """Secret-free Railway build context owned by voicey."""

    directory: Path
    dockerfile: Path
    config: Path
    ignore: Path
    engine_wheel: Path | None
    digest: str


@dataclass(frozen=True, slots=True)
class RailwayCommandResult:
    """Captured command result; callers never include secret values in errors."""

    returncode: int
    stdout: str
    stderr: str


class RailwayCommandRunner(Protocol):
    """Injectable Railway CLI boundary."""

    def run(
        self,
        arguments: Sequence[str],
        *,
        stdin: str | None = None,
        check: bool = True,
        timeout_s: float = 600,
    ) -> RailwayCommandResult: ...


class RailwayCliRunner:
    """Run the empirically verified Railway CLI from an isolated link directory."""

    def __init__(
        self,
        work_dir: Path,
        executable: str | None = None,
    ) -> None:
        selected = executable or shutil.which("railway")
        if selected is None:
            raise VoiceyError(
                "VY-DEP-006",
                detail="the `railway` executable is unavailable; install Railway CLI 5.30.x.",
            )
        self.executable = selected
        self.work_dir = work_dir.resolve()

    def run(
        self,
        arguments: Sequence[str],
        *,
        stdin: str | None = None,
        check: bool = True,
        timeout_s: float = 600,
    ) -> RailwayCommandResult:
        try:
            self.work_dir.mkdir(parents=True, exist_ok=True)
            completed = subprocess.run(
                [self.executable, *arguments],
                cwd=self.work_dir,
                check=False,
                capture_output=True,
                text=True,
                input=stdin,
                timeout=timeout_s,
                env={
                    **os.environ,
                    "NO_COLOR": "1",
                    "RAILWAY_TELEMETRY_DISABLED": "1",
                },
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise VoiceyError(
                "VY-DEP-006",
                detail=f"Railway CLI execution failed ({type(exc).__name__}).",
            ) from exc
        result = RailwayCommandResult(
            returncode=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
        )
        if check and result.returncode != 0:
            command = " ".join(arguments[:3])
            raise VoiceyError(
                "VY-DEP-006",
                detail=f"`railway {command}` failed with exit {result.returncode}.",
            )
        return result


@dataclass(frozen=True, slots=True)
class RailwayResourceState:
    """Owner-only, non-secret checkpoint for exact adoption and reverse rollback."""

    schema_version: int
    project_name: str
    workspace: str
    environment: str
    service_name: str
    bucket_name: str
    service_region: str
    bucket_region: str
    project_id: str | None = None
    environment_id: str | None = None
    service_id: str | None = None
    postgres_id: str | None = None
    postgres_name: str | None = None
    bucket_id: str | None = None
    domain_id: str | None = None
    public_base: str | None = None
    project_created: bool = False
    service_created: bool = False
    postgres_created: bool = False
    bucket_created: bool = False
    domain_created: bool = False
    relay_key_id: str | None = None
    relay_fingerprint: str | None = None
    results_fingerprint: str | None = None
    artifact_digest: str | None = None
    deployment_id: str | None = None
    preflight_green: bool = False
    smoke_green: bool = False
    rolled_back: bool = False
    updated_at: str | None = None

    @classmethod
    def initial(cls, plan: RailwayPlan) -> RailwayResourceState:
        return cls(
            schema_version=_RESOURCE_SCHEMA,
            project_name=plan.project_name,
            workspace=plan.workspace,
            environment=plan.environment,
            service_name=plan.service_name,
            bucket_name=plan.bucket_name,
            service_region=plan.service_region,
            bucket_region=plan.bucket_region,
            project_id=plan.project_id,
        )

    @classmethod
    def from_payload(cls, payload: object) -> RailwayResourceState:
        if not isinstance(payload, dict):
            raise VoiceyError(
                "VY-DEP-007",
                detail="Railway resource ledger is not an object.",
            )
        try:
            state = TypeAdapter(cls).validate_python(payload)
        except ValidationError as exc:
            raise VoiceyError(
                "VY-DEP-007",
                detail="Railway resource ledger fields are invalid.",
            ) from exc
        if (
            state.schema_version != _RESOURCE_SCHEMA
            or any(
                not _NAME.fullmatch(value)
                for value in (state.project_name, state.service_name, state.bucket_name)
            )
            or not state.workspace
            or not _ENVIRONMENT.fullmatch(state.environment)
            or state.service_region not in _SERVICE_REGIONS
            or state.bucket_region not in _BUCKET_REGIONS
        ):
            raise VoiceyError(
                "VY-DEP-007",
                detail="Railway resource ledger version or identity is invalid.",
            )
        return state

    def validate_plan(self, plan: RailwayPlan) -> None:
        expected = (
            self.project_name,
            self.workspace,
            self.environment,
            self.service_name,
            self.bucket_name,
            self.service_region,
            self.bucket_region,
        )
        actual = (
            plan.project_name,
            plan.workspace,
            plan.environment,
            plan.service_name,
            plan.bucket_name,
            plan.service_region,
            plan.bucket_region,
        )
        if (
            expected != actual
            or self.rolled_back
            or (plan.project_id is not None and self.project_id != plan.project_id)
        ):
            raise VoiceyError(
                "VY-DEP-007",
                detail="Railway resource ledger does not match this plan or is rolled back.",
            )

    def checkpoint(self, **changes: object) -> RailwayResourceState:
        return replace(self, **changes, updated_at=datetime.now(UTC).isoformat())


class RailwayResourceStore:
    """Atomic 0600 checkpoint that never contains credentials."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> RailwayResourceState | None:
        if not self.path.exists():
            return None
        if self.path.is_symlink():
            raise VoiceyError("VY-SEC-002", detail=str(self.path))
        try:
            if stat.S_IMODE(self.path.stat().st_mode) & 0o077:
                raise VoiceyError(
                    "VY-SEC-001",
                    detail=f"{self.path} must be owner-only (0600).",
                )
            return RailwayResourceState.from_payload(
                json.loads(self.path.read_text(encoding="utf-8"))
            )
        except VoiceyError:
            raise
        except (OSError, json.JSONDecodeError) as exc:
            raise VoiceyError(
                "VY-DEP-007",
                detail="Railway resource ledger cannot be read.",
            ) from exc

    def save(self, state: RailwayResourceState) -> None:
        payload = json.dumps(asdict(state), indent=2, sort_keys=True) + "\n"
        _atomic_write(self.path, payload.encode(), mode=0o600)


@dataclass(frozen=True, slots=True)
class RailwaySmokeReport:
    """Platform deployment, migration/preflight, and signed readiness evidence."""

    project_id: str
    service_id: str
    deployment_id: str
    public_base: str
    deployment_status: str
    liveness: bool
    signed_readiness: bool
    migration_preflight: bool
    rolling_generation_preflight: bool


@dataclass(frozen=True, slots=True)
class RailwayDeploymentReport:
    """Non-secret result from one resumable Railway operation."""

    state: RailwayResourceState
    artifacts: RailwayArtifacts
    smoke: RailwaySmokeReport | None


class RailwayArtifactGenerator:
    """Generate a companion-only image and current Railway config-as-code."""

    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root.resolve()
        self.directory = self.project_root / ".voicey" / "deploy" / "railway"

    def generate(self, *, engine_wheel: Path | None) -> RailwayArtifacts:
        wheel = self._copy_wheel(engine_wheel)
        dockerfile = self.directory / "Dockerfile.results"
        config = self.directory / "railway.json"
        ignore = self.directory / ".railwayignore"
        rendered = {
            dockerfile: _dockerfile(wheel),
            config: _railway_config(),
            ignore: _railwayignore(wheel),
        }
        for path, payload in rendered.items():
            _atomic_write(path, payload.encode(), mode=0o644)
        try:
            parsed = json.loads(config.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise VoiceyError(
                "VY-DEP-003",
                detail="generated Railway JSON is invalid.",
            ) from exc
        deploy = parsed.get("deploy", {})
        build = parsed.get("build", {})
        if (
            build.get("builder") != "DOCKERFILE"
            or build.get("dockerfilePath") != "Dockerfile.results"
            or deploy.get("healthcheckPath") != "/healthz"
            or "--preflight-only" not in str(deploy.get("preDeployCommand", ""))
            or deploy.get("numReplicas") != 2
        ):
            raise VoiceyError(
                "VY-DEP-003",
                detail="generated Railway artifact failed its topology invariant.",
            )
        digest = hashlib.sha256(b"\0".join(path.read_bytes() for path in rendered)).hexdigest()
        return RailwayArtifacts(
            directory=self.directory,
            dockerfile=dockerfile,
            config=config,
            ignore=ignore,
            engine_wheel=wheel,
            digest=digest,
        )

    def _copy_wheel(self, engine_wheel: Path | None) -> Path | None:
        if engine_wheel is None:
            if __version__.endswith(".dev0"):
                raise VoiceyError(
                    "VY-DEP-003",
                    detail=(
                        "this unpublished development build requires "
                        "`--engine-wheel /absolute/path/to/voicey-*.whl`."
                    ),
                )
            return None
        source = engine_wheel.expanduser().resolve()
        if not source.is_file() or source.suffix != ".whl" or not source.name.startswith("voicey-"):
            raise VoiceyError("VY-DEP-003", detail="the engine wheel is invalid.")
        destination = self.directory / source.name
        if source != destination:
            self.directory.mkdir(parents=True, exist_ok=True)
            temporary = destination.with_suffix(".whl.tmp")
            try:
                shutil.copyfile(source, temporary)
                os.replace(temporary, destination)
            except OSError as exc:
                temporary.unlink(missing_ok=True)
                raise VoiceyError(
                    "VY-DEP-001",
                    detail=f"could not stage engine wheel at {destination}.",
                ) from exc
        return destination


class RailwayDeploymentManager:
    """Provision exact resources and checkpoint after every external mutation."""

    def __init__(
        self,
        project_root: Path,
        *,
        runner: RailwayCommandRunner | None = None,
        http_client: httpx.AsyncClient | None = None,
        poll_interval_s: float = 2,
        deployment_timeout_s: float = 1200,
    ) -> None:
        self.project_root = project_root.resolve()
        self.artifacts = RailwayArtifactGenerator(self.project_root)
        self.runner = runner or RailwayCliRunner(self.artifacts.directory)
        self.http_client = http_client
        self.poll_interval_s = poll_interval_s
        self.deployment_timeout_s = deployment_timeout_s
        self.store = RailwayResourceStore(
            self.project_root / ".voicey" / "deploy" / "railway-resources.json"
        )

    async def deploy(
        self,
        plan: RailwayPlan,
        *,
        environment: Mapping[str, str],
        engine_wheel: Path | None = None,
        adopt: bool = False,
        rotate_credentials: bool = False,
        skip_smoke: bool = False,
    ) -> RailwayDeploymentReport:
        """Run the resumable project→service→storage→release→smoke sequence."""
        ensure_env_ignored(self.project_root)
        loaded = self.store.load()
        state = (
            RailwayResourceState.initial(plan) if loaded is None or loaded.rolled_back else loaded
        )
        state.validate_plan(plan)
        artifacts = self.artifacts.generate(engine_wheel=engine_wheel)
        bundle = prepare_managed_secrets(
            self.project_root,
            environment,
            plan.callback_providers,
            rotate=rotate_credentials,
            expected_relay_fingerprint=state.relay_fingerprint,
            expected_results_fingerprint=state.results_fingerprint,
        )
        validate_secret_continuity(state, bundle, rotate=rotate_credentials)
        state = state.checkpoint(artifact_digest=artifacts.digest)
        self.store.save(state)

        await asyncio.to_thread(self._authenticate)
        state = await asyncio.to_thread(self._ensure_project, plan, state, adopt)
        state = await asyncio.to_thread(self._ensure_service, plan, state, adopt)
        state = await asyncio.to_thread(self._ensure_postgres, plan, state, adopt)
        state = await asyncio.to_thread(self._ensure_bucket, plan, state, adopt)
        state = await asyncio.to_thread(self._ensure_domain, plan, state, adopt)
        state = await asyncio.to_thread(
            self._sync_variables,
            plan,
            state,
            bundle,
            environment,
        )
        state = await asyncio.to_thread(self._deploy_release, plan, state, artifacts)
        smoke = None
        if not skip_smoke:
            smoke = await self._smoke(state, bundle.relay)
            state = state.checkpoint(smoke_green=True)
            self.store.save(state)
        return RailwayDeploymentReport(state=state, artifacts=artifacts, smoke=smoke)

    def rollback_created(self, plan: RailwayPlan) -> RailwayResourceState:
        """Delete only identities explicitly marked created by voicey."""
        state = self.store.load()
        if state is None:
            raise VoiceyError("VY-DEP-007", detail="no Railway resource ledger exists.")
        state.validate_plan(plan)
        self._authenticate()
        self._link(state)
        common = self._context_args(state)
        if state.domain_created and state.public_base is not None:
            self.runner.run(
                [
                    "domain",
                    "delete",
                    state.domain_id or state.public_base,
                    *common,
                    "--yes",
                    "--json",
                ]
            )
            state = state.checkpoint(
                domain_created=False,
                domain_id=None,
                public_base=None,
            )
            self.store.save(state)
        if state.bucket_created and state.bucket_id is not None:
            self.runner.run(
                [
                    "bucket",
                    "delete",
                    "--bucket",
                    state.bucket_id,
                    "--environment",
                    state.environment_id or state.environment,
                    "--yes",
                    "--json",
                ]
            )
            state = state.checkpoint(bucket_created=False, bucket_id=None)
            self.store.save(state)
        if state.postgres_created and state.postgres_id is not None:
            self.runner.run(
                [
                    "service",
                    "delete",
                    "--service",
                    state.postgres_id,
                    *common,
                    "--yes",
                    "--json",
                ]
            )
            state = state.checkpoint(
                postgres_created=False,
                postgres_id=None,
                postgres_name=None,
            )
            self.store.save(state)
        if state.service_created and state.service_id is not None:
            self.runner.run(
                [
                    "service",
                    "delete",
                    "--service",
                    state.service_id,
                    *common,
                    "--yes",
                    "--json",
                ]
            )
            state = state.checkpoint(
                service_created=False,
                service_id=None,
                deployment_id=None,
                preflight_green=False,
                smoke_green=False,
            )
            self.store.save(state)
        if state.project_created and state.project_id is not None:
            self.runner.run(
                [
                    "delete",
                    "--project",
                    state.project_id,
                    "--yes",
                    "--json",
                ]
            )
            state = state.checkpoint(
                project_created=False,
                project_id=None,
                environment_id=None,
            )
            self.store.save(state)
        state = state.checkpoint(rolled_back=True)
        self.store.save(state)
        return state

    def _authenticate(self) -> None:
        version = self.runner.run(["--version"], timeout_s=30).stdout
        match = re.search(r"(\d+)\.(\d+)\.(\d+)", version)
        if match is None:
            raise VoiceyError(
                "VY-DEP-006",
                detail="Railway CLI did not report a semantic version.",
            )
        parsed = tuple(int(part) for part in match.groups())
        if not _RAILWAY_CLI_MIN <= parsed < _RAILWAY_CLI_MAX:
            raise VoiceyError(
                "VY-DEP-006",
                detail="Railway CLI must be >=5.30.1,<6 for this command surface.",
            )
        self.runner.run(["whoami"], timeout_s=30)

    def _ensure_project(
        self,
        plan: RailwayPlan,
        state: RailwayResourceState,
        adopt: bool,
    ) -> RailwayResourceState:
        projects = self._projects()
        by_id = {_item_text(item, "id"): item for item in projects if _item_text(item, "id")}
        matches = [
            item
            for item in projects
            if _item_text(item, "name") == plan.project_name
            or (state.project_id is not None and _item_text(item, "id") == state.project_id)
        ]
        if len({_item_text(item, "id") for item in matches}) > 1:
            raise VoiceyError("VY-DEP-007", detail="Railway project identity is ambiguous.")
        if state.project_id is not None:
            if state.project_id not in by_id:
                raise VoiceyError(
                    "VY-DEP-007",
                    detail="ledgered Railway project is missing.",
                )
            project = by_id[state.project_id]
            if _item_text(project, "name") != plan.project_name:
                raise VoiceyError("VY-DEP-007", detail="Railway project name drifted.")
            linked = self._link(state)
            return self._checkpoint_link(state, linked)
        if matches:
            project_id = _item_text(matches[0], "id")
            if not adopt or plan.project_id != project_id:
                raise VoiceyError(
                    "VY-DEP-007",
                    detail=(
                        f"Railway project {plan.project_name} exists without ownership "
                        "evidence; pass its exact --project-id with --adopt."
                    ),
                )
            state = state.checkpoint(project_id=project_id)
            linked = self._link(state)
            checkpoint = self._checkpoint_link(state, linked)
            self.store.save(checkpoint)
            return checkpoint

        created = self.runner.run(
            [
                "init",
                "--name",
                plan.project_name,
                "--workspace",
                plan.workspace,
                "--json",
            ],
            timeout_s=120,
        )
        payload = _parse_json(created.stdout, label="Railway project create")
        project_id = _find_text(payload, "projectId", "project_id", "id")
        if not project_id:
            fresh = [
                item for item in self._projects() if _item_text(item, "name") == plan.project_name
            ]
            if len(fresh) != 1:
                raise VoiceyError(
                    "VY-DEP-007",
                    detail="created Railway project could not be resolved uniquely.",
                )
            project_id = _item_text(fresh[0], "id")
        checkpoint = state.checkpoint(
            project_id=project_id,
            project_created=True,
        )
        self.store.save(checkpoint)
        linked = self._link(checkpoint)
        checkpoint = self._checkpoint_link(checkpoint, linked)
        self.store.save(checkpoint)
        return checkpoint

    def _ensure_service(
        self,
        plan: RailwayPlan,
        state: RailwayResourceState,
        adopt: bool,
    ) -> RailwayResourceState:
        services = self._services(state)
        matches = _identity_matches(services, plan.service_name, state.service_id)
        if len(matches) > 1:
            raise VoiceyError("VY-DEP-007", detail="Railway service identity is ambiguous.")
        if matches:
            service_id = _item_text(matches[0], "id")
            if state.service_id is None and not adopt:
                raise VoiceyError(
                    "VY-DEP-007",
                    detail=(
                        f"Railway service {plan.service_name} exists without ownership "
                        "evidence; rerun with --adopt after verification."
                    ),
                )
            if state.service_id not in {None, service_id}:
                raise VoiceyError("VY-DEP-007", detail="Railway service id drifted.")
            checkpoint = state.checkpoint(service_id=service_id)
            self.store.save(checkpoint)
            return checkpoint
        if state.service_id is not None:
            raise VoiceyError("VY-DEP-007", detail="ledgered Railway service is missing.")
        result = self.runner.run(
            ["add", "--service", plan.service_name, "--json"],
            timeout_s=120,
        )
        service_id = _find_text(
            _parse_json(result.stdout, label="Railway service create"),
            "serviceId",
            "service_id",
            "id",
        )
        if not service_id:
            created = _identity_matches(self._services(state), plan.service_name, None)
            if len(created) != 1:
                raise VoiceyError(
                    "VY-DEP-007",
                    detail="created Railway service could not be resolved uniquely.",
                )
            service_id = _item_text(created[0], "id")
        checkpoint = state.checkpoint(service_id=service_id, service_created=True)
        self.store.save(checkpoint)
        return checkpoint

    def _ensure_postgres(
        self,
        _plan: RailwayPlan,
        state: RailwayResourceState,
        adopt: bool,
    ) -> RailwayResourceState:
        services = self._services(state)
        matches = (
            []
            if state.postgres_name is None and state.postgres_id is None
            else _identity_matches(
                services,
                state.postgres_name or "",
                state.postgres_id,
            )
        )
        if len(matches) > 1:
            raise VoiceyError("VY-DEP-007", detail="Railway Postgres identity is ambiguous.")
        if matches:
            postgres_id = _item_text(matches[0], "id")
            postgres_name = _item_text(matches[0], "name")
            if state.postgres_id not in {None, postgres_id}:
                raise VoiceyError("VY-DEP-007", detail="Railway Postgres id drifted.")
            checkpoint = state.checkpoint(
                postgres_id=postgres_id,
                postgres_name=postgres_name,
            )
            self.store.save(checkpoint)
            return checkpoint
        if state.postgres_id is not None:
            raise VoiceyError("VY-DEP-007", detail="ledgered Railway Postgres is missing.")

        unowned = [item for item in services if _item_text(item, "name") == "Postgres"]
        if unowned:
            if not adopt or len(unowned) != 1:
                raise VoiceyError(
                    "VY-DEP-007",
                    detail=(
                        "an unledgered Railway Postgres service exists; rerun with "
                        "--adopt only after verifying it."
                    ),
                )
            checkpoint = state.checkpoint(
                postgres_id=_item_text(unowned[0], "id"),
                postgres_name=_item_text(unowned[0], "name"),
            )
            self.store.save(checkpoint)
            return checkpoint

        before = {_item_text(item, "id") for item in services}
        result = self.runner.run(["add", "--database", "postgres", "--json"], timeout_s=600)
        payload = _parse_json(result.stdout, label="Railway Postgres create")
        postgres_id = _find_text(payload, "serviceId", "service_id", "id")
        postgres_name = _find_text(payload, "serviceName", "service_name", "name")
        if not postgres_id:
            created = [
                item for item in self._services(state) if _item_text(item, "id") not in before
            ]
            if len(created) != 1:
                raise VoiceyError(
                    "VY-DEP-007",
                    detail="created Railway Postgres could not be resolved uniquely.",
                )
            postgres_id = _item_text(created[0], "id")
            postgres_name = _item_text(created[0], "name")
        if not postgres_name or not _safe_reference_namespace(postgres_name):
            raise VoiceyError(
                "VY-DEP-007",
                detail="Railway Postgres omitted a safe variable-reference namespace.",
            )
        checkpoint = state.checkpoint(
            postgres_id=postgres_id,
            postgres_name=postgres_name,
            postgres_created=True,
        )
        self.store.save(checkpoint)
        return checkpoint

    def _ensure_bucket(
        self,
        plan: RailwayPlan,
        state: RailwayResourceState,
        adopt: bool,
    ) -> RailwayResourceState:
        buckets = self._buckets(state)
        matches = _identity_matches(buckets, plan.bucket_name, state.bucket_id)
        if len(matches) > 1:
            raise VoiceyError("VY-DEP-007", detail="Railway bucket identity is ambiguous.")
        if matches:
            bucket_id = _item_text(matches[0], "id")
            if state.bucket_id is None and not adopt:
                raise VoiceyError(
                    "VY-DEP-007",
                    detail=(
                        f"Railway bucket {plan.bucket_name} exists without ownership "
                        "evidence; rerun with --adopt after verification."
                    ),
                )
            if state.bucket_id not in {None, bucket_id}:
                raise VoiceyError("VY-DEP-007", detail="Railway bucket id drifted.")
            checkpoint = state.checkpoint(bucket_id=bucket_id)
            self.store.save(checkpoint)
            return checkpoint
        if state.bucket_id is not None:
            raise VoiceyError("VY-DEP-007", detail="ledgered Railway bucket is missing.")
        result = self.runner.run(
            [
                "bucket",
                "create",
                plan.bucket_name,
                "--region",
                plan.bucket_region,
                "--environment",
                state.environment_id or state.environment,
                "--json",
            ],
            timeout_s=600,
        )
        bucket_id = _find_text(
            _parse_json(result.stdout, label="Railway bucket create"),
            "bucketId",
            "bucket_id",
            "id",
        )
        if not bucket_id:
            created = _identity_matches(self._buckets(state), plan.bucket_name, None)
            if len(created) != 1:
                raise VoiceyError(
                    "VY-DEP-007",
                    detail="created Railway bucket could not be resolved uniquely.",
                )
            bucket_id = _item_text(created[0], "id")
        checkpoint = state.checkpoint(bucket_id=bucket_id, bucket_created=True)
        self.store.save(checkpoint)
        return checkpoint

    def _ensure_domain(
        self,
        _plan: RailwayPlan,
        state: RailwayResourceState,
        adopt: bool,
    ) -> RailwayResourceState:
        domains = self._domains(state)
        matches = [
            item
            for item in domains
            if (state.domain_id is not None and _item_text(item, "id") == state.domain_id)
            or (
                state.public_base is not None
                and _domain_text(item) == state.public_base.removeprefix("https://")
            )
        ]
        if len(matches) > 1:
            raise VoiceyError("VY-DEP-007", detail="Railway domain identity is ambiguous.")
        if matches:
            domain = _domain_text(matches[0])
            if not domain:
                raise VoiceyError("VY-DEP-007", detail="Railway domain is malformed.")
            checkpoint = state.checkpoint(
                domain_id=_item_text(matches[0], "id") or state.domain_id,
                public_base=f"https://{domain}",
            )
            self.store.save(checkpoint)
            return checkpoint
        if state.domain_id is not None or state.public_base is not None:
            raise VoiceyError("VY-DEP-007", detail="ledgered Railway domain is missing.")
        if domains:
            railway_domains = [
                item for item in domains if _domain_text(item).endswith(".up.railway.app")
            ]
            if not adopt or len(railway_domains) != 1:
                raise VoiceyError(
                    "VY-DEP-007",
                    detail=(
                        "unledgered Railway domains exist; rerun with --adopt only when "
                        "there is one verified Railway service domain."
                    ),
                )
            domain = _domain_text(railway_domains[0])
            checkpoint = state.checkpoint(
                domain_id=_item_text(railway_domains[0], "id") or None,
                public_base=f"https://{domain}",
            )
            self.store.save(checkpoint)
            return checkpoint

        result = self.runner.run(
            [
                "domain",
                "--port",
                "8080",
                *self._context_args(state),
                "--json",
            ],
            timeout_s=120,
        )
        payload = _parse_json(result.stdout, label="Railway domain create")
        domain = _find_text(payload, "domain", "hostname", "url").removeprefix("https://")
        domain_id = _find_text(payload, "domainId", "domain_id", "id")
        if not domain:
            created = self._domains(state)
            railway_domains = [
                item for item in created if _domain_text(item).endswith(".up.railway.app")
            ]
            if len(railway_domains) != 1:
                raise VoiceyError(
                    "VY-DEP-007",
                    detail="created Railway domain could not be resolved uniquely.",
                )
            domain = _domain_text(railway_domains[0])
            domain_id = _item_text(railway_domains[0], "id")
        checkpoint = state.checkpoint(
            domain_id=domain_id or None,
            public_base=f"https://{domain}",
            domain_created=True,
        )
        self.store.save(checkpoint)
        return checkpoint

    def _sync_variables(
        self,
        plan: RailwayPlan,
        state: RailwayResourceState,
        bundle: ManagedSecretBundle,
        environment: Mapping[str, str],
    ) -> RailwayResourceState:
        if (
            state.postgres_name is None
            or state.bucket_id is None
            or state.public_base is None
            or state.service_id is None
        ):
            raise VoiceyError(
                "VY-DEP-007",
                detail="Railway resources are incomplete before variable sync.",
            )
        postgres = state.postgres_name
        bucket = plan.bucket_name
        callbacks = ",".join(plan.callback_providers)
        references = {
            "DATABASE_URL": _variable_reference(postgres, "DATABASE_URL"),
            "VOICEY_OBJECT_BUCKET": _variable_reference(bucket, "BUCKET"),
            "VOICEY_OBJECT_ENDPOINT": _variable_reference(bucket, "ENDPOINT"),
            "AWS_REGION": _variable_reference(bucket, "REGION"),
            "AWS_ACCESS_KEY_ID": _variable_reference(bucket, "ACCESS_KEY_ID"),
            "AWS_SECRET_ACCESS_KEY": _variable_reference(
                bucket,
                "SECRET_ACCESS_KEY",
            ),
        }
        values = {
            **references,
            "VOICEY_PUBLIC_BASE": state.public_base,
            "VOICEY_OBJECT_PREFIX": "voicey",
            "VOICEY_DEPLOY_TARGET": "railway",
            "VOICEY_STORAGE_BACKEND": "postgres",
            "VOICEY_ARTIFACT_BACKEND": "s3",
            "VOICEY_CALLBACK_PROVIDERS": callbacks,
            "VOICEY_DB_POOL_MIN": "1",
            "VOICEY_DB_POOL_MAX": "5",
            "VOICEY_DB_CONNECTION_BUDGET": "20",
            "VOICEY_DRAIN_GRACE_S": "20",
            "VOICEY_PROMETHEUS_ENABLED": "1",
            "VOICEY_PROMETHEUS_BIND": "0.0.0.0",
            "VOICEY_PROMETHEUS_PORT": "9464",
            "VOICEY_PROMETHEUS_PATH": "/metrics",
            "RAILWAY_DEPLOYMENT_OVERLAP_SECONDS": "30",
            "PORT": "8080",
        }
        otlp_endpoint = environment.get("VOICEY_OTLP_ENDPOINT", "").strip()
        if otlp_endpoint:
            values["VOICEY_OTLP_ENDPOINT"] = otlp_endpoint
        self.runner.run(
            [
                "variable",
                "set",
                *(f"{name}={value}" for name, value in sorted(values.items())),
                *self._context_args(state),
                "--skip-deploys",
                "--json",
            ],
            timeout_s=120,
        )
        secret_values = bundle.platform_values()
        otlp_headers = environment.get("VOICEY_OTLP_HEADERS", "").strip()
        if otlp_headers:
            secret_values["VOICEY_OTLP_HEADERS"] = otlp_headers
            self.runner.run(
                [
                    "variable",
                    "set",
                    "VOICEY_OTLP_HEADERS_ENV=VOICEY_OTLP_HEADERS",
                    *self._context_args(state),
                    "--skip-deploys",
                    "--json",
                ],
                timeout_s=120,
            )
        for name, value in sorted(secret_values.items()):
            self.runner.run(
                [
                    "variable",
                    "set",
                    name,
                    *self._context_args(state),
                    "--stdin",
                    "--skip-deploys",
                    "--json",
                ],
                stdin=value,
                timeout_s=120,
            )
        checkpoint = state.checkpoint(
            relay_key_id=bundle.relay.key_id,
            relay_fingerprint=fingerprint(bundle.relay_current),
            results_fingerprint=fingerprint(bundle.results_current),
        )
        self.store.save(checkpoint)
        return checkpoint

    def _deploy_release(
        self,
        plan: RailwayPlan,
        state: RailwayResourceState,
        artifacts: RailwayArtifacts,
    ) -> RailwayResourceState:
        self.runner.run(
            [
                "up",
                str(artifacts.directory),
                "--path-as-root",
                *self._context_args(state),
                "--detach",
                "--yes",
                "--json",
                "--message",
                f"voicey:{artifacts.digest}",
            ],
            timeout_s=1800,
        )
        deployment = self._wait_for_deployment(state)
        deployment_id = _item_text(deployment, "id")
        status = _item_text(deployment, "status").upper()
        if not deployment_id or status not in _SUCCESS_STATUSES:
            raise VoiceyError(
                "VY-DEP-004",
                detail="Railway deployment did not reach a healthy terminal status.",
            )
        self.runner.run(
            [
                "scale",
                *self._context_args(state),
                "--json",
                f"{plan.service_region}=2",
            ],
            timeout_s=120,
        )
        service_status = self.runner.run(
            ["service", "status", *self._context_args(state), "--json"],
            timeout_s=60,
        )
        _parse_json(service_status.stdout, label="Railway service status")
        checkpoint = state.checkpoint(
            deployment_id=deployment_id,
            preflight_green=True,
            smoke_green=False,
        )
        self.store.save(checkpoint)
        return checkpoint

    def _wait_for_deployment(
        self,
        state: RailwayResourceState,
    ) -> dict[str, object]:
        deadline = time.monotonic() + self.deployment_timeout_s
        while True:
            result = self.runner.run(
                [
                    "deployment",
                    "list",
                    *self._context_args(state),
                    "--limit",
                    "5",
                    "--json",
                ],
                timeout_s=60,
            )
            deployments = _json_items(_parse_json(result.stdout, label="Railway deployment list"))
            if deployments:
                latest = deployments[0]
                status = _item_text(latest, "status").upper()
                if status in _SUCCESS_STATUSES:
                    return latest
                if status in _FAILURE_STATUSES:
                    raise VoiceyError(
                        "VY-DEP-004",
                        detail=f"Railway deployment ended with status {status}.",
                    )
            if time.monotonic() >= deadline:
                raise VoiceyError(
                    "VY-DEP-004",
                    detail="Railway deployment readiness timed out.",
                )
            time.sleep(self.poll_interval_s)

    async def _smoke(
        self,
        state: RailwayResourceState,
        relay_credential: RelayCredential,
    ) -> RailwaySmokeReport:
        if (
            state.project_id is None
            or state.service_id is None
            or state.deployment_id is None
            or state.public_base is None
            or not state.preflight_green
        ):
            raise VoiceyError(
                "VY-DEP-007",
                detail="Railway deployment identity is incomplete before smoke.",
            )
        owns_client = self.http_client is None
        client = self.http_client or httpx.AsyncClient(timeout=10)
        try:
            response = await client.get(f"{state.public_base}/healthz")
            if response.status_code != 200:
                raise VoiceyError(
                    "VY-DEP-004",
                    detail=f"Railway liveness returned HTTP {response.status_code}.",
                )
            async with RelayClient(
                state.public_base,
                relay_credential,
                client=client,
                max_attempts=3,
            ):
                pass
        except httpx.HTTPError as exc:
            raise VoiceyError(
                "VY-DEP-004",
                detail=f"Railway endpoint smoke failed ({type(exc).__name__}).",
            ) from exc
        finally:
            if owns_client:
                await client.aclose()
        return RailwaySmokeReport(
            project_id=state.project_id,
            service_id=state.service_id,
            deployment_id=state.deployment_id,
            public_base=state.public_base,
            deployment_status="SUCCESS",
            liveness=True,
            signed_readiness=True,
            migration_preflight=True,
            rolling_generation_preflight=True,
        )

    def _projects(self) -> list[dict[str, object]]:
        result = self.runner.run(["list", "--json"], timeout_s=60)
        return _json_items(_parse_json(result.stdout, label="Railway project list"))

    def _services(self, state: RailwayResourceState) -> list[dict[str, object]]:
        if state.project_id is None:
            raise VoiceyError("VY-DEP-007", detail="Railway project id is missing.")
        result = self.runner.run(
            [
                "service",
                "list",
                "--project",
                state.project_id,
                "--environment",
                state.environment_id or state.environment,
                "--json",
            ],
            timeout_s=60,
        )
        return _json_items(_parse_json(result.stdout, label="Railway service list"))

    def _buckets(self, state: RailwayResourceState) -> list[dict[str, object]]:
        result = self.runner.run(
            [
                "bucket",
                "list",
                "--environment",
                state.environment_id or state.environment,
                "--json",
            ],
            timeout_s=60,
        )
        return _json_items(_parse_json(result.stdout, label="Railway bucket list"))

    def _domains(self, state: RailwayResourceState) -> list[dict[str, object]]:
        result = self.runner.run(
            ["domain", "list", *self._context_args(state), "--json"],
            timeout_s=60,
        )
        return _json_items(_parse_json(result.stdout, label="Railway domain list"))

    def _link(self, state: RailwayResourceState) -> object:
        if state.project_id is None:
            raise VoiceyError("VY-DEP-007", detail="Railway project id is missing.")
        result = self.runner.run(
            [
                "link",
                "--project",
                state.project_id,
                "--environment",
                state.environment,
                "--json",
            ],
            timeout_s=60,
        )
        return _parse_json(result.stdout, label="Railway project link")

    def _checkpoint_link(
        self,
        state: RailwayResourceState,
        payload: object,
    ) -> RailwayResourceState:
        project_id = _find_text(payload, "projectId", "project_id")
        environment_id = _find_text(payload, "environmentId", "environment_id")
        if project_id and state.project_id != project_id:
            raise VoiceyError("VY-DEP-007", detail="Railway link returned another project.")
        if not environment_id:
            raise VoiceyError(
                "VY-DEP-007",
                detail="Railway link omitted the environment id.",
            )
        return state.checkpoint(environment_id=environment_id)

    def _context_args(self, state: RailwayResourceState) -> list[str]:
        if state.project_id is None or state.service_id is None:
            raise VoiceyError(
                "VY-DEP-007",
                detail="Railway project/service context is incomplete.",
            )
        return [
            "--project",
            state.project_id,
            "--environment",
            state.environment_id or state.environment,
            "--service",
            state.service_id,
        ]


def _dockerfile(wheel: Path | None) -> str:
    package = f"voicey[companion]=={__version__}"
    install = (
        f'uv pip install --python /opt/voicey/bin/python "/tmp/{wheel.name}[companion]"'
        if wheel is not None
        else f'uv pip install --python /opt/voicey/bin/python "{package}"'
    )
    copy = f"COPY {wheel.name} /tmp/{wheel.name}\n" if wheel else ""
    return f"""# syntax=docker/dockerfile:1.7
# Generated by voicey {__version__}. Secrets are supplied by Railway at runtime.
FROM python:3.14-slim-bookworm AS build
RUN python -m pip install --no-cache-dir uv==0.11.7 \\
    && python -m venv /opt/voicey
{copy}RUN {install}

FROM python:3.14-slim-bookworm AS runtime
RUN apt-get update \\
    && apt-get install --no-install-recommends -y ca-certificates \\
    && rm -rf /var/lib/apt/lists/* \\
    && groupadd --system --gid 10001 voicey \\
    && useradd --system --uid 10001 --gid 10001 --home-dir /app \\
         --shell /usr/sbin/nologin voicey
WORKDIR /app
COPY --from=build /opt/voicey /opt/voicey
ENV PATH="/opt/voicey/bin:$PATH" \\
    PYTHONUNBUFFERED="1" \\
    PYTHONDONTWRITEBYTECODE="1"
USER 10001:10001
EXPOSE 8080
CMD ["python", "-m", "voicey.deploy.results_service"]
"""


def _railway_config() -> str:
    payload = {
        "$schema": "https://railway.com/railway.schema.json",
        "build": {
            "builder": "DOCKERFILE",
            "dockerfilePath": "Dockerfile.results",
        },
        "deploy": {
            "preDeployCommand": ("python -m voicey.deploy.results_service --preflight-only"),
            "startCommand": "python -m voicey.deploy.results_service",
            "healthcheckPath": "/healthz",
            "healthcheckTimeout": 300,
            "restartPolicyType": "ON_FAILURE",
            "restartPolicyMaxRetries": 10,
            "numReplicas": 2,
        },
    }
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


def _railwayignore(wheel: Path | None) -> str:
    include = f"!{wheel.name}\n" if wheel is not None else ""
    return f"""*
!Dockerfile.results
!railway.json
!.railwayignore
{include}"""


def _atomic_write(path: Path, payload: bytes, *, mode: int) -> None:
    if path.is_symlink():
        raise VoiceyError("VY-SEC-002", detail=str(path))
    temporary: Path | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
        )
        temporary = Path(temporary_name)
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb") as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        path.chmod(mode)
    except OSError as exc:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        raise VoiceyError("VY-DEP-001", detail=f"could not write {path}.") from exc


def _parse_json(value: str, *, label: str) -> object:
    try:
        return json.loads(value)
    except json.JSONDecodeError as exc:
        raise VoiceyError("VY-DEP-007", detail=f"{label} was not valid JSON.") from exc


def _json_items(value: object) -> list[dict[str, object]]:
    if isinstance(value, list):
        return [
            cast("dict[str, object]", item)
            for item in cast("list[object]", value)
            if isinstance(item, dict)
        ]
    if isinstance(value, dict):
        mapping = cast("dict[str, object]", value)
        for key in (
            "projects",
            "services",
            "buckets",
            "domains",
            "deployments",
            "items",
            "data",
        ):
            nested = mapping.get(key)
            if isinstance(nested, list):
                return [
                    cast("dict[str, object]", item)
                    for item in cast("list[object]", nested)
                    if isinstance(item, dict)
                ]
        if mapping:
            return [mapping]
    return []


def _find_text(value: object, *keys: str) -> str:
    if not isinstance(value, dict):
        return ""
    mapping = cast("dict[str, object]", value)
    for key in keys:
        candidate = mapping.get(key)
        if isinstance(candidate, str):
            return candidate
    for nested in mapping.values():
        found = _find_text(nested, *keys)
        if found:
            return found
    return ""


def _item_text(item: Mapping[str, object], key: str) -> str:
    for candidate in (
        key,
        key.upper(),
        key.capitalize(),
        f"{key}_id",
        f"{key}Id",
    ):
        value = item.get(candidate)
        if isinstance(value, str):
            return value
    return ""


def _identity_matches(
    items: list[dict[str, object]],
    name: str,
    identity: str | None,
) -> list[dict[str, object]]:
    return [
        item
        for item in items
        if _item_text(item, "name") == name
        or (identity is not None and _item_text(item, "id") == identity)
    ]


def _domain_text(item: Mapping[str, object]) -> str:
    return (
        _item_text(item, "domain") or _item_text(item, "hostname") or _item_text(item, "url")
    ).removeprefix("https://")


def _safe_reference_namespace(value: str) -> bool:
    return bool(value) and "${{" not in value and "}}" not in value and "\n" not in value


def _variable_reference(namespace: str, variable: str) -> str:
    if not _safe_reference_namespace(namespace):
        raise VoiceyError(
            "VY-DEP-007",
            detail="Railway variable-reference namespace is unsafe.",
        )
    return "${{" + namespace + "." + variable + "}}"


__all__ = [
    "RailwayArtifactGenerator",
    "RailwayArtifacts",
    "RailwayCliRunner",
    "RailwayCommandResult",
    "RailwayCommandRunner",
    "RailwayDeploymentManager",
    "RailwayDeploymentReport",
    "RailwayPlan",
    "RailwayResourceState",
    "RailwayResourceStore",
    "RailwaySmokeReport",
]
