"""Resumable Pipecat Cloud and LiveKit Cloud deployment drivers."""

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
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Literal, Protocol, cast
from urllib.parse import urlsplit

from voicey._version import __version__
from voicey.cli.environment import EnvFileStore
from voicey.config.catalog import DEFAULT_PROVIDER_CATALOG
from voicey.config.manifest import ManifestStore, ProjectManifest
from voicey.config.models import Agent
from voicey.deploy.cloud_smoke import LiveKitCloudSessionSmoke
from voicey.errors import VoiceyError
from voicey.relay.auth import RelayCredential
from voicey.relay.client import RelayClient

CloudPlatform = Literal["pipecat-cloud", "livekit-cloud"]
_NAME = re.compile(r"^[a-z][a-z0-9-]{1,53}$")
_IMAGE = re.compile(r"^[^\s:@]+(?:/[^\s:@]+)+:[^\s:@]+$")
_LIVEKIT_ID = re.compile(r"^[A-Za-z0-9_-]{6,128}$")
_SECRET_NAME = re.compile(r"^[A-Z][A-Z0-9_]*$")
_PIPECAT_CLOUD_BASE = "dailyco/pipecat-base:0.1.0-py3.13"
_EXCLUDED_PARTS = {
    ".git",
    ".hg",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    ".venv",
    ".voicey",
    "__pycache__",
    "dist",
    "node_modules",
}
_WORKER_SECRET_EXCLUSIONS = {
    "AWS_ACCESS_KEY_ID",
    "AWS_ENDPOINT_URL_S3",
    "AWS_REGION",
    "AWS_SECRET_ACCESS_KEY",
    "BUCKET_NAME",
    "DATABASE_URL",
    "VOICEY_ARTIFACT_BACKEND",
    "VOICEY_OBJECT_BUCKET",
    "VOICEY_OBJECT_ENDPOINT",
    "VOICEY_OBJECT_PREFIX",
    "VOICEY_OBJECT_REGION",
    "VOICEY_RELAY_PREVIOUS_CREDENTIAL",
    "VOICEY_RESULTS_PREVIOUS_SECRET",
    "VOICEY_RESULTS_SECRET",
    "VOICEY_STORAGE_BACKEND",
}


@dataclass(frozen=True, slots=True)
class CloudCommandResult:
    """Captured noninteractive platform CLI result."""

    returncode: int
    stdout: str = ""
    stderr: str = ""


class CloudCommandRunner(Protocol):
    """Command adapter used by both real and deterministic fake control planes."""

    def run(
        self,
        arguments: Sequence[str],
        *,
        cwd: Path,
        check: bool = True,
        timeout_s: float = 1800,
    ) -> CloudCommandResult: ...


