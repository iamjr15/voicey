"""PID-1 production supervisor for the generated Docker deployment."""

from __future__ import annotations

import asyncio
import importlib
import ipaddress
import os
import secrets
import signal
import sys
from collections.abc import Callable, Generator, Mapping
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from types import FrameType
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

import uvicorn
from fastapi import FastAPI, Request

from voicekit.config.manifest import ManifestStore, ProjectManifest
from voicekit.config.models import Agent
from voicekit.deploy.persistence import (
    PersistencePreflightReport,
    docker_persistence_preflight,
    rolling_generation_invariant,
)
from voicekit.errors import VoicekitError
from voicekit.obs.logging import configure_logging, get_logger
from voicekit.playground import (
    OriginPolicy,
    PlaygroundService,
    PlaygroundSettings,
    SessionTokenManager,
    WebSessionSecurity,
    embedded_frontend,
)
from voicekit.results import DeliveryWorker
from voicekit.runtimes.pipecat import PipecatHost, PipecatHostSettings
from voicekit.storage.sqlite import SQLiteRepository

if TYPE_CHECKING:
    from voicekit.telephony.telnyx import TelnyxAdapter
    from voicekit.telephony.twilio import TwilioAdapter

_LOG = get_logger(component="docker-runtime")


@dataclass(frozen=True, slots=True)
class ContainerSettings:
    """Validated environment-only container settings."""

    project_root: Path
    data_dir: Path
    public_base: str
    public_port: int
    admin_port: int
    admin_origin: str
    deploy_target: str
    storage_backend: str
    sqlite_local_only: bool
    replica_count: int
    trusted_proxy_ips: frozenset[str]
    trusted_proxy_cidrs: tuple[str, ...]
    integrator_secret: str | None

    @classmethod
    def from_environment(
        cls,
        environment: Mapping[str, str],
        *,
        project_root: Path,
    ) -> ContainerSettings:
        """Parse without retaining provider or webhook secret values."""
        public_base = environment.get("VOICEKIT_PUBLIC_BASE", "").rstrip("/")
        parsed = urlsplit(public_base)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.query
            or parsed.fragment
            or parsed.username
            or parsed.password
        ):
            raise VoicekitError(
                "VK-DEP-003",
                detail="VOICEKIT_PUBLIC_BASE must be an HTTPS base URL.",
            )
        data_dir = Path(environment.get("VOICEKIT_DATA_DIR", "/app/data"))
        if not data_dir.is_absolute():
            raise VoicekitError("VK-DEP-003", detail="VOICEKIT_DATA_DIR must be absolute.")
        public_port = _integer(environment, "VOICEKIT_PORT", default=7860)
        admin_port = _integer(environment, "VOICEKIT_ADMIN_PORT", default=7861)
        if not 1 <= public_port <= 65535 or not 1 <= admin_port <= 65535:
            raise VoicekitError("VK-DEP-003", detail="container ports must be in 1-65535.")
        admin_origin = environment.get("VOICEKIT_ADMIN_ORIGIN", "http://agent:7861")
        if not _is_origin(admin_origin):
            raise VoicekitError(
                "VK-DEP-003",
                detail="VOICEKIT_ADMIN_ORIGIN must be a normalized HTTP(S) origin.",
            )
        trusted_proxy_ips = _trusted_ips(environment.get("VOICEKIT_TRUSTED_PROXY_IPS", ""))
        trusted_proxy_cidrs = _trusted_cidrs(environment.get("VOICEKIT_TRUSTED_PROXY_CIDRS", ""))
        return cls(
            project_root=project_root.resolve(),
            data_dir=data_dir,
            public_base=public_base,
            public_port=public_port,
            admin_port=admin_port,
            admin_origin=admin_origin,
            deploy_target=environment.get("VOICEKIT_DEPLOY_TARGET", ""),
            storage_backend=environment.get("VOICEKIT_STORAGE_BACKEND", ""),
            sqlite_local_only=_flag(environment, "VOICEKIT_SQLITE_LOCAL_ONLY"),
            replica_count=_integer(environment, "VOICEKIT_REPLICA_COUNT", default=0),
            trusted_proxy_ips=trusted_proxy_ips,
            trusted_proxy_cidrs=trusted_proxy_cidrs,
            integrator_secret=environment.get("VOICEKIT_INTEGRATOR_SECRET") or None,
        )


class _ManagedServer(uvicorn.Server):
    """Let the supervisor own process signals across both listeners."""

    @contextmanager
    def capture_signals(self) -> Generator[None, None, None]:
        yield


