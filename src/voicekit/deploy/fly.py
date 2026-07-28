"""Resumable Fly companion provisioning, deployment, rotation, and smoke."""

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
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol, cast

import httpx
from pydantic import TypeAdapter, ValidationError

from voicekit._version import __version__
from voicekit.cli.environment import ensure_env_ignored
from voicekit.deploy.managed_secrets import (
    ManagedSecretBundle,
    fingerprint,
    prepare_managed_secrets,
    validate_secret_continuity,
)
from voicekit.errors import VoicekitError
from voicekit.relay.auth import RelayCredential
from voicekit.relay.client import RelayClient

_RESOURCE_SCHEMA = 1
_FLY_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{2,62}$")
_REGION = re.compile(r"^[a-z0-9]{3,8}$")
_CALLBACK_PROVIDERS = frozenset({"twilio", "telnyx", "vobiz", "plivo"})


@dataclass(frozen=True, slots=True)
class FlyPlan:
    """Explicit resource names and placement for one results companion."""

    app_name: str
    organization: str
    region: str
    postgres_name: str
    bucket_name: str
    callback_providers: tuple[str, ...] = ()
    postgres_plan: str = "Basic"
    postgres_volume_gb: int = 10

    def __post_init__(self) -> None:
        names = (self.app_name, self.postgres_name, self.bucket_name)
        if (
            any(not _FLY_NAME.fullmatch(value) for value in names)
            or not self.organization.strip()
            or not _REGION.fullmatch(self.region)
            or self.postgres_plan not in {"Basic", "Starter", "Launch", "Scale", "Performance"}
            or not 10 <= self.postgres_volume_gb <= 500
            or any(provider not in _CALLBACK_PROVIDERS for provider in self.callback_providers)
            or len(set(self.callback_providers)) != len(self.callback_providers)
        ):
            raise VoicekitError(
                "VK-DEP-003",
                detail="Fly names, region, MPG plan/volume, or callback providers are invalid.",
            )

    @property
    def public_base(self) -> str:
        return f"https://{self.app_name}.fly.dev"


@dataclass(frozen=True, slots=True)
class FlyArtifacts:
    """Tool-owned Fly files kept outside the user's source contract."""

    directory: Path
    dockerfile: Path
    config: Path
    dockerignore: Path
    engine_wheel: Path | None
    digest: str


@dataclass(frozen=True, slots=True)
class FlyCommandResult:
    """Captured non-secret process result."""

    returncode: int
    stdout: str
    stderr: str


class FlyCommandRunner(Protocol):
    """Injectable flyctl execution boundary."""

    def run(
        self,
        arguments: Sequence[str],
        *,
        stdin: str | None = None,
        check: bool = True,
        timeout_s: float = 600,
    ) -> FlyCommandResult: ...