class PlatformCliRunner:
    """Execute one exact CLI without ever reflecting platform output on failure."""

    def __init__(self, executable_name: str) -> None:
        executable = shutil.which(executable_name)
        if executable is None:
            raise VoiceyError(
                "VY-DEP-009",
                detail=f"the `{executable_name}` platform CLI is unavailable.",
            )
        self.executable = executable
        self.executable_name = executable_name

    def run(
        self,
        arguments: Sequence[str],
        *,
        cwd: Path,
        check: bool = True,
        timeout_s: float = 1800,
    ) -> CloudCommandResult:
        try:
            completed = subprocess.run(
                [self.executable, *arguments],
                cwd=cwd,
                check=False,
                capture_output=True,
                text=True,
                timeout=timeout_s,
                env={**os.environ, "NO_COLOR": "1"},
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise VoiceyError(
                "VY-DEP-009",
                detail=f"{self.executable_name} failed ({type(exc).__name__}).",
            ) from exc
        result = CloudCommandResult(
            returncode=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
        )
        if check and result.returncode != 0:
            raise VoiceyError(
                "VY-DEP-009",
                detail=(
                    f"{self.executable_name} command failed with exit "
                    f"{result.returncode}; inspect the platform CLI directly."
                ),
            )
        return result


@dataclass(frozen=True, slots=True)
class PipecatCloudPlan:
    """Every operator-selected Pipecat Cloud cost and identity input."""

    agent_name: str
    organization: str
    region: str
    secret_set: str
    image: str
    relay_url: str
    min_agents: int
    max_agents: int
    profile: Literal["agent-1x", "agent-2x", "agent-3x"]
    image_pull_secret: str | None = None

    def __post_init__(self) -> None:
        _validate_common(self.agent_name, self.region, self.relay_url)
        if (
            not _NAME.fullmatch(self.organization)
            or not _NAME.fullmatch(self.secret_set)
            or not _IMAGE.fullmatch(self.image)
            or not 0 <= self.min_agents <= self.max_agents <= 50
            or self.max_agents < 1
            or self.profile not in {"agent-1x", "agent-2x", "agent-3x"}
            or (self.image_pull_secret is not None and not _NAME.fullmatch(self.image_pull_secret))
        ):
            raise VoiceyError(
                "VY-DEP-008",
                detail="Pipecat Cloud identity, image, or scaling plan is invalid.",
            )


@dataclass(frozen=True, slots=True)
class LiveKitCloudPlan:
    """Every operator-selected LiveKit Cloud identity and regional input."""

    agent_name: str
    project: str
    region: str
    relay_url: str
    agent_id: str | None = None

    def __post_init__(self) -> None:
        _validate_common(self.agent_name, self.region, self.relay_url)
        if not _NAME.fullmatch(self.project) or (
            self.agent_id is not None and not _LIVEKIT_ID.fullmatch(self.agent_id)
        ):
            raise VoiceyError(
                "VY-DEP-008",
                detail="LiveKit Cloud project or agent id is invalid.",
            )


@dataclass(frozen=True, slots=True)
class CloudArtifacts:
    """Secret-free immutable build context generated for a cloud worker."""

    platform: CloudPlatform
    directory: Path
    context: Path
    dockerfile: Path
    platform_config: Path | None
    bot: Path | None
    engine_wheel: Path | None
    digest: str


@dataclass(frozen=True, slots=True)
class CloudResourceState:
    """Owner-only, nonsecret resume/rollback facts."""

    schema_version: int
    platform: CloudPlatform
    agent_name: str
    account_scope: str
    region: str
    relay_origin: str
    relay_key_id: str
    relay_fingerprint: str
    artifact_digest: str
    agent_created: bool = False
    agent_adopted: bool = False
    agent_id: str | None = None
    previous_version: str | None = None
    cutover_provider: str | None = None
    cutover_token: str | None = None
    smoke_call_id: str | None = None
    secrets_synced: bool = False
    deployed: bool = False
    platform_ready: bool = False
    relay_ready: bool = False
    rolled_back: bool = False

    @classmethod
    def initial(
        cls,
        *,
        platform: CloudPlatform,
        agent_name: str,
        account_scope: str,
        region: str,
        relay_url: str,
        relay: RelayCredential,
        relay_fingerprint: str,
        artifact_digest: str,
    ) -> CloudResourceState:
        return cls(
            schema_version=1,
            platform=platform,
            agent_name=agent_name,
            account_scope=account_scope,
            region=region,
            relay_origin=_origin(relay_url),
            relay_key_id=relay.key_id,
            relay_fingerprint=relay_fingerprint,
            artifact_digest=artifact_digest,
        )

    @classmethod
    def from_payload(cls, value: object) -> CloudResourceState:
        if not isinstance(value, dict):
            raise VoiceyError("VY-DEP-010", detail="cloud resource ledger is not an object.")
        payload = cast("dict[str, object]", value)
        required_strings = (
            "agent_name",
            "account_scope",
            "region",
            "relay_origin",
            "relay_key_id",
            "relay_fingerprint",
            "artifact_digest",
        )
        boolean_fields = (
            "agent_created",
            "agent_adopted",
            "secrets_synced",
            "deployed",
            "platform_ready",
            "relay_ready",
            "rolled_back",
        )
        expected_fields = {
            "schema_version",
            "platform",
            *required_strings,
            *boolean_fields,
            "agent_id",
            "previous_version",
            "cutover_provider",
            "cutover_token",
            "smoke_call_id",
        }
        platform = payload.get("platform")
        if (
            set(payload) != expected_fields
            or payload.get("schema_version") != 1
            or platform not in {"pipecat-cloud", "livekit-cloud"}
            or any(not isinstance(payload.get(name), str) for name in required_strings)
            or any(not isinstance(payload.get(name), bool) for name in boolean_fields)
            or any(
                payload.get(name) is not None and not isinstance(payload.get(name), str)
                for name in (
                    "agent_id",
                    "previous_version",
                    "cutover_provider",
                    "cutover_token",
                    "smoke_call_id",
                )
            )
        ):
            raise VoiceyError(
                "VY-DEP-010",
                detail="cloud resource ledger fields are invalid.",
            )
        return cls(
            schema_version=1,
            platform=cast(CloudPlatform, platform),
            agent_name=cast(str, payload["agent_name"]),
            account_scope=cast(str, payload["account_scope"]),
            region=cast(str, payload["region"]),
            relay_origin=cast(str, payload["relay_origin"]),
            relay_key_id=cast(str, payload["relay_key_id"]),
            relay_fingerprint=cast(str, payload["relay_fingerprint"]),
            artifact_digest=cast(str, payload["artifact_digest"]),
            agent_created=cast(bool, payload["agent_created"]),
            agent_adopted=cast(bool, payload["agent_adopted"]),
            agent_id=cast("str | None", payload["agent_id"]),
            previous_version=cast("str | None", payload["previous_version"]),
            cutover_provider=cast("str | None", payload["cutover_provider"]),
            cutover_token=cast("str | None", payload["cutover_token"]),
            smoke_call_id=cast("str | None", payload["smoke_call_id"]),
            secrets_synced=cast(bool, payload["secrets_synced"]),
            deployed=cast(bool, payload["deployed"]),
            platform_ready=cast(bool, payload["platform_ready"]),
            relay_ready=cast(bool, payload["relay_ready"]),
            rolled_back=cast(bool, payload["rolled_back"]),
        )

    def validate(
        self,
        *,
        platform: CloudPlatform,
        agent_name: str,
        account_scope: str,
        region: str,
        relay_url: str,
        relay_fingerprint: str,
    ) -> None:
        if self.rolled_back:
            raise VoiceyError(
                "VY-DEP-010",
                detail="cloud resource ledger is already rolled back.",
            )
        if (
            self.platform != platform
            or self.agent_name != agent_name
            or self.account_scope != account_scope
            or self.region != region
            or self.relay_origin != _origin(relay_url)
            or self.relay_fingerprint != relay_fingerprint
        ):
            raise VoiceyError(
                "VY-DEP-010",
                detail="cloud deployment identity or relay credential drifted.",
            )

    def checkpoint(self, **changes: object) -> CloudResourceState:
        return replace(self, **changes)


class CloudResourceStore:
    """Atomic owner-only cloud checkpoint file."""

    def __init__(self, project_root: Path, platform: CloudPlatform) -> None:
        self.path = project_root.resolve() / ".voicey" / "deploy" / f"{platform}-resources.json"

    def load(self) -> CloudResourceState | None:
        if not self.path.exists():
            return None
        _require_private_regular(self.path)
        try:
            payload: object = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise VoiceyError("VY-DEP-010", detail="cloud resource ledger cannot be read.") from exc
        return CloudResourceState.from_payload(payload)

    def save(self, state: CloudResourceState) -> None:
        payload = json.dumps(asdict(state), indent=2, sort_keys=True).encode() + b"\n"
        _atomic_write(self.path, payload, mode=0o600)


@dataclass(frozen=True, slots=True)
class CloudSmokeReport:
    """Evidence shared by both platform deployment paths."""

    platform: CloudPlatform
    agent_name: str
    platform_ready: bool
    relay_ready: bool
    session_smoke: bool


@dataclass(frozen=True, slots=True)
class CloudDeploymentReport:
    """Completed local/platform facts returned to CLI JSON output."""

    state: CloudResourceState
    artifacts: CloudArtifacts
    smoke: CloudSmokeReport


class CloudArtifactGenerator:
    """Create a filtered context that cannot copy `.env` or VCS material."""

    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root.resolve()

    def generate(
        self,
        platform: CloudPlatform,
        *,
        engine_wheel: Path | None,
        agent_name: str,
        secret_set: str | None = None,
        image: str | None = None,
        region: str,
        min_agents: int | None = None,
        max_agents: int | None = None,
        profile: str | None = None,
    ) -> CloudArtifacts:
        manifest = ManifestStore(self.project_root / "voicey.jsonc").load()
        expected = "pipecat" if platform == "pipecat-cloud" else "livekit"
        if manifest.runtime != expected:
            raise VoiceyError(
                "VY-DEP-008",
                detail=f"{platform} requires a {expected} project.",
            )
        directory = self.project_root / ".voicey" / "deploy" / platform
        directory.mkdir(parents=True, exist_ok=True)
        temporary = Path(tempfile.mkdtemp(prefix=".context.", dir=directory))
        try:
            project = temporary / "project"
            self._copy_project(project)
            wheel = _stage_wheel(engine_wheel, temporary)
            extras = _runtime_extras(manifest)
            requirements = _project_requirements(self.project_root / "pyproject.toml")
            (temporary / "project-requirements.txt").write_text(
                requirements,
                encoding="utf-8",
            )
            bot: Path | None = None
            if platform == "pipecat-cloud":
                bot = temporary / "bot.py"
                bot.write_text(_pipecat_bot(), encoding="utf-8")
            dockerfile = temporary / "Dockerfile"
            dockerfile.write_text(
                _cloud_dockerfile(platform, wheel=wheel, extras=extras),
                encoding="utf-8",
            )
            platform_config: Path | None = None
            if platform == "pipecat-cloud":
                if (
                    secret_set is None
                    or image is None
                    or min_agents is None
                    or max_agents is None
                    or profile is None
                ):
                    raise VoiceyError(
                        "VY-DEP-008",
                        detail="Pipecat Cloud artifact inputs are incomplete.",
                    )
                platform_config = temporary / "pcc-deploy.toml"
                platform_config.write_text(
                    _pcc_config(
                        agent_name=agent_name,
                        image=image,
                        secret_set=secret_set,
                        region=region,
                        min_agents=min_agents,
                        max_agents=max_agents,
                        profile=profile,
                    ),
                    encoding="utf-8",
                )
            digest = _tree_digest(temporary)
            target = directory / "context"
            backup = directory / ".context.previous"
            if backup.exists():
                shutil.rmtree(backup)
            if target.exists():
                os.replace(target, backup)
            os.replace(temporary, target)
            if backup.exists():
                shutil.rmtree(backup)
        except Exception:
            if temporary.exists():
                shutil.rmtree(temporary)
            raise
        wheel_target = None if wheel is None else target / wheel.name
        bot_target = None if bot is None else target / bot.name
        config_target = None if platform_config is None else target / platform_config.name
        return CloudArtifacts(
            platform=platform,
            directory=directory,
            context=target,
            dockerfile=target / "Dockerfile",
            platform_config=config_target,
            bot=bot_target,
            engine_wheel=wheel_target,
            digest=digest,
        )

    def _copy_project(self, destination: Path) -> None:
        destination.mkdir(parents=True, exist_ok=True)
        for source in sorted(self.project_root.rglob("*")):
            relative = source.relative_to(self.project_root)
            if any(part in _EXCLUDED_PARTS or part.startswith(".") for part in relative.parts):
                continue
            if source.is_symlink():
                raise VoiceyError(
                    "VY-DEP-008",
                    detail=f"cloud build context rejects symlink {relative}.",
                )
            target = destination / relative
            if source.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            if not source.is_file():
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
            target.chmod(0o644)


class PipecatCloudDeploymentManager:
    """Sync a secret set and deploy a pre-pushed image with the current CLI."""

    def __init__(
        self,
        project_root: Path,
        *,
        runner: CloudCommandRunner | None = None,
        relay_client_factory: (Callable[[str, RelayCredential], RelayClient] | None) = None,
        smoke_claim_timeout_s: float = 90,
        smoke_terminal_timeout_s: float = 120,
        smoke_poll_interval_s: float = 2,
    ) -> None:
        if smoke_claim_timeout_s <= 0 or smoke_terminal_timeout_s <= 0 or smoke_poll_interval_s < 0:
            raise VoiceyError("VY-DEP-008", detail="cloud smoke timeouts are invalid.")
        self.project_root = project_root.resolve()
        self.runner = runner or PlatformCliRunner("pipecat")
        self.store = CloudResourceStore(self.project_root, "pipecat-cloud")
        self.artifacts = CloudArtifactGenerator(self.project_root)
        self._relay_client_factory = relay_client_factory or RelayClient
        self._smoke_claim_timeout_s = smoke_claim_timeout_s
        self._smoke_terminal_timeout_s = smoke_terminal_timeout_s
        self._smoke_poll_interval_s = smoke_poll_interval_s

    def prepare(
        self,
        plan: PipecatCloudPlan,
        *,
        engine_wheel: Path | None,
    ) -> CloudArtifacts:
        """Generate a secret-free context before the operator builds and pushes."""
        manifest = ManifestStore(self.project_root / "voicey.jsonc").load()
        agent = _load_agent(self.project_root, manifest)
        _require_agent_identity(agent, plan.agent_name)
        return self.artifacts.generate(
            "pipecat-cloud",
            engine_wheel=engine_wheel,
            agent_name=plan.agent_name,
            secret_set=plan.secret_set,
            image=plan.image,
            region=plan.region,
            min_agents=plan.min_agents,
            max_agents=plan.max_agents,
            profile=plan.profile,
        )

    async def deploy(
        self,
        plan: PipecatCloudPlan,
        *,
        environment: Mapping[str, str],
        engine_wheel: Path | None,
        adopt: bool = False,
        skip_session_smoke: bool = False,
    ) -> CloudDeploymentReport:
        manifest = ManifestStore(self.project_root / "voicey.jsonc").load()
        agent = _load_agent(self.project_root, manifest)
        _require_agent_identity(agent, plan.agent_name)
        artifacts = self.artifacts.generate(
            "pipecat-cloud",
            engine_wheel=engine_wheel,
            agent_name=plan.agent_name,
            secret_set=plan.secret_set,
            image=plan.image,
            region=plan.region,
            min_agents=plan.min_agents,
            max_agents=plan.max_agents,
            profile=plan.profile,
        )
        relay, worker_secrets = _worker_secrets(
            self.project_root,
            plan.relay_url,
            "pipecat",
            manifest,
            agent,
            environment,
        )
        relay_fingerprint = _fingerprint(relay.reveal())
        await _validate_relay(
            plan.relay_url,
            relay,
            client_factory=self._relay_client_factory,
        )
        state = self.store.load()
        if state is None:
            state = CloudResourceState.initial(
                platform="pipecat-cloud",
                agent_name=plan.agent_name,
                account_scope=plan.organization,
                region=plan.region,
                relay_url=plan.relay_url,
                relay=relay,
                relay_fingerprint=relay_fingerprint,
                artifact_digest=artifacts.digest,
            )
            self.store.save(state)
        else:
            state.validate(
                platform="pipecat-cloud",
                agent_name=plan.agent_name,
                account_scope=plan.organization,
                region=plan.region,
                relay_url=plan.relay_url,
                relay_fingerprint=relay_fingerprint,
            )
            state = state.checkpoint(artifact_digest=artifacts.digest)
            self.store.save(state)

        self.runner.run(["cloud", "auth", "whoami"], cwd=artifacts.context, timeout_s=30)
        regions = self.runner.run(
            ["cloud", "regions", "list"],
            cwd=artifacts.context,
            timeout_s=60,
        )
        _require_region(regions.stdout, plan.region, platform="Pipecat Cloud")
        status = self.runner.run(
            [
                "cloud",
                "agent",
                "status",
                plan.agent_name,
                "--organization",
                plan.organization,
            ],
            cwd=artifacts.context,
            check=False,
            timeout_s=60,
        )
        exists = _pipecat_agent_exists(status)
        if exists and not (state.agent_created or state.agent_adopted):
            if not adopt:
                raise VoiceyError(
                    "VY-DEP-010",
                    detail="Pipecat Cloud agent exists without ownership evidence; use --adopt.",
                )
            state = state.checkpoint(agent_adopted=True)
            self.store.save(state)
        elif not exists and (state.agent_created or state.agent_adopted or state.deployed):
            raise VoiceyError(
                "VY-DEP-010",
                detail="ledgered Pipecat Cloud agent is missing.",
            )

        with _secret_file(worker_secrets) as secret_file:
            self.runner.run(
                [
                    "cloud",
                    "secrets",
                    "set",
                    plan.secret_set,
                    "--file",
                    str(secret_file),
                    "--skip",
                    "--organization",
                    plan.organization,
                    "--region",
                    plan.region,
                ],
                cwd=artifacts.context,
                timeout_s=300,
            )
        listed = self.runner.run(
            [
                "cloud",
                "secrets",
                "list",
                plan.secret_set,
                "--organization",
                plan.organization,
                "--region",
                plan.region,
            ],
            cwd=artifacts.context,
            timeout_s=60,
        )
        _require_secret_names(listed.stdout, set(worker_secrets))
        state = state.checkpoint(secrets_synced=True)
        self.store.save(state)

        if state.deployed:
            _require_pipecat_image(status.stdout, plan.image)
            ready = status
        else:
            command = [
                "cloud",
                "deploy",
                plan.agent_name,
                plan.image,
                "--min-agents",
                str(plan.min_agents),
                "--max-agents",
                str(plan.max_agents),
                "--secrets",
                plan.secret_set,
                "--organization",
                plan.organization,
                "--profile",
                plan.profile,
                "--region",
                plan.region,
                "--force",
            ]
            if plan.image_pull_secret is None:
                command.append("--no-credentials")
            else:
                command.extend(["--credentials", plan.image_pull_secret])
            self.runner.run(command, cwd=artifacts.context, timeout_s=1800)
            if not exists:
                state = state.checkpoint(agent_created=True)
            state = state.checkpoint(deployed=True)
            self.store.save(state)
            ready = self.runner.run(
                [
                    "cloud",
                    "agent",
                    "status",
                    plan.agent_name,
                    "--organization",
                    plan.organization,
                ],
                cwd=artifacts.context,
                timeout_s=60,
            )
        _require_ready(ready.stdout, platform="Pipecat Cloud")
        session_green = False
        if not skip_session_smoke:
            session_green = await self._session_smoke(
                plan,
                artifacts,
                relay,
            )
        state = state.checkpoint(platform_ready=True, relay_ready=True)
        self.store.save(state)
        return CloudDeploymentReport(
            state=state,
            artifacts=artifacts,
            smoke=CloudSmokeReport(
                platform="pipecat-cloud",
                agent_name=plan.agent_name,
                platform_ready=True,
                relay_ready=True,
                session_smoke=session_green and not skip_session_smoke,
            ),
        )

    async def _session_smoke(
        self,
        plan: PipecatCloudPlan,
        artifacts: CloudArtifacts,
        relay: RelayCredential,
    ) -> bool:
        session_id: str | None = None
        client = self._relay_client_factory(plan.relay_url, relay)
        try:
            await client.open()
            try:
                started = self.runner.run(
                    [
                        "cloud",
                        "agent",
                        "start",
                        plan.agent_name,
                        "--organization",
                        plan.organization,
                        "--use-daily",
                        "--force",
                    ],
                    cwd=artifacts.context,
                    timeout_s=300,
                )
                session_id = _session_id(started.stdout)
                if session_id is None:
                    raise VoiceyError(
                        "VY-DEP-004",
                        detail="Pipecat Cloud session smoke omitted its session id.",
                    )
                call_id = f"pcc_{session_id}"
                await _wait_relay_call(
                    client,
                    call_id,
                    timeout_s=self._smoke_claim_timeout_s,
                    poll_interval_s=self._smoke_poll_interval_s,
                    terminal=False,
                    failure="Pipecat Cloud session did not begin through the results relay.",
                )
            finally:
                if session_id is not None:
                    self.runner.run(
                        [
                            "cloud",
                            "agent",
                            "stop",
                            plan.agent_name,
                            "--session-id",
                            session_id,
                            "--organization",
                            plan.organization,
                            "--force",
                        ],
                        cwd=artifacts.context,
                        timeout_s=120,
                    )
            await _wait_relay_call(
                client,
                f"pcc_{session_id}",
                timeout_s=self._smoke_terminal_timeout_s,
                poll_interval_s=self._smoke_poll_interval_s,
                terminal=True,
                failure="Pipecat Cloud session stop produced no terminal relay event.",
            )
            return True
        finally:
            await client.close()

    def rollback_created(self, plan: PipecatCloudPlan) -> CloudResourceState:
        state = self.store.load()
        if state is None:
            raise VoiceyError("VY-DEP-010", detail="no Pipecat Cloud ledger exists.")
        state.validate(
            platform="pipecat-cloud",
            agent_name=plan.agent_name,
            account_scope=plan.organization,
            region=plan.region,
            relay_url=plan.relay_url,
            relay_fingerprint=state.relay_fingerprint,
        )
        if not state.agent_created:
            raise VoiceyError(
                "VY-DEP-010",
                detail="adopted Pipecat Cloud agents cannot be deleted by voicey.",
            )
        self.runner.run(
            [
                "cloud",
                "agent",
                "delete",
                plan.agent_name,
                "--organization",
                plan.organization,
                "--force",
            ],
            cwd=self.artifacts_directory,
            timeout_s=300,
        )
        state = state.checkpoint(
            agent_created=False,
            deployed=False,
            platform_ready=False,
            rolled_back=True,
        )
        self.store.save(state)
        return state

    @property
    def artifacts_directory(self) -> Path:
        return self.project_root / ".voicey" / "deploy" / "pipecat-cloud" / "context"


class LiveKitCloudDeploymentManager:
    """Create/adopt, deploy, validate, and roll back a LiveKit Cloud agent."""

    def __init__(
        self,
        project_root: Path,
        *,
        runner: CloudCommandRunner | None = None,
        relay_client_factory: (Callable[[str, RelayCredential], RelayClient] | None) = None,
        session_smoke_runner: LiveKitCloudSessionSmoke | None = None,
    ) -> None:
        self.project_root = project_root.resolve()
        self.runner = runner or PlatformCliRunner("lk")
        self.store = CloudResourceStore(self.project_root, "livekit-cloud")
        self.artifacts = CloudArtifactGenerator(self.project_root)
        self._relay_client_factory = relay_client_factory or RelayClient
        self._session_smoke_runner = session_smoke_runner or LiveKitCloudSessionSmoke(
            relay_client_factory=relay_client_factory,
        )

    async def deploy(
        self,
        plan: LiveKitCloudPlan,
        *,
        environment: Mapping[str, str],
        engine_wheel: Path | None,
        adopt: bool = False,
        skip_session_smoke: bool = False,
        smoke_to: str | None = None,
    ) -> CloudDeploymentReport:
        manifest = ManifestStore(self.project_root / "voicey.jsonc").load()
        agent = _load_agent(self.project_root, manifest)
        _require_agent_identity(agent, plan.agent_name)
        artifacts = self.artifacts.generate(
            "livekit-cloud",
            engine_wheel=engine_wheel,
            agent_name=plan.agent_name,
            region=plan.region,
        )
        relay, worker_secrets = _worker_secrets(
            self.project_root,
            plan.relay_url,
            "livekit",
            manifest,
            agent,
            environment,
        )
        relay_fingerprint = _fingerprint(relay.reveal())
        await _validate_relay(
            plan.relay_url,
            relay,
            client_factory=self._relay_client_factory,
        )
        state = self.store.load()
        if state is None:
            state = CloudResourceState.initial(
                platform="livekit-cloud",
                agent_name=plan.agent_name,
                account_scope=plan.project,
                region=plan.region,
                relay_url=plan.relay_url,
                relay=relay,
                relay_fingerprint=relay_fingerprint,
                artifact_digest=artifacts.digest,
            )
            self.store.save(state)
        else:
            state.validate(
                platform="livekit-cloud",
                agent_name=plan.agent_name,
                account_scope=plan.project,
                region=plan.region,
                relay_url=plan.relay_url,
                relay_fingerprint=relay_fingerprint,
            )
            state = state.checkpoint(artifact_digest=artifacts.digest)
            self.store.save(state)

        projects = self.runner.run(
            ["project", "list", "--json"],
            cwd=artifacts.context,
            timeout_s=60,
        )
        _require_livekit_project(projects.stdout, plan.project)
        config = artifacts.context / "livekit.toml"
        if state.agent_id is not None and not config.exists():
            self.runner.run(
                [
                    "agent",
                    "config",
                    "--id",
                    state.agent_id,
                    "--project",
                    plan.project,
                    "--yes",
                ],
                cwd=artifacts.context,
                timeout_s=120,
            )
        elif state.agent_id is None and plan.agent_id is not None:
            if not adopt:
                raise VoiceyError(
                    "VY-DEP-010",
                    detail="LiveKit Cloud agent id requires explicit --adopt.",
                )
            self.runner.run(
                [
                    "agent",
                    "config",
                    "--id",
                    plan.agent_id,
                    "--project",
                    plan.project,
                    "--yes",
                ],
                cwd=artifacts.context,
                timeout_s=120,
            )
            state = state.checkpoint(agent_id=plan.agent_id, agent_adopted=True)
            self.store.save(state)
        elif state.agent_id is None and config.exists():
            raise VoiceyError(
                "VY-DEP-010",
                detail="unledgered LiveKit agent config requires an explicit agent id.",
            )

        versions_before = ""
        if state.agent_id is not None:
            versions = self.runner.run(
                [
                    "agent",
                    "versions",
                    "--id",
                    state.agent_id,
                    "--project",
                    plan.project,
                ],
                cwd=artifacts.context,
                timeout_s=60,
            )
            versions_before = _current_version(versions.stdout) or ""

        with _secret_file(worker_secrets) as secret_file:
            if state.agent_id is None:
                created = self.runner.run(
                    [
                        "agent",
                        "create",
                        str(artifacts.context),
                        "--secrets-file",
                        str(secret_file),
                        "--region",
                        plan.region,
                        "--silent",
                        "--project",
                        plan.project,
                        "--yes",
                    ],
                    cwd=artifacts.context,
                    timeout_s=1800,
                )
                agent_id = _livekit_agent_id(config, created.stdout)
                state = state.checkpoint(
                    agent_created=True,
                    agent_id=agent_id,
                    secrets_synced=True,
                    deployed=True,
                )
            else:
                self.runner.run(
                    [
                        "agent",
                        "deploy",
                        str(artifacts.context),
                        "--secrets-file",
                        str(secret_file),
                        "--region",
                        plan.region,
                        "--silent",
                        "--project",
                        plan.project,
                        "--yes",
                    ],
                    cwd=artifacts.context,
                    timeout_s=1800,
                )
                state = state.checkpoint(
                    previous_version=versions_before or state.previous_version,
                    secrets_synced=True,
                    deployed=True,
                )
        self.store.save(state)
        status = self.runner.run(
            [
                "agent",
                "status",
                "--id",
                state.agent_id or "",
                "--project",
                plan.project,
            ],
            cwd=artifacts.context,
            timeout_s=120,
        )
        _require_ready(status.stdout, platform="LiveKit Cloud")
        session_green = False
        if not skip_session_smoke:
            session_green = await self._session_smoke_runner.run(
                agent=agent,
                relay_url=plan.relay_url,
                relay_credential=relay,
                environment=worker_secrets,
                to_number=smoke_to,
            )
        state = state.checkpoint(platform_ready=True, relay_ready=True)
        self.store.save(state)
        return CloudDeploymentReport(
            state=state,
            artifacts=artifacts,
            smoke=CloudSmokeReport(
                platform="livekit-cloud",
                agent_name=plan.agent_name,
                platform_ready=True,
                relay_ready=True,
                session_smoke=session_green,
            ),
        )

    def rollback(self, plan: LiveKitCloudPlan) -> CloudResourceState:
        state = self.store.load()
        if state is None or state.agent_id is None:
            raise VoiceyError("VY-DEP-010", detail="no LiveKit Cloud ledger exists.")
        state.validate(
            platform="livekit-cloud",
            agent_name=plan.agent_name,
            account_scope=plan.project,
            region=plan.region,
            relay_url=plan.relay_url,
            relay_fingerprint=state.relay_fingerprint,
        )
        cwd = self.project_root / ".voicey" / "deploy" / "livekit-cloud" / "context"
        if state.agent_created and state.previous_version is None:
            self.runner.run(
                [
                    "agent",
                    "delete",
                    "--id",
                    state.agent_id,
                    "--project",
                    plan.project,
                    "--yes",
                ],
                cwd=cwd,
                timeout_s=300,
            )
            state = state.checkpoint(agent_created=False, rolled_back=True)
        elif state.previous_version is not None:
            self.runner.run(
                [
                    "agent",
                    "rollback",
                    "--id",
                    state.agent_id,
                    "--version",
                    state.previous_version,
                    "--project",
                    plan.project,
                    "--yes",
                ],
                cwd=cwd,
                timeout_s=600,
            )
            state = state.checkpoint(
                deployed=True,
                platform_ready=True,
                previous_version=None,
                rolled_back=True,
            )
        else:
            raise VoiceyError(
                "VY-DEP-010",
                detail="adopted LiveKit agent has no ledgered previous version.",
            )
        self.store.save(state)
        return state


def _validate_common(agent_name: str, region: str, relay_url: str) -> None:
    parsed = urlsplit(relay_url.rstrip("/"))
    loopback = parsed.hostname in {"127.0.0.1", "localhost", "::1"}
    if (
        not _NAME.fullmatch(agent_name)
        or not _NAME.fullmatch(region)
        or parsed.scheme not in ({"http", "https"} if loopback else {"https"})
        or not parsed.hostname
        or parsed.query
        or parsed.fragment
        or parsed.username
        or parsed.password
    ):
        raise VoiceyError("VY-DEP-008", detail="cloud plan identity or relay URL is invalid.")


def _runtime_extras(manifest: ProjectManifest) -> str:
    extras = [manifest.runtime]
    for carrier in manifest.carriers:
        extra = "livekit" if carrier == "sip" else carrier
        if extra not in extras:
            extras.append(extra)
    return ",".join(extras)


def _stage_wheel(source_value: Path | None, destination: Path) -> Path | None:
    if source_value is None:
        if __version__.endswith(".dev0"):
            raise VoiceyError(
                "VY-DEP-008",
                detail="unpublished builds require --engine-wheel voicey-*.whl.",
            )
        return None
    source = source_value.expanduser().resolve()
    if (
        not source.is_file()
        or source.is_symlink()
        or source.suffix != ".whl"
        or not source.name.startswith("voicey-")
    ):
        raise VoiceyError("VY-DEP-008", detail="cloud engine wheel is invalid.")
    target = destination / source.name
    shutil.copyfile(source, target)
    target.chmod(0o644)
    return target


def _cloud_dockerfile(
    platform: CloudPlatform,
    *,
    wheel: Path | None,
    extras: str,
) -> str:
    package = (
        f'"/tmp/{wheel.name}[{extras}]"'
        if wheel is not None
        else f'"voicey[{extras}]=={__version__}"'
    )
    wheel_copy = f"COPY {wheel.name} /tmp/{wheel.name}\n" if wheel is not None else ""
    if platform == "pipecat-cloud":
        return f"""# syntax=docker/dockerfile:1.7
FROM {_PIPECAT_CLOUD_BASE} AS build
USER root
RUN python -m pip install --no-cache-dir uv==0.11.7 \\
    && python -m venv --system-site-packages /opt/voicey
{wheel_copy}COPY project-requirements.txt /tmp/project-requirements.txt
RUN uv pip install --python /opt/voicey/bin/python {package} \\
    && if [ -s /tmp/project-requirements.txt ]; then \\
         uv pip install --python /opt/voicey/bin/python -r /tmp/project-requirements.txt; \\
       fi

FROM {_PIPECAT_CLOUD_BASE} AS runtime
USER root
RUN if ! getent group 10001 >/dev/null; then \\
      groupadd --system --gid 10001 voicey; \\
    fi \\
    && if ! getent passwd 10001 >/dev/null; then \\
      useradd --system --uid 10001 --gid 10001 --home-dir /nonexistent \\
        --shell /usr/sbin/nologin voicey; \\
    fi
COPY --from=build /opt/voicey /opt/voicey
COPY --chown=10001:10001 project /voicey/project
COPY --chown=10001:10001 bot.py /app/bot.py
ENV PATH="/opt/voicey/bin:$PATH" \\
    PYTHONUNBUFFERED=1 \\
    PYTHONDONTWRITEBYTECODE=1 \\
    VOICEY_PROJECT_ROOT=/voicey/project \\
    PORT=8080
WORKDIR /app
USER 10001:10001
"""
    return f"""# syntax=docker/dockerfile:1.7
FROM python:3.14-slim-bookworm AS build
RUN python -m pip install --no-cache-dir uv==0.11.7 \\
    && python -m venv /opt/voicey
{wheel_copy}COPY project-requirements.txt /tmp/project-requirements.txt
RUN uv pip install --python /opt/voicey/bin/python {package} \\
    && if [ -s /tmp/project-requirements.txt ]; then \\
         uv pip install --python /opt/voicey/bin/python -r /tmp/project-requirements.txt; \\
       fi

FROM python:3.14-slim-bookworm AS runtime
RUN apt-get update \\
    && apt-get install --no-install-recommends -y ca-certificates \\
    && rm -rf /var/lib/apt/lists/* \\
    && groupadd --system --gid 10001 voicey \\
    && useradd --system --uid 10001 --gid 10001 --home-dir /app \\
         --shell /usr/sbin/nologin voicey
COPY --from=build /opt/voicey /opt/voicey
COPY project /app/project
CMD ["python", "-m", "voicey.deploy.cloud_runtime", "livekit"]
ENV PATH="/opt/voicey/bin:$PATH" \\
    PYTHONUNBUFFERED=1 \\
    PYTHONDONTWRITEBYTECODE=1 \\
    VOICEY_PROJECT_ROOT=/app/project
WORKDIR /app/project
USER 10001:10001
"""


def _pipecat_bot() -> str:
    return '''"""Generated Pipecat Cloud entrypoint. Do not add secrets here."""

from voicey.deploy.cloud_runtime import run_pipecat_cloud_session


async def bot(session_args: object) -> None:
    await run_pipecat_cloud_session(session_args)
'''


def _pcc_config(
    *,
    agent_name: str,
    image: str,
    secret_set: str,
    region: str,
    min_agents: int,
    max_agents: int,
    profile: str,
) -> str:
    return f"""agent_name = {json.dumps(agent_name)}
image = {json.dumps(image)}
secret_set = {json.dumps(secret_set)}
region = {json.dumps(region)}
agent_profile = {json.dumps(profile)}

[scaling]
min_agents = {min_agents}
max_agents = {max_agents}
"""


def _project_requirements(path: Path) -> str:
    try:
        payload = tomllib.loads(path.read_text(encoding="utf-8"))
        project = payload.get("project", {})
        dependencies = project.get("dependencies", [])
    except (OSError, tomllib.TOMLDecodeError, AttributeError) as exc:
        raise VoiceyError("VY-DEP-008", detail="project pyproject.toml is invalid.") from exc
    if not isinstance(dependencies, list):
        raise VoiceyError("VY-DEP-008", detail="project dependencies are invalid.")
    dependency_values = cast("list[object]", dependencies)
    if not all(isinstance(item, str) for item in dependency_values):
        raise VoiceyError("VY-DEP-008", detail="project dependencies are invalid.")
    return "".join(
        f"{item}\n"
        for item in cast("list[str]", dependency_values)
        if not re.match(r"^\s*voicey(?:\[|[<>=!~ @;]|$)", item, flags=re.IGNORECASE)
    )


def _worker_secrets(
    project_root: Path,
    relay_url: str,
    runtime: Literal["pipecat", "livekit"],
    manifest: ProjectManifest,
    agent: Agent,
    environment: Mapping[str, str],
) -> tuple[RelayCredential, dict[str, str]]:
    persisted = EnvFileStore(project_root / ".env").read()
    names = set(persisted)
    for axis in ("stt", "llm", "tts"):
        model_id = cast(str, getattr(agent.models, axis))
        entry = DEFAULT_PROVIDER_CATALOG.get(cast("object", axis), model_id)  # type: ignore[arg-type]
        if entry is not None:
            names.update(entry.key_env_vars)
    for carrier in manifest.carriers:
        entry = DEFAULT_PROVIDER_CATALOG.get("carrier", carrier)
        if entry is not None:
            names.update(entry.key_env_vars)
    combined = dict(environment) | persisted
    relay_value = combined.get("VOICEY_RELAY_CREDENTIAL", "").strip()
    relay = RelayCredential.parse(relay_value)
    excluded = {
        *_WORKER_SECRET_EXCLUSIONS,
        agent.results.secret_env,
        *(
            ()
            if agent.results.previous_secret_env is None
            else (agent.results.previous_secret_env,)
        ),
    }
    values = {
        name: combined[name]
        for name in sorted(names)
        if name not in excluded and _SECRET_NAME.fullmatch(name) and combined.get(name, "")
    }
    values.update(
        {
            "VOICEY_DEPLOY_TARGET": f"{runtime}-cloud",
            "VOICEY_RELAY_CREDENTIAL": relay_value,
            "VOICEY_RELAY_URL": relay_url.rstrip("/"),
            "VOICEY_RUNTIME": runtime,
        }
    )
    return relay, values


async def _validate_relay(
    relay_url: str,
    relay: RelayCredential,
    *,
    client_factory: Callable[[str, RelayCredential], RelayClient],
) -> None:
    client = client_factory(relay_url, relay)
    async with client:
        return


def _load_agent(project_root: Path, manifest: ProjectManifest) -> Agent:
    import importlib
    import sys

    text = str(project_root)
    sys.path.insert(0, text)
    try:
        module = importlib.import_module(manifest.agent_module)
        value: object = cast("object", module.agent)
    except (ImportError, AttributeError) as exc:
        raise VoiceyError(
            "VY-DEP-008",
            detail=f"{manifest.agent_module}.py must export an Agent named `agent`.",
        ) from exc
    finally:
        with suppress(ValueError):
            sys.path.remove(text)
    if not isinstance(value, Agent):
        raise VoiceyError("VY-DEP-008", detail="cloud project export is not an Agent.")
    return value


def _require_agent_identity(agent: Agent, expected_name: str) -> None:
    if agent.name != expected_name:
        raise VoiceyError(
            "VY-DEP-010",
            detail=(
                f"cloud plan selects {expected_name!r}, but the project exports "
                f"agent {agent.name!r}."
            ),
        )


def _require_livekit_project(output: str, expected_name: str) -> None:
    try:
        payload: object = json.loads(output)
    except json.JSONDecodeError as exc:
        raise VoiceyError(
            "VY-DEP-009",
            detail="LiveKit project list did not return JSON.",
        ) from exc
    projects: list[object]
    if isinstance(payload, list):
        projects = cast("list[object]", payload)
    elif isinstance(payload, dict):
        value = cast("dict[str, object]", payload).get("projects")
        projects = cast("list[object]", value) if isinstance(value, list) else []
    else:
        projects = []
    names = {item for item in projects if isinstance(item, str)} | {
        name
        for item in projects
        if isinstance(item, dict)
        for name in [cast("dict[str, object]", item).get("name")]
        if isinstance(name, str)
    }
    if expected_name not in names:
        raise VoiceyError(
            "VY-DEP-010",
            detail=f"authenticated LiveKit account does not contain project {expected_name!r}.",
        )


class _secret_file:
    def __init__(self, values: Mapping[str, str]) -> None:
        self.values = values
        self.path: Path | None = None

    def __enter__(self) -> Path:
        descriptor, name = tempfile.mkstemp(prefix="voicey-cloud-secrets-", text=True)
        self.path = Path(name)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                for key, value in sorted(self.values.items()):
                    if "\n" in value or "\r" in value:
                        raise VoiceyError(
                            "VY-DEP-008",
                            detail=f"secret {key} contains a line break.",
                        )
                    output.write(f"{key}={value}\n")
                output.flush()
                os.fsync(output.fileno())
            return self.path
        except Exception:
            with suppress(OSError):
                os.close(descriptor)
            self.path.unlink(missing_ok=True)
            raise

    def __exit__(self, *_exc: object) -> None:
        if self.path is not None:
            self.path.unlink(missing_ok=True)


def _require_secret_names(output: str, names: set[str]) -> None:
    missing = sorted(name for name in names if name not in output)
    if missing:
        raise VoiceyError(
            "VY-DEP-010",
            detail=f"platform secret sync lacks {', '.join(missing)}.",
        )


def _pipecat_agent_exists(result: CloudCommandResult) -> bool:
    normalized = re.sub(
        r"\x1b\[[0-9;]*m",
        "",
        f"{result.stdout}\n{result.stderr}",
    ).casefold()
    current_shape = re.search(r"(?m)^\s*agent:\s*\S+\s*$", normalized) is not None
    return (
        result.returncode == 0
        and "no deployment data found" not in normalized
        and ("status for agent" in normalized or current_shape)
    )


def _require_pipecat_image(output: str, image: str) -> None:
    normalized = re.sub(r"\x1b\[[0-9;]*m", "", output)
    if re.search(rf"(?m)^\s*Image:\s*{re.escape(image)}\s*$", normalized) is None:
        raise VoiceyError(
            "VY-DEP-010",
            detail="ledgered Pipecat Cloud deployment image does not match the platform.",
        )


def _require_ready(output: str, *, platform: str) -> None:
    normalized = re.sub(r"\x1b\[[0-9;]*m", "", output).casefold()
    positive = any(
        re.search(pattern, normalized) is not None
        for pattern in (
            r"(?m)^\s*ready:\s*true\s*$",
            r"(?m)^\s*deployment phase:\s*active\s*$",
            r"(?m)^\s*status:\s*(?:running|active|deployed|ready)\s*$",
            r"(?m)^\s*health:\s*ready\s*$",
        )
    )
    negative = any(
        re.search(pattern, normalized) is not None
        for pattern in (
            r"(?m)^\s*ready:\s*false\s*$",
            r"(?m)^\s*(?:deployment phase|status|health):\s*"
            r"(?:failed|unhealthy|stopped|error)\s*$",
        )
    )
    if not positive or negative:
        raise VoiceyError("VY-DEP-004", detail=f"{platform} did not report ready.")


def _require_region(output: str, region: str, *, platform: str) -> None:
    if re.search(rf"(?<![a-z0-9-]){re.escape(region)}(?![a-z0-9-])", output.casefold()) is None:
        raise VoiceyError(
            "VY-DEP-010",
            detail=f"{platform} account does not expose region {region!r}.",
        )


async def _wait_relay_call(
    client: RelayClient,
    call_id: str,
    *,
    timeout_s: float,
    poll_interval_s: float,
    terminal: bool,
    failure: str,
) -> None:
    deadline = asyncio.get_running_loop().time() + timeout_s
    while True:
        try:
            call = await client.get_call(call_id)
        except VoiceyError as exc:
            if exc.code != "VY-OBS-003":
                raise
            call = None
        if call is not None and (not terminal or call.ended_at is not None):
            return
        if asyncio.get_running_loop().time() >= deadline:
            raise VoiceyError("VY-DEP-004", detail=failure)
        await asyncio.sleep(poll_interval_s)


def _session_id(output: str) -> str | None:
    match = re.search(r"Session ID:\s*([A-Za-z0-9_-]+)", output, flags=re.IGNORECASE)
    return None if match is None else match.group(1)


def _current_version(output: str) -> str | None:
    for pattern in (
        r"(?im)^\s*[>*]?\s*(v?[A-Za-z0-9_.-]+)\s+.*\bcurrent\b",
        r"(?im)current(?:\s+version)?\s*[:=]\s*([A-Za-z0-9_.-]+)",
    ):
        match = re.search(pattern, output)
        if match is not None:
            return match.group(1)
    return None


def _livekit_agent_id(config: Path, output: str) -> str:
    if config.exists():
        try:
            payload = tomllib.loads(config.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise VoiceyError("VY-DEP-010", detail="livekit.toml is invalid.") from exc
        for key in ("id", "agent_id", "agentId"):
            value = payload.get(key)
            if isinstance(value, str) and _LIVEKIT_ID.fullmatch(value):
                return value
        agent = payload.get("agent")
        if isinstance(agent, dict):
            value = cast("dict[str, object]", agent).get("id")
            if isinstance(value, str) and _LIVEKIT_ID.fullmatch(value):
                return value
    match = re.search(r"\b(agent_[A-Za-z0-9_-]{6,})\b", output)
    if match is None:
        raise VoiceyError(
            "VY-DEP-010",
            detail="LiveKit create did not persist or report its agent id.",
        )
    return match.group(1)


def _tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        digest.update(str(path.relative_to(root)).encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _origin(value: str) -> str:
    parsed = urlsplit(value.rstrip("/"))
    return f"{parsed.scheme}://{parsed.netloc}"


def _fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _require_private_regular(path: Path) -> None:
    if path.is_symlink():
        raise VoiceyError("VY-SEC-002", detail=str(path))
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        raise VoiceyError("VY-SEC-001", detail=str(path))


def _atomic_write(path: Path, payload: bytes, *, mode: int) -> None:
    if path.is_symlink():
        raise VoiceyError("VY-SEC-002", detail=str(path))
    temporary: Path | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, name = tempfile.mkstemp(
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
        )
        temporary = Path(name)
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