async def run_container(
    *,
    environment: Mapping[str, str] | None = None,
    project_root: Path | None = None,
) -> None:
    """Preflight storage, load one project, and supervise listeners until drain."""
    values = dict(os.environ if environment is None else environment)
    root, settings, manifest = await asyncio.to_thread(
        _load_container_inputs,
        values,
        project_root,
    )
    if manifest.runtime != "pipecat":
        raise VoicekitError(
            "VK-DEP-003",
            detail="the P1 Docker runtime supports Pipecat projects; LiveKit lands in P2.",
        )
    preflight = await docker_persistence_preflight(
        settings.data_dir,
        deploy_target=settings.deploy_target,
        storage_backend=settings.storage_backend,
        sqlite_local_only=settings.sqlite_local_only,
        replica_count=settings.replica_count,
    )
    rolling = await rolling_generation_invariant(settings.data_dir)
    if not rolling.stale_writer_rejected or rolling.terminal_event_count != 1:
        raise VoicekitError("VK-DEP-002", detail="rolling-generation invariant did not hold.")

    with _project_import_path(root), _project_environment(values):
        agent = _load_agent(manifest.agent_module)
        await _serve(
            settings=settings,
            agent=agent,
            preflight=preflight,
            environment=values,
        )


async def _serve(
    *,
    settings: ContainerSettings,
    agent: Agent,
    preflight: PersistencePreflightReport,
    environment: Mapping[str, str],
) -> None:
    twilio: TwilioAdapter | None = None
    telnyx: TelnyxAdapter | None = None
    database_path = preflight.database_path
    async with SQLiteRepository(database_path) as repository:
        if agent.phone is not None:
            if agent.phone.provider == "twilio":
                from voicekit.telephony.twilio import TwilioAdapter

                twilio = TwilioAdapter(
                    account_sid=environment.get("TWILIO_ACCOUNT_SID"),
                    auth_token=environment.get("TWILIO_AUTH_TOKEN"),
                    ledger_path=settings.data_dir / "telephony.sqlite3",
                    expected_public_base=settings.public_base,
                    trusted_proxies=settings.trusted_proxy_ips,
                )
            elif agent.phone.provider == "telnyx":
                from voicekit.telephony.telnyx import TelnyxAdapter

                missing = [
                    name
                    for name in (
                        "TELNYX_API_KEY",
                        "TELNYX_PUBLIC_KEY",
                        "TELNYX_CONNECTION_ID",
                    )
                    if not environment.get(name)
                ]
                if missing:
                    raise VoicekitError(
                        "VK-DEP-003",
                        detail=f"Telnyx deployment is missing {', '.join(missing)}.",
                    )
                telnyx = TelnyxAdapter(
                    api_key=environment.get("TELNYX_API_KEY"),
                    public_key=environment.get("TELNYX_PUBLIC_KEY"),
                    connection_id=environment.get("TELNYX_CONNECTION_ID"),
                    ledger_path=settings.data_dir / "telephony.sqlite3",
                )
            else:
                raise VoicekitError(
                    "VK-DEP-003",
                    detail=f"carrier {agent.phone.provider!r} is not available in Docker.",
                )

        secret = _required_secret(environment, agent.results.secret_env)
        previous_secret = (
            None
            if agent.results.previous_secret_env is None
            else _required_secret(environment, agent.results.previous_secret_env)
        )
        tokens: SessionTokenManager | None = None
        web_security: WebSessionSecurity | None = None
        if agent.web.enabled:
            if settings.integrator_secret is None:
                raise VoicekitError(
                    "VK-DEP-003",
                    detail="web deployments require VOICEKIT_INTEGRATOR_SECRET.",
                )
            tokens = SessionTokenManager(
                secret,
                audience=_origin(settings.public_base),
                agent_name=agent.name,
                max_active=agent.limits.max_concurrent,
            )
            web_security = WebSessionSecurity(
                tokens,
                OriginPolicy(
                    allowed_origins=frozenset(
                        {
                            *agent.web.allowed_origins,
                            settings.admin_origin,
                        }
                    ),
                    expected_public_origin=_origin(settings.public_base),
                    trusted_proxy_cidrs=settings.trusted_proxy_cidrs,
                ),
            )

        host = PipecatHost(
            agent=agent,
            repository=repository,
            settings=PipecatHostSettings(
                public_base=settings.public_base,
                twilio_account_sid=environment.get("TWILIO_ACCOUNT_SID", ""),
                twilio_auth_token=environment.get("TWILIO_AUTH_TOKEN", ""),
                telnyx_api_key=environment.get("TELNYX_API_KEY", ""),
                storage_ready=preflight.schema_ready and preflight.artifact_round_trip,
            ),
            twilio=twilio,
            telnyx=telnyx,
            web_sessions=web_security,
        )
        delivery = DeliveryWorker(
            repository,
            owner_id=f"docker-{os.getpid()}",
            current_secret=secret,
            previous_secret=previous_secret,
        )
        try:
            with embedded_frontend() as frontend:
                admin_app = _admin_app(
                    settings=settings,
                    agent=agent,
                    host=host,
                    repository=repository,
                    tokens=tokens,
                    frontend=frontend,
                )
                await _supervise(
                    host=host,
                    admin_app=admin_app,
                    delivery=delivery,
                    settings=settings,
                )
        finally:
            await delivery.close()
            if twilio is not None:
                await asyncio.to_thread(twilio.ledger.close)
            if telnyx is not None:
                await asyncio.to_thread(telnyx.ledger.close)


