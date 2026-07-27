"""Deterministic Docker artifacts, validation, and live smoke entrypoint."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import cast
from urllib.parse import urlsplit

import httpx

from voicekit._version import __version__
from voicekit.config.manifest import ManifestStore
from voicekit.errors import VoicekitError

_WHEEL_NAME = re.compile(r"^voicekit-[A-Za-z0-9_.+!-]+-.*\.whl$")


@dataclass(frozen=True, slots=True)
class DockerArtifacts:
    """Files emitted for the canonical self-host target."""

    dockerfile: Path
    compose: Path
    dockerignore: Path
    environment_example: Path
    project_requirements: Path
    engine_wheel: Path | None


@dataclass(frozen=True, slots=True)
class DockerSmokeResult:
    """Safe endpoint facts collected before an optional paid call is placed."""

    url: str
    runtime: str
    active_calls: int
    accepting: bool
    storage_ready: bool


class DockerDeploymentGenerator:
    """Emit idempotent artifacts without overwriting user-authored files."""

    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root.resolve()

    def generate(self, *, engine_wheel: Path | None = None) -> DockerArtifacts:
        manifest = ManifestStore(self.project_root / "voicekit.jsonc").load()
        extras = ",".join(dict.fromkeys((manifest.runtime, *manifest.carriers)))
        package_spec = f"voicekit[{extras}]=={__version__}"
        wheel_destination = self._copy_wheel(engine_wheel)
        artifacts = DockerArtifacts(
            dockerfile=self.project_root / "Dockerfile.voicekit",
            compose=self.project_root / "compose.voicekit.yaml",
            dockerignore=self.project_root / ".dockerignore",
            environment_example=self.project_root / "docker.env.example",
            project_requirements=(
                self.project_root / ".voicekit" / "deploy" / "project-requirements.txt"
            ),
            engine_wheel=wheel_destination,
        )
        rendered = {
            artifacts.dockerfile: _dockerfile(
                package_spec=package_spec,
                wheel_extras=extras,
            ),
            artifacts.compose: _compose(),
            artifacts.dockerignore: _dockerignore(),
            artifacts.environment_example: _environment_example(),
            artifacts.project_requirements: self._project_requirements(),
        }
        for path, payload in rendered.items():
            _write_without_overwrite(path, payload)
        return artifacts

    def validate(self, artifacts: DockerArtifacts) -> None:
        executable = shutil.which("docker")
        if executable is None:
            raise VoicekitError("VK-DEP-005", detail="the `docker` executable is unavailable.")
        try:
            completed = subprocess.run(
                [
                    executable,
                    "compose",
                    "--project-directory",
                    str(self.project_root),
                    "-f",
                    str(artifacts.compose),
                    "config",
                    "--quiet",
                    "--no-env-resolution",
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=30,
                env={
                    **os.environ,
                    "VOICEKIT_PUBLIC_BASE": os.environ.get(
                        "VOICEKIT_PUBLIC_BASE",
                        "https://validation.invalid",
                    ),
                },
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise VoicekitError("VK-DEP-005", detail=type(exc).__name__) from exc
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout).strip()
            raise VoicekitError(
                "VK-DEP-005",
                detail=f"Compose validation failed: {detail[:500]}",
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
        if not source.is_file() or source.is_symlink() or not _WHEEL_NAME.fullmatch(source.name):
            raise VoicekitError(
                "VK-DEP-003",
                detail="--engine-wheel must be a regular voicekit-*.whl file.",
            )
        destination = self.project_root / ".voicekit" / "deploy" / source.name
        try:
            payload = source.read_bytes()
        except OSError as exc:
            raise VoicekitError("VK-DEP-003", detail=f"could not read {source}.") from exc
        _write_bytes_without_overwrite(destination, payload)
        return destination

    def _project_requirements(self) -> str:
        path = self.project_root / "pyproject.toml"
        try:
            payload = tomllib.loads(path.read_text(encoding="utf-8"))
            project_value: object = payload.get("project", {})
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise VoicekitError(
                "VK-DEP-003",
                detail=f"could not read project dependencies from {path}.",
            ) from exc
        if not isinstance(project_value, dict):
            raise VoicekitError("VK-DEP-003", detail="pyproject [project] must be a table.")
        project = cast("dict[str, object]", project_value)
        dependencies_value = project.get("dependencies", [])
        if not isinstance(dependencies_value, list):
            raise VoicekitError(
                "VK-DEP-003",
                detail="pyproject [project].dependencies must be a string array.",
            )
        dependencies = cast("list[object]", dependencies_value)
        if not all(isinstance(value, str) for value in dependencies):
            raise VoicekitError(
                "VK-DEP-003",
                detail="pyproject [project].dependencies must be a string array.",
            )
        external = [
            value
            for value in cast("list[str]", dependencies)
            if not re.match(r"^\s*voicekit(?:\[|[<>=!~ @;]|$)", value, flags=re.IGNORECASE)
        ]
        return "".join(f"{value}\n" for value in external)


class DockerSmokeVerifier:
    """Verify production readiness before the carrier smoke call is placed."""

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._client = client

    async def verify(self, url: str, *, timeout_s: float = 15) -> DockerSmokeResult:
        normalized = _smoke_base(url)
        owns_client = self._client is None
        client = self._client or httpx.AsyncClient(timeout=timeout_s)
        try:
            response = await client.get(f"{normalized}/health")
            response.raise_for_status()
            payload_value: object = response.json()
        except (httpx.HTTPError, json.JSONDecodeError, TypeError, ValueError) as exc:
            raise VoicekitError(
                "VK-DEP-004",
                detail=f"{normalized}/health did not return a ready voicekit host.",
            ) from exc
        finally:
            if owns_client:
                await client.aclose()
        if not isinstance(payload_value, dict):
            raise VoicekitError(
                "VK-DEP-004",
                detail="health response is not a JSON object.",
            )
        payload = cast("dict[str, object]", payload_value)
        active_calls = payload.get("active_calls")
        if (
            payload.get("ok") is not True
            or payload.get("accepting") is not True
            or payload.get("storage_ready") is not True
            or payload.get("runtime") not in {"pipecat", "livekit"}
            or not isinstance(active_calls, int)
        ):
            raise VoicekitError(
                "VK-DEP-004",
                detail="health response lacks ready runtime, drain, or persistence evidence.",
            )
        return DockerSmokeResult(
            url=normalized,
            runtime=str(payload["runtime"]),
            active_calls=active_calls,
            accepting=bool(payload["accepting"]),
            storage_ready=bool(payload["storage_ready"]),
        )


def _smoke_base(value: str) -> str:
    parsed = urlsplit(value.rstrip("/"))
    loopback = parsed.hostname in {"127.0.0.1", "localhost", "::1"}
    if (
        parsed.scheme not in ({"https", "http"} if loopback else {"https"})
        or not parsed.hostname
        or parsed.query
        or parsed.fragment
        or parsed.username
        or parsed.password
    ):
        raise VoicekitError(
            "VK-DEP-004",
            detail="--smoke must be an HTTPS base URL (HTTP is loopback-only).",
        )
    return value.rstrip("/")


def _write_without_overwrite(path: Path, payload: str) -> None:
    if path.exists():
        try:
            if path.is_file() and not path.is_symlink() and path.read_text("utf-8") == payload:
                return
        except OSError:
            pass
        raise VoicekitError("VK-DEP-001", detail=str(path))
    _atomic_write(path, payload.encode("utf-8"), mode=0o644)


def _write_bytes_without_overwrite(path: Path, payload: bytes) -> None:
    if path.exists():
        try:
            if path.is_file() and not path.is_symlink() and path.read_bytes() == payload:
                return
        except OSError:
            pass
        raise VoicekitError("VK-DEP-001", detail=str(path))
    _atomic_write(path, payload, mode=0o600)


def _atomic_write(path: Path, payload: bytes, *, mode: int) -> None:
    temporary: Path | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, name = tempfile.mkstemp(
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
        )
        temporary = Path(name)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.chmod(mode)
        os.replace(temporary, path)
    except OSError as exc:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        raise VoicekitError("VK-DEP-001", detail=f"could not write {path}: {exc}") from exc


def _dockerfile(*, package_spec: str, wheel_extras: str) -> str:
    wheel_find = (
        "find /app/.voicekit/deploy -maxdepth 1 -name 'voicekit-*.whl' -print -quit 2>/dev/null"
    )
    health = (
        "import urllib.request; "
        "urllib.request.urlopen('http://127.0.0.1:7860/health', timeout=3).read()"
    )
    return f"""# syntax=docker/dockerfile:1.7