class FlyctlRunner:
    """Run the installed Fly CLI without exposing imported secret values."""

    def __init__(self, executable: str | None = None) -> None:
        selected = executable or shutil.which("fly") or shutil.which("flyctl")
        if selected is None:
            raise VoicekitError(
                "VK-DEP-006",
                detail="the `fly`/`flyctl` executable is unavailable.",
            )
        self.executable = selected

    def run(
        self,
        arguments: Sequence[str],
        *,
        stdin: str | None = None,
        check: bool = True,
        timeout_s: float = 600,
    ) -> FlyCommandResult:
        try:
            completed = subprocess.run(
                [self.executable, *arguments],
                check=False,
                capture_output=True,
                text=True,
                input=stdin,
                timeout=timeout_s,
                env={**os.environ, "NO_COLOR": "1"},
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise VoicekitError(
                "VK-DEP-006",
                detail=f"flyctl execution failed ({type(exc).__name__}).",
            ) from exc
        result = FlyCommandResult(
            returncode=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
        )
        if check and result.returncode != 0:
            command = " ".join(arguments[:3])
            detail = f"`fly {command}` failed with exit {result.returncode}"
            raise VoicekitError("VK-DEP-006", detail=detail)
        return result


@dataclass(frozen=True, slots=True)
class FlyResourceState:
    """Non-secret checkpoint for resource adoption and reverse rollback."""

    schema_version: int
    app_name: str
    organization: str
    region: str
    postgres_name: str
    bucket_name: str
    app_created: bool = False
    postgres_created: bool = False
    bucket_created: bool = False
    postgres_id: str | None = None
    postgres_attached: bool = False
    bucket_attached: bool = False
    relay_key_id: str | None = None
    relay_fingerprint: str | None = None
    results_fingerprint: str | None = None
    artifact_digest: str | None = None
    deployed: bool = False
    smoke_green: bool = False
    rolled_back: bool = False
    updated_at: str | None = None

    @classmethod
    def initial(cls, plan: FlyPlan) -> FlyResourceState:
        return cls(
            schema_version=_RESOURCE_SCHEMA,
            app_name=plan.app_name,
            organization=plan.organization,
            region=plan.region,
            postgres_name=plan.postgres_name,
            bucket_name=plan.bucket_name,
        )

    @classmethod
    def from_payload(cls, payload: object) -> FlyResourceState:
        if not isinstance(payload, dict):
            raise VoicekitError("VK-DEP-007", detail="Fly resource ledger is not an object.")
        try:
            state = TypeAdapter(cls).validate_python(payload)
        except ValidationError as exc:
            raise VoicekitError(
                "VK-DEP-007",
                detail="Fly resource ledger fields are invalid.",
            ) from exc
        if (
            state.schema_version != _RESOURCE_SCHEMA
            or not _FLY_NAME.fullmatch(state.app_name)
            or not _FLY_NAME.fullmatch(state.postgres_name)
            or not _FLY_NAME.fullmatch(state.bucket_name)
            or not state.organization
            or not _REGION.fullmatch(state.region)
        ):
            raise VoicekitError(
                "VK-DEP-007",
                detail="Fly resource ledger version or identity is invalid.",
            )
        return state

    def validate_plan(self, plan: FlyPlan) -> None:
        expected = (
            self.app_name,
            self.organization,
            self.region,
            self.postgres_name,
            self.bucket_name,
        )
        actual = (
            plan.app_name,
            plan.organization,
            plan.region,
            plan.postgres_name,
            plan.bucket_name,
        )
        if expected != actual or self.rolled_back:
            raise VoicekitError(
                "VK-DEP-007",
                detail="Fly resource ledger does not match this plan or is already rolled back.",
            )

    def checkpoint(self, **changes: object) -> FlyResourceState:
        return replace(
            self,
            **changes,
            updated_at=datetime.now(UTC).isoformat(),
        )


class FlyResourceStore:
    """Atomic owner-only checkpoint that never contains secret values."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> FlyResourceState | None:
        if not self.path.exists():
            return None
        if self.path.is_symlink():
            raise VoicekitError("VK-SEC-002", detail=str(self.path))
        try:
            mode = stat.S_IMODE(self.path.stat().st_mode)
            if mode & 0o077:
                raise VoicekitError(
                    "VK-SEC-001",
                    detail=f"{self.path} must be owner-only (0600).",
                )
            return FlyResourceState.from_payload(json.loads(self.path.read_text(encoding="utf-8")))
        except VoicekitError:
            raise
        except (OSError, json.JSONDecodeError) as exc:
            raise VoicekitError(
                "VK-DEP-007",
                detail="Fly resource ledger cannot be read.",
            ) from exc

    def save(self, state: FlyResourceState) -> None:
        payload = json.dumps(asdict(state), indent=2, sort_keys=True) + "\n"
        _atomic_write(self.path, payload.encode(), mode=0o600)


@dataclass(frozen=True, slots=True)
class FlySmokeReport:
    """Platform and signed application readiness evidence."""

    app_name: str
    public_base: str
    platform_checks: int
    liveness: bool
    signed_readiness: bool


@dataclass(frozen=True, slots=True)
class FlyDeploymentReport:
    """Non-secret output from a resumable deployment."""

    state: FlyResourceState
    artifacts: FlyArtifacts
    smoke: FlySmokeReport | None


class FlyArtifactGenerator:
    """Render the minimal companion image and Fly service contract."""

    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root.resolve()
        self.directory = self.project_root / ".voicekit" / "deploy" / "fly"

    def generate(
        self,
        plan: FlyPlan,
        *,
        engine_wheel: Path | None,
    ) -> FlyArtifacts:
        wheel = self._copy_wheel(engine_wheel)
        dockerfile = self.directory / "Dockerfile.results"
        config = self.directory / "fly.results.toml"
        dockerignore = self.directory / "dockerignore"
        rendered = {
            dockerfile: _dockerfile(wheel),
            config: _fly_config(plan),
            dockerignore: _dockerignore(wheel),
        }
        for path, payload in rendered.items():
            _atomic_write(path, payload.encode(), mode=0o644)
        try:
            parsed = tomllib.loads(config.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise VoicekitError("VK-DEP-003", detail="generated Fly TOML is invalid.") from exc
        if (
            parsed.get("app") != plan.app_name
            or parsed.get("http_service", {}).get("internal_port") != 8080
            or parsed.get("env", {}).get("VOICEKIT_DEPLOY_TARGET") != "fly"
        ):
            raise VoicekitError(
                "VK-DEP-003",
                detail="generated Fly artifact failed its topology invariant.",
            )
        digest = hashlib.sha256(b"\0".join(path.read_bytes() for path in rendered)).hexdigest()
        return FlyArtifacts(
            directory=self.directory,
            dockerfile=dockerfile,
            config=config,
            dockerignore=dockerignore,
            engine_wheel=wheel,
            digest=digest,
        )

    def _copy_wheel(self, engine_wheel: Path | None) -> Path | None:
        if engine_wheel is None:
            if __version__.endswith(".dev0"):
                raise VoicekitError(
                    "VK-DEP-003",
                    detail=(
                        "this unpublished development build requires "
                        "`--engine-wheel /absolute/path/to/voicekit-*.whl`."
                    ),
                )
            return None
        source = engine_wheel.expanduser().resolve()
        if (
            not source.is_file()
            or source.suffix != ".whl"
            or not source.name.startswith("voicekit-")
        ):
            raise VoicekitError("VK-DEP-003", detail="the engine wheel is invalid.")
        destination = self.directory / source.name
        if source != destination:
            self.directory.mkdir(parents=True, exist_ok=True)
            temporary = destination.with_suffix(".whl.tmp")
            try:
                shutil.copyfile(source, temporary)
                os.replace(temporary, destination)
            except OSError as exc:
                temporary.unlink(missing_ok=True)
                raise VoicekitError(
                    "VK-DEP-001",
                    detail=f"could not stage engine wheel at {destination}.",
                ) from exc
        return destination


class FlyDeploymentManager:
    """Provision or reuse resources and checkpoint every external mutation."""

    def __init__(
        self,
        project_root: Path,
        *,
        runner: FlyCommandRunner | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.project_root = project_root.resolve()
        self.runner = runner or FlyctlRunner()
        self.http_client = http_client
        self.artifacts = FlyArtifactGenerator(self.project_root)
        self.store = FlyResourceStore(
            self.project_root / ".voicekit" / "deploy" / "fly-resources.json"
        )

    async def deploy(
        self,
        plan: FlyPlan,
        *,
        environment: Mapping[str, str],
        engine_wheel: Path | None = None,
        adopt: bool = False,
        rotate_credentials: bool = False,
        skip_smoke: bool = False,
    ) -> FlyDeploymentReport:
        """Execute the resumable resource, secret, deploy, and smoke sequence."""
        ensure_env_ignored(self.project_root)
        loaded = self.store.load()
        state = FlyResourceState.initial(plan) if loaded is None or loaded.rolled_back else loaded
        state.validate_plan(plan)
        generated = self.artifacts.generate(plan, engine_wheel=engine_wheel)
        bundle = prepare_managed_secrets(
            self.project_root,
            environment,
            plan.callback_providers,
            rotate=rotate_credentials,
            expected_relay_fingerprint=state.relay_fingerprint,
            expected_results_fingerprint=state.results_fingerprint,
        )
        validate_secret_continuity(state, bundle, rotate=rotate_credentials)
        state = state.checkpoint(artifact_digest=generated.digest)
        self.store.save(state)

        await asyncio.to_thread(self._authenticate)
        state = await asyncio.to_thread(self._ensure_app, plan, state, adopt)
        state = await asyncio.to_thread(self._ensure_postgres, plan, state, adopt)
        state = await asyncio.to_thread(self._ensure_bucket, plan, state, adopt)
        state = await asyncio.to_thread(self._sync_secrets, plan, state, bundle)
        state = await asyncio.to_thread(self._deploy_release, plan, state, generated)
        smoke = None
        if not skip_smoke:
            smoke = await self._smoke(plan, bundle.relay)
            state = state.checkpoint(smoke_green=True)
            self.store.save(state)
        return FlyDeploymentReport(state=state, artifacts=generated, smoke=smoke)

    async def validate_existing(
        self,
        plan: FlyPlan,
        *,
        relay_credential: str,
    ) -> FlySmokeReport:
        """Run the same platform and signed-readiness suite without mutation."""
        credential = RelayCredential.parse(relay_credential)
        return await self._smoke(plan, credential)

    def rollback_created(self, plan: FlyPlan) -> FlyResourceState:
        """Destroy only resources explicitly recorded as created by voicekit."""
        state = self.store.load()
        if state is None:
            raise VoicekitError("VK-DEP-007", detail="no Fly resource ledger exists.")
        state.validate_plan(plan)
        if state.bucket_created:
            self.runner.run(
                ["storage", "destroy", state.bucket_name, "--app", state.app_name, "--yes"]
            )
            state = state.checkpoint(bucket_created=False, bucket_attached=False)
            self.store.save(state)
        if state.postgres_created and state.postgres_id is not None:
            self.runner.run(["mpg", "destroy", state.postgres_id, "--yes"])
            state = state.checkpoint(
                postgres_created=False,
                postgres_attached=False,
                postgres_id=None,
            )
            self.store.save(state)
        if state.app_created:
            self.runner.run(["apps", "destroy", state.app_name, "--yes"])
            state = state.checkpoint(app_created=False, deployed=False, smoke_green=False)
            self.store.save(state)
        state = state.checkpoint(rolled_back=True)
        self.store.save(state)
        return state

    def _authenticate(self) -> None:
        self.runner.run(["auth", "whoami"], timeout_s=30)

    def _ensure_app(
        self,
        plan: FlyPlan,
        state: FlyResourceState,
        adopt: bool,
    ) -> FlyResourceState:
        probe = self.runner.run(
            ["status", "--app", plan.app_name, "--json"],
            check=False,
            timeout_s=30,
        )
        exists = probe.returncode == 0
        if state.app_created or state.deployed:
            if not exists:
                raise VoicekitError(
                    "VK-DEP-007",
                    detail="ledgered Fly app is missing; refusing speculative recreation.",
                )
            return state
        if exists:
            if not adopt:
                raise VoicekitError(
                    "VK-DEP-007",
                    detail=(
                        f"Fly app {plan.app_name} exists without ownership evidence; "
                        "rerun with --adopt after verifying it."
                    ),
                )
            checkpoint = state.checkpoint(app_created=False)
            self.store.save(checkpoint)
            return checkpoint
        self.runner.run(
            [
                "apps",
                "create",
                plan.app_name,
                "--org",
                plan.organization,
                "--yes",
                "--json",
            ],
            timeout_s=120,
        )
        checkpoint = state.checkpoint(app_created=True)
        self.store.save(checkpoint)
        return checkpoint

    def _ensure_postgres(
        self,
        plan: FlyPlan,
        state: FlyResourceState,
        adopt: bool,
    ) -> FlyResourceState:
        clusters = self._postgres_clusters(plan.organization)
        matches = [
            item
            for item in clusters
            if _item_text(item, "name") == plan.postgres_name
            or (state.postgres_id is not None and _item_text(item, "id") == state.postgres_id)
        ]
        if len(matches) > 1:
            raise VoicekitError("VK-DEP-007", detail="managed Postgres identity is ambiguous.")
        if matches:
            cluster_id = _item_text(matches[0], "id")
            if not cluster_id:
                raise VoicekitError(
                    "VK-DEP-007",
                    detail="managed Postgres list omitted the cluster id.",
                )
            if state.postgres_id is None and not adopt:
                raise VoicekitError(
                    "VK-DEP-007",
                    detail=(
                        f"MPG {plan.postgres_name} exists without ownership evidence; "
                        "rerun with --adopt after verifying it."
                    ),
                )
            if state.postgres_id not in {None, cluster_id}:
                raise VoicekitError("VK-DEP-007", detail="managed Postgres id drifted.")
            checkpoint = state.checkpoint(postgres_id=cluster_id)
        else:
            if state.postgres_id is not None:
                raise VoicekitError(
                    "VK-DEP-007",
                    detail="ledgered managed Postgres cluster is missing.",
                )
            self.runner.run(
                [
                    "mpg",
                    "create",
                    "--name",
                    plan.postgres_name,
                    "--org",
                    plan.organization,
                    "--region",
                    plan.region,
                    "--plan",
                    plan.postgres_plan,
                    "--pg-major-version",
                    "17",
                    "--volume-size",
                    str(plan.postgres_volume_gb),
                ],
                timeout_s=1800,
            )
            created = [
                item
                for item in self._postgres_clusters(plan.organization)
                if _item_text(item, "name") == plan.postgres_name
            ]
            if len(created) != 1 or not _item_text(created[0], "id"):
                raise VoicekitError(
                    "VK-DEP-007",
                    detail="created MPG cluster could not be resolved uniquely.",
                )
            checkpoint = state.checkpoint(
                postgres_created=True,
                postgres_id=_item_text(created[0], "id"),
            )
        self.store.save(checkpoint)
        if checkpoint.postgres_attached:
            return checkpoint
        secret_names = self._secret_names(plan.app_name)
        if "DATABASE_URL" in secret_names:
            if not adopt:
                raise VoicekitError(
                    "VK-DEP-007",
                    detail=(
                        "DATABASE_URL already exists without an attachment checkpoint; "
                        "rerun with --adopt only after verifying its MPG target."
                    ),
                )
        else:
            self.runner.run(
                [
                    "mpg",
                    "attach",
                    checkpoint.postgres_id or "",
                    "--app",
                    plan.app_name,
                    "--variable-name",
                    "DATABASE_URL",
                ],
                timeout_s=600,
            )
            if "DATABASE_URL" not in self._secret_names(plan.app_name):
                raise VoicekitError(
                    "VK-DEP-007",
                    detail="MPG attach completed without DATABASE_URL evidence.",
                )
        attached = checkpoint.checkpoint(postgres_attached=True)
        self.store.save(attached)
        return attached

    def _ensure_bucket(
        self,
        plan: FlyPlan,
        state: FlyResourceState,
        adopt: bool,
    ) -> FlyResourceState:
        listed = self.runner.run(
            ["storage", "list", "--org", plan.organization, "--yes"],
            timeout_s=60,
        )
        exists = _table_contains_name(listed.stdout, plan.bucket_name)
        if state.bucket_created or state.bucket_attached:
            if not exists:
                raise VoicekitError(
                    "VK-DEP-007",
                    detail="ledgered Tigris bucket is missing.",
                )
            _require_tigris_secrets(self._secret_names(plan.app_name))
            return state
        if exists:
            if not adopt:
                raise VoicekitError(
                    "VK-DEP-007",
                    detail=(
                        f"Tigris bucket {plan.bucket_name} exists without ownership evidence; "
                        "rerun with --adopt after verifying it."
                    ),
                )
            _require_tigris_secrets(self._secret_names(plan.app_name))
            checkpoint = state.checkpoint(bucket_attached=True)
            self.store.save(checkpoint)
            return checkpoint
        self.runner.run(
            [
                "storage",
                "create",
                "--app",
                plan.app_name,
                "--name",
                plan.bucket_name,
                "--org",
                plan.organization,
                "--yes",
            ],
            timeout_s=600,
        )
        _require_tigris_secrets(self._secret_names(plan.app_name))
        checkpoint = state.checkpoint(bucket_created=True, bucket_attached=True)
        self.store.save(checkpoint)
        return checkpoint

    def _sync_secrets(
        self,
        plan: FlyPlan,
        state: FlyResourceState,
        bundle: ManagedSecretBundle,
    ) -> FlyResourceState:
        values = bundle.platform_values()
        payload = "".join(f"{name}={value}\n" for name, value in sorted(values.items()))
        self.runner.run(
            ["secrets", "import", "--app", plan.app_name, "--stage"],
            stdin=payload,
            timeout_s=120,
        )
        names = self._secret_names(plan.app_name)
        missing = sorted(set(values) - names)
        if missing:
            raise VoicekitError(
                "VK-DEP-007",
                detail=f"Fly secret sync lacks {', '.join(missing)}.",
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
        plan: FlyPlan,
        state: FlyResourceState,
        artifacts: FlyArtifacts,
    ) -> FlyResourceState:
        self.runner.run(
            [
                "deploy",
                str(self.project_root),
                "--app",
                plan.app_name,
                "--config",
                str(artifacts.config),
                "--strategy",
                "rolling",
                "--ha",
                "--yes",
                "--wait-timeout",
                "10m",
            ],
            timeout_s=1800,
        )
        status = self.runner.run(
            ["status", "--app", plan.app_name, "--deployment", "--json"],
            timeout_s=60,
        )
        _parse_json(status.stdout, label="Fly application status")
        checkpoint = state.checkpoint(deployed=True, smoke_green=False)
        self.store.save(checkpoint)
        return checkpoint

    async def _smoke(
        self,
        plan: FlyPlan,
        relay_credential: RelayCredential,
    ) -> FlySmokeReport:
        checks = await asyncio.to_thread(
            self.runner.run,
            ["checks", "list", "--app", plan.app_name, "--json"],
            timeout_s=60,
        )
        check_items = _json_items(_parse_json(checks.stdout, label="Fly checks"))
        if not check_items or any(not _check_passing(item) for item in check_items):
            raise VoicekitError(
                "VK-DEP-004",
                detail="Fly service checks are absent or not passing.",
            )
        owns_client = self.http_client is None
        client = self.http_client or httpx.AsyncClient(timeout=10)
        try:
            response = await client.get(f"{plan.public_base}/healthz")
            if response.status_code != 200:
                raise VoicekitError(
                    "VK-DEP-004",
                    detail=f"Fly liveness returned HTTP {response.status_code}.",
                )
            async with RelayClient(
                plan.public_base,
                relay_credential,
                client=client,
                max_attempts=3,
            ):
                pass
        except httpx.HTTPError as exc:
            raise VoicekitError(
                "VK-DEP-004",
                detail=f"Fly endpoint smoke failed ({type(exc).__name__}).",
            ) from exc
        finally:
            if owns_client:
                await client.aclose()
        return FlySmokeReport(
            app_name=plan.app_name,
            public_base=plan.public_base,
            platform_checks=len(check_items),
            liveness=True,
            signed_readiness=True,
        )

    def _postgres_clusters(self, organization: str) -> list[dict[str, object]]:
        result = self.runner.run(
            ["mpg", "list", "--org", organization, "--json"],
            timeout_s=60,
        )
        return _json_items(_parse_json(result.stdout, label="managed Postgres list"))

    def _secret_names(self, app_name: str) -> set[str]:
        result = self.runner.run(
            ["secrets", "list", "--app", app_name, "--json"],
            timeout_s=60,
        )
        return {
            name
            for item in _json_items(_parse_json(result.stdout, label="Fly secret list"))
            if (name := _item_text(item, "name"))
        }


def _dockerfile(wheel: Path | None) -> str:
    package = f"voicekit[companion]=={__version__}"
    install = (
        f'uv pip install --python /opt/voicekit/bin/python "/tmp/{wheel.name}[companion]"'
        if wheel is not None
        else f'uv pip install --python /opt/voicekit/bin/python "{package}"'
    )
    copy = f"COPY .voicekit/deploy/fly/{wheel.name} /tmp/{wheel.name}\n" if wheel else ""
    return f"""# syntax=docker/dockerfile:1.7
# Generated by voicekit {__version__}. Secrets are supplied by Fly at runtime.
FROM python:3.14-slim-bookworm AS build
RUN python -m pip install --no-cache-dir uv==0.11.7 \\
    && python -m venv /opt/voicekit
{copy}RUN {install}

FROM python:3.14-slim-bookworm AS runtime
RUN apt-get update \\
    && apt-get install --no-install-recommends -y ca-certificates \\
    && rm -rf /var/lib/apt/lists/* \\
    && groupadd --system --gid 10001 voicekit \\
    && useradd --system --uid 10001 --gid 10001 --home-dir /app \\
         --shell /usr/sbin/nologin voicekit
WORKDIR /app
COPY --from=build /opt/voicekit /opt/voicekit
ENV PATH="/opt/voicekit/bin:$PATH" \\
    PYTHONUNBUFFERED="1" \\
    PYTHONDONTWRITEBYTECODE="1"
USER 10001:10001
EXPOSE 8080 9464
CMD ["python", "-m", "voicekit.deploy.results_service"]
"""


def _fly_config(plan: FlyPlan) -> str:
    callbacks = ",".join(plan.callback_providers)
    values = {
        "VOICEKIT_PUBLIC_BASE": plan.public_base,
        "VOICEKIT_OBJECT_BUCKET": plan.bucket_name,
        "VOICEKIT_OBJECT_PREFIX": "voicekit",
        "VOICEKIT_OBJECT_ENDPOINT": "https://fly.storage.tigris.dev",
        "AWS_REGION": "auto",
        "VOICEKIT_DEPLOY_TARGET": "fly",
        "VOICEKIT_STORAGE_BACKEND": "postgres",
        "VOICEKIT_ARTIFACT_BACKEND": "s3",
        "VOICEKIT_CALLBACK_PROVIDERS": callbacks,
        "VOICEKIT_DB_POOL_MIN": "1",
        "VOICEKIT_DB_POOL_MAX": "5",
        "VOICEKIT_DB_CONNECTION_BUDGET": "20",
        "VOICEKIT_DRAIN_GRACE_S": "20",
        "VOICEKIT_PROMETHEUS_ENABLED": "1",
        "VOICEKIT_PROMETHEUS_BIND": "0.0.0.0",
        "VOICEKIT_PROMETHEUS_PORT": "9464",
        "VOICEKIT_PROMETHEUS_PATH": "/metrics",
        "PORT": "8080",
    }
    environment = "\n".join(
        f"{name} = {json.dumps(value)}" for name, value in sorted(values.items())
    )
    return f"""# Generated by voicekit {__version__}. Do not add secrets here.
app = {json.dumps(plan.app_name)}
primary_region = {json.dumps(plan.region)}
kill_signal = "SIGTERM"
kill_timeout = 45

[build]
  dockerfile = ".voicekit/deploy/fly/Dockerfile.results"
  ignorefile = ".voicekit/deploy/fly/dockerignore"

[env]
{environment}

[http_service]
  internal_port = 8080
  force_https = true
  auto_stop_machines = "off"
  auto_start_machines = true
  min_machines_running = 2

  [http_service.concurrency]
    type = "requests"
    soft_limit = 100
    hard_limit = 200

  [[http_service.checks]]
    grace_period = "60s"
    interval = "15s"
    method = "GET"
    path = "/healthz"
    timeout = "5s"

[metrics]
  port = 9464
  path = "/metrics"

[[vm]]
  size = "shared-cpu-1x"
  memory = "512mb"
"""


def _dockerignore(wheel: Path | None) -> str:
    include = f"!.voicekit/deploy/fly/{wheel.name}\n" if wheel is not None else ""
    return f"""*
!.voicekit/
!.voicekit/deploy/
!.voicekit/deploy/fly/
{include}"""


def _atomic_write(path: Path, payload: bytes, *, mode: int) -> None:
    if path.is_symlink():
        raise VoicekitError("VK-SEC-002", detail=str(path))
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
        raise VoicekitError("VK-DEP-001", detail=f"could not write {path}.") from exc


def _parse_json(value: str, *, label: str) -> object:
    try:
        return json.loads(value)
    except json.JSONDecodeError as exc:
        raise VoicekitError("VK-DEP-007", detail=f"{label} was not valid JSON.") from exc


def _json_items(value: object) -> list[dict[str, object]]:
    if isinstance(value, list):
        items = cast("list[object]", value)
        return [cast("dict[str, object]", item) for item in items if isinstance(item, dict)]
    if isinstance(value, dict):
        mapping = cast("dict[str, object]", value)
        for key in ("data", "clusters", "secrets", "checks", "items"):
            nested = mapping.get(key)
            if isinstance(nested, list):
                items = cast("list[object]", nested)
                return [cast("dict[str, object]", item) for item in items if isinstance(item, dict)]
        if mapping:
            return [mapping]
    return []


def _item_text(item: Mapping[str, object], key: str) -> str:
    for candidate in (key, key.upper(), key.capitalize(), f"{key}_id", f"{key}Id"):
        value = item.get(candidate)
        if isinstance(value, str):
            return value
    return ""


def _table_contains_name(output: str, name: str) -> bool:
    plain = re.sub(r"\x1b\[[0-9;]*m", "", output)
    return any(name in re.split(r"[\s|,]+", line.strip()) for line in plain.splitlines())


def _check_passing(item: Mapping[str, object]) -> bool:
    status = _item_text(item, "status").lower()
    return status in {"passing", "pass", "healthy", "success", "ok"}


def _require_tigris_secrets(names: set[str]) -> None:
    required = {"AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"}
    if not required.issubset(names):
        raise VoicekitError(
            "VK-DEP-007",
            detail="Tigris bucket is not attached to the app with private access credentials.",
        )


__all__ = [
    "FlyArtifactGenerator",
    "FlyArtifacts",
    "FlyCommandResult",
    "FlyCommandRunner",
    "FlyDeploymentManager",
    "FlyDeploymentReport",
    "FlyPlan",
    "FlyResourceState",
    "FlyResourceStore",
    "FlySmokeReport",
    "FlyctlRunner",
]