def _admin_app(
    *,
    settings: ContainerSettings,
    agent: Agent,
    host: PipecatHost,
    repository: SQLiteRepository,
    tokens: SessionTokenManager | None,
    frontend: Path,
) -> FastAPI | None:
    if tokens is None:
        return None
    integrator_secret = settings.integrator_secret
    assert integrator_secret is not None

    async def authorize(request: Request) -> bool:
        supplied = request.headers.get("authorization", "")
        expected = f"Bearer {integrator_secret}"
        return secrets.compare_digest(supplied, expected)

    playground = PlaygroundService(
        agent=agent,
        public_app=host.app,
        repository=repository,
        tokens=tokens,
        settings=PlaygroundSettings(
            admin_origin=settings.admin_origin,
            public_origin=_origin(settings.public_base),
            frontend_dir=frontend,
            local_only=False,
        ),
        reserve_web_call=host.reserve_web_call,
        admin_authorizer=authorize,
    )
    return playground.admin_app


async def _supervise(
    *,
    host: PipecatHost,
    admin_app: FastAPI | None,
    delivery: DeliveryWorker,
    settings: ContainerSettings,
) -> None:
    public_server = _server(host.app, port=settings.public_port)
    servers = [public_server]
    if admin_app is not None:
        servers.append(_server(admin_app, port=settings.admin_port))
    server_tasks = [
        asyncio.create_task(server.serve(), name=f"voicekit-listener-{server.config.port}")
        for server in servers
    ]
    delivery_stop = asyncio.Event()
    delivery_task = asyncio.create_task(
        _delivery_loop(delivery, delivery_stop),
        name="voicekit-result-delivery",
    )
    shutdown = asyncio.Event()
    restore_signals = _install_signal_handlers(shutdown)
    shutdown_task = asyncio.create_task(shutdown.wait(), name="voicekit-signal-wait")
    try:
        await asyncio.gather(
            *(
                _wait_started(server, task)
                for server, task in zip(servers, server_tasks, strict=True)
            )
        )
        _LOG.info(
            "container_ready",
            runtime="pipecat",
            public_port=settings.public_port,
            admin_listener=admin_app is not None,
        )
        done, _ = await asyncio.wait(
            {*server_tasks, delivery_task, shutdown_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        unexpected = [task for task in done if task is not shutdown_task]
        if unexpected:
            await unexpected[0]
            raise VoicekitError("VK-DEP-003", detail="a container service exited early.")
    finally:
        await host.begin_drain()
        report = await host.drain(timeout_s=float(host.agent.limits.max_duration_s))
        _LOG.info(
            "container_drained",
            active_at_start=report.active_at_start,
            pending_at_start=report.pending_at_start,
            forced_sessions=report.forced_sessions,
            remaining_calls=report.remaining_calls,
        )
        delivery_stop.set()
        await delivery_task
        for server in servers:
            server.should_exit = True
        await asyncio.gather(*server_tasks, return_exceptions=True)
        shutdown_task.cancel()
        with suppress(asyncio.CancelledError):
            await shutdown_task
        restore_signals()


async def _delivery_loop(worker: DeliveryWorker, stop: asyncio.Event) -> None:
    while not stop.is_set():
        await worker.run_once()
        with suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=1)
    await worker.run_once()


def _server(app: FastAPI, *, port: int) -> _ManagedServer:
    return _ManagedServer(
        uvicorn.Config(
            app,
            host="0.0.0.0",
            port=port,
            log_level="info",
            proxy_headers=False,
            timeout_graceful_shutdown=30,
        )
    )


async def _wait_started(
    server: uvicorn.Server,
    task: asyncio.Task[None],
    *,
    timeout_s: float = 30,
) -> None:
    async def wait() -> None:
        while not server.started:
            if task.done():
                await task
                raise VoicekitError("VK-DEP-003", detail="container listener exited early.")
            await asyncio.sleep(0.02)

    try:
        await asyncio.wait_for(wait(), timeout=timeout_s)
    except TimeoutError as exc:
        raise VoicekitError(
            "VK-DEP-003",
            detail="container listener did not become ready.",
        ) from exc


def _install_signal_handlers(shutdown: asyncio.Event) -> Callable[[], None]:
    loop = asyncio.get_running_loop()
    originals = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}

    def handle(_signum: int, _frame: FrameType | None) -> None:
        loop.call_soon_threadsafe(shutdown.set)

    for sig in originals:
        signal.signal(sig, handle)

    def restore() -> None:
        for sig, handler in originals.items():
            signal.signal(sig, handler)

    return restore