# Generated by voicekit {__version__}. Secrets are runtime-only.
FROM python:3.14-slim-bookworm AS build
ARG VOICEKIT_PACKAGE="{package_spec}"
RUN python -m pip install --no-cache-dir uv==0.11.7 \\
    && python -m venv /opt/voicekit
WORKDIR /app
COPY . /app
RUN wheel="$({wheel_find})" \\
    && if [ -n "$wheel" ]; then \\
         uv pip install --python /opt/voicekit/bin/python "${{wheel}}[{wheel_extras}]" \\
           -r /app/.voicekit/deploy/project-requirements.txt; \\
       else \\
         uv pip install --python /opt/voicekit/bin/python "$VOICEKIT_PACKAGE" \\
           -r /app/.voicekit/deploy/project-requirements.txt; \\
       fi \\
    && mkdir -p /opt/nltk_data \\
    && NLTK_DATA=/opt/nltk_data \\
       /opt/voicekit/bin/python -m nltk.downloader -q -d /opt/nltk_data punkt_tab \\
    && test -f /opt/nltk_data/tokenizers/punkt_tab/english/abbrev_types.txt

FROM python:3.14-slim-bookworm AS runtime
RUN apt-get update \\
    && apt-get install --no-install-recommends -y ca-certificates libgomp1 \\
    && rm -rf /var/lib/apt/lists/* \\
    && groupadd --system --gid 10001 voicekit \\
    && useradd --system --uid 10001 --gid 10001 --home-dir /app --shell /usr/sbin/nologin voicekit
WORKDIR /app
COPY --from=build /opt/voicekit /opt/voicekit
COPY --from=build /opt/nltk_data /opt/nltk_data
COPY --chown=10001:10001 . /app
RUN rm -rf /app/.voicekit/deploy \\
    && mkdir -p /app/data /app/data/cache \\
    && chown -R 10001:10001 /app/data
ENV PATH="/opt/voicekit/bin:$PATH" \\
    PYTHONUNBUFFERED="1" \\
    PYTHONDONTWRITEBYTECODE="1" \\
    XDG_CACHE_HOME="/app/data/cache" \\
    NLTK_DATA="/opt/nltk_data"
USER 10001:10001
EXPOSE 7860
HEALTHCHECK --interval=15s --timeout=5s --start-period=30s --retries=4 \\
  CMD ["python", "-c", "{health}"]
CMD ["python", "-m", "voicekit.deploy.runtime"]
"""


def _compose() -> str:
    health = (
        "import urllib.request; "
        "urllib.request.urlopen('http://127.0.0.1:7860/health', timeout=3).read()"
    )
    payload = """name: voicekit
services:
  agent:
    image: ${VOICEKIT_IMAGE:-voicekit-agent:local}
    build:
      context: .
      dockerfile: Dockerfile.voicekit
    init: true
    restart: unless-stopped
    read_only: true
    cap_drop:
      - ALL
    security_opt:
      - no-new-privileges:true
    pids_limit: 512
    env_file:
      - path: ./.env
        required: true
    environment:
      VOICEKIT_DEPLOY_TARGET: docker
      VOICEKIT_STORAGE_BACKEND: sqlite
      VOICEKIT_SQLITE_LOCAL_ONLY: "1"
      VOICEKIT_REPLICA_COUNT: "1"
      VOICEKIT_DATA_DIR: /app/data
      VOICEKIT_PORT: "7860"
      VOICEKIT_ADMIN_PORT: "7861"
      VOICEKIT_PUBLIC_BASE: ${VOICEKIT_PUBLIC_BASE:?set VOICEKIT_PUBLIC_BASE}
      VOICEKIT_ADMIN_ORIGIN: ${VOICEKIT_ADMIN_ORIGIN:-http://agent:7861}
    ports:
      - "${VOICEKIT_PORT:-7860}:7860"
    expose:
      - "7861"
    volumes:
      - voicekit-data:/app/data
    tmpfs:
      - /tmp:rw,noexec,nosuid,nodev,size=128m
    healthcheck:
      test:
        - CMD
        - python
        - -c
        - "__VOICEKIT_HEALTH_COMMAND__"
      interval: 15s
      timeout: 5s
      start_period: 30s
      retries: 4
    stop_grace_period: ${VOICEKIT_STOP_GRACE_PERIOD:-14460s}

volumes:
  voicekit-data:
    driver: local
"""
    return payload.replace("__VOICEKIT_HEALTH_COMMAND__", health)


def _dockerignore() -> str:
    return """.git
.github
.venv
__pycache__
*.py[cod]
.pytest_cache
.ruff_cache
.coverage
htmlcov
dist
build
playground-web
.env*
!.env.example
.voicekit/*
!.voicekit/deploy
.voicekit/deploy/*
!.voicekit/deploy/voicekit-*.whl
!.voicekit/deploy/project-requirements.txt
"""


def _environment_example() -> str:
    return """# Deployment setting reference. Inject at runtime; never commit .env.
VOICEKIT_PUBLIC_BASE=https://voice.example.com
VOICEKIT_PORT=7860
VOICEKIT_STOP_GRACE_PERIOD=14460s
# Required only when agent.web.enabled=true; used by the internal token/admin API.
VOICEKIT_INTEGRATOR_SECRET=
# Exact reverse-proxy peer IPs for Twilio signature reconstruction.
VOICEKIT_TRUSTED_PROXY_IPS=127.0.0.1,::1
# CIDRs allowed to supply browser X-Forwarded-* headers.
VOICEKIT_TRUSTED_PROXY_CIDRS=127.0.0.0/8,::1/128
# Required by `voicekit deploy docker --smoke URL --to E164`.
VOICEKIT_SMOKE_TO=
"""