def _load_container_inputs(
    environment: Mapping[str, str],
    project_root: Path | None,
) -> tuple[Path, ContainerSettings, ProjectManifest]:
    root = (project_root or Path.cwd()).resolve()
    settings = ContainerSettings.from_environment(environment, project_root=root)
    manifest = ManifestStore(root / "voicekit.jsonc").load()
    return root, settings, manifest


def _load_agent(module_name: str) -> Agent:
    try:
        module = importlib.import_module(module_name)
        agent: object = module.agent
    except (ImportError, AttributeError) as exc:
        raise VoicekitError(
            "VK-DEP-003",
            detail=f"{module_name}.py must export an Agent named `agent`.",
        ) from exc
    if not isinstance(agent, Agent):
        raise VoicekitError(
            "VK-DEP-003",
            detail=f"{module_name}.agent is not a voicekit Agent.",
        )
    return agent


@contextmanager
def _project_import_path(root: Path) -> Generator[None, None, None]:
    text = str(root)
    sys.path.insert(0, text)
    try:
        yield
    finally:
        with suppress(ValueError):
            sys.path.remove(text)


@contextmanager
def _project_environment(values: Mapping[str, str]) -> Generator[None, None, None]:
    original = {name: os.environ.get(name) for name in values}
    os.environ.update(values)
    try:
        yield
    finally:
        for name, value in original.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def _required_secret(environment: Mapping[str, str], name: str) -> str:
    value = environment.get(name, "")
    if not value:
        raise VoicekitError("VK-DEP-003", detail=f"required secret variable {name} is empty.")
    return value


def _integer(
    environment: Mapping[str, str],
    name: str,
    *,
    default: int,
) -> int:
    try:
        return int(environment.get(name, str(default)))
    except ValueError as exc:
        raise VoicekitError("VK-DEP-003", detail=f"{name} must be an integer.") from exc


def _flag(environment: Mapping[str, str], name: str) -> bool:
    return environment.get(name, "").casefold() in {"1", "true", "yes"}


def _trusted_ips(value: str) -> frozenset[str]:
    values = frozenset(item.strip() for item in value.split(",") if item.strip())
    try:
        for item in values:
            ipaddress.ip_address(item)
    except ValueError as exc:
        raise VoicekitError(
            "VK-DEP-003",
            detail="VOICEKIT_TRUSTED_PROXY_IPS contains an invalid address.",
        ) from exc
    return values


def _trusted_cidrs(value: str) -> tuple[str, ...]:
    values = tuple(item.strip() for item in value.split(",") if item.strip())
    try:
        for item in values:
            ipaddress.ip_network(item, strict=False)
    except ValueError as exc:
        raise VoicekitError(
            "VK-DEP-003",
            detail="VOICEKIT_TRUSTED_PROXY_CIDRS contains an invalid network.",
        ) from exc
    return values


def _is_origin(value: str) -> bool:
    parsed = urlsplit(value)
    return bool(
        parsed.scheme in {"http", "https"}
        and parsed.hostname
        and parsed.path in {"", "/"}
        and not parsed.query
        and not parsed.fragment
        and parsed.username is None
        and parsed.password is None
        and value == value.rstrip("/")
    )


def _origin(value: str) -> str:
    parsed = urlsplit(value)
    if not parsed.scheme or not parsed.netloc:
        raise VoicekitError("VK-DEP-003", detail="public base has no origin.")
    return f"{parsed.scheme}://{parsed.netloc}"


def main() -> None:
    """Module entrypoint used by the generated image."""
    configure_logging(format="json")
    try:
        asyncio.run(run_container())
    except VoicekitError as exc:
        _LOG.error("container_start_failed", error_code=exc.code, detail=exc.detail)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
