"""PID-1 runtime for Fly/Railway user-owned results companions."""

from __future__ import annotations

import argparse
import asyncio
import os
import signal
import socket
import sys
import tempfile
from collections.abc import Callable, Generator, Mapping, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from pathlib import Path
from types import FrameType
from typing import cast

import uvicorn
from pydantic import ValidationError

from voicey.config.models import Observability
from voicey.deploy.managed import ManagedPersistenceReport, managed_persistence_preflight
from voicey.errors import VoiceyError
from voicey.obs.logging import configure_logging, get_logger
from voicey.obs.telemetry import InstrumentedRepository, Telemetry, TelemetryServer
from voicey.relay.auth import RelayCredential, RelayKeyring
from voicey.relay.companion import (
    CompanionMaintenance,
    CompanionRepository,
    CompanionService,
    CompanionSettings,
)
from voicey.relay.postgres import PostgresRelayJournal
from voicey.relay.recording import (
    CallbackProvider,
    CarrierCallbackAdapter,
    CarrierCallbackIngress,
    parse_callback_providers,
)
from voicey.results.recording import (
    PipecatRecordingHandler,
    PlivoRecordingAdapter,
    TelnyxRecordingAdapter,
    TwilioRecordingAdapter,
    VobizRecordingAdapter,
)
from voicey.results.signing import WebhookSigner
from voicey.storage.models import ProviderCallState
from voicey.storage.postgres import PostgresRepository
from voicey.storage.repository import StorageRepository
from voicey.storage.s3 import S3ArtifactStore
from voicey.telephony.ledger import TelephonyLedger
from voicey.telephony.models import CallEvent

_LOG = get_logger(component="results-service")


@dataclass(frozen=True, slots=True)
class ResultsServiceSettings:
    """Strict environment contract; secret values are excluded from repr."""

    public_base: str
    database_url: str = field(repr=False)
    object_bucket: str
    object_prefix: str
    object_endpoint: str | None
    object_region: str | None
    object_access_key: str | None = field(repr=False)
    object_secret_key: str | None = field(repr=False)
    object_force_path_style: bool
    relay_current: str = field(repr=False)
    relay_previous: str | None = field(repr=False)
    result_current: str = field(repr=False)
    result_previous: str | None = field(repr=False)
    target: str
    storage_backend: str
    artifact_backend: str
    port: int
    pool_min: int
    pool_max: int
    db_connection_budget: int
    maintenance_interval_s: float
    drain_grace_s: float
    owner_id: str
    callback_providers: tuple[CallbackProvider, ...]
    observability: Observability

    @classmethod
    def from_environment(cls, environment: Mapping[str, str]) -> ResultsServiceSettings:
        """Parse every deployment input without defaulting a topology decision."""
        port = _integer(environment, "PORT", default=8080)
        pool_min = _integer(environment, "VOICEY_DB_POOL_MIN", default=1)
        pool_max = _integer(environment, "VOICEY_DB_POOL_MAX", default=5)
        connection_budget = _integer(
            environment,
            "VOICEY_DB_CONNECTION_BUDGET",
            default=20,
        )
        maintenance_interval = _number(
            environment,
            "VOICEY_MAINTENANCE_INTERVAL_S",
            default=1.0,
        )
        drain_grace = _number(
            environment,
            "VOICEY_DRAIN_GRACE_S",
            default=10.0,
        )
        if (
            not 1 <= port <= 65535
            or pool_min < 0
            or pool_max < 1
            or pool_min > pool_max
            or connection_budget < 2 * pool_max
            or not 0.1 <= maintenance_interval <= 60
            or not 0 <= drain_grace <= 300
        ):
            raise VoiceyError(
                "VY-DEP-003",
                detail="results-service port, pool budget, interval, or drain grace is invalid.",
            )
        owner = environment.get("VOICEY_RESULTS_OWNER", "").strip()
        if not owner:
            owner = f"results-{socket.gethostname()}-{os.getpid()}"
        try:
            observability = Observability(
                prometheus_enabled=_flag(environment, "VOICEY_PROMETHEUS_ENABLED"),
                prometheus_bind=environment.get(
                    "VOICEY_PROMETHEUS_BIND",
                    "127.0.0.1",
                ),
                prometheus_port=_integer(
                    environment,
                    "VOICEY_PROMETHEUS_PORT",
                    default=9464,
                ),
                prometheus_path=environment.get(
                    "VOICEY_PROMETHEUS_PATH",
                    "/metrics",
                ),
                otlp_endpoint=environment.get("VOICEY_OTLP_ENDPOINT") or None,
                otlp_headers_env=(
                    environment.get("VOICEY_OTLP_HEADERS_ENV")
                    or ("VOICEY_OTLP_HEADERS" if "VOICEY_OTLP_HEADERS" in environment else None)
                ),
            )
        except ValidationError as exc:
            raise VoiceyError(
                "VY-OBS-006",
                detail="the results-service observability environment is invalid.",
            ) from exc
        settings = cls(
            public_base=_required(environment, "VOICEY_PUBLIC_BASE"),
            database_url=_required_any(
                environment,
                ("VOICEY_DATABASE_URL", "DATABASE_URL"),
            ),
            object_bucket=_required(environment, "VOICEY_OBJECT_BUCKET"),
            object_prefix=environment.get("VOICEY_OBJECT_PREFIX", "voicey"),
            object_endpoint=environment.get("VOICEY_OBJECT_ENDPOINT") or None,
            object_region=environment.get("AWS_REGION") or None,
            object_access_key=environment.get("AWS_ACCESS_KEY_ID") or None,
            object_secret_key=environment.get("AWS_SECRET_ACCESS_KEY") or None,
            object_force_path_style=_flag(
                environment,
                "VOICEY_OBJECT_FORCE_PATH_STYLE",
            ),
            relay_current=_required(environment, "VOICEY_RELAY_CREDENTIAL"),
            relay_previous=environment.get("VOICEY_RELAY_PREVIOUS_CREDENTIAL") or None,
            result_current=_required(environment, "VOICEY_RESULTS_SECRET"),
            result_previous=environment.get("VOICEY_RESULTS_PREVIOUS_SECRET") or None,
            target=_required(environment, "VOICEY_DEPLOY_TARGET"),
            storage_backend=_required(environment, "VOICEY_STORAGE_BACKEND"),
            artifact_backend=_required(environment, "VOICEY_ARTIFACT_BACKEND"),
            port=port,
            pool_min=pool_min,
            pool_max=pool_max,
            db_connection_budget=connection_budget,
            maintenance_interval_s=maintenance_interval,
            drain_grace_s=drain_grace,
            owner_id=owner,
            callback_providers=parse_callback_providers(
                environment.get("VOICEY_CALLBACK_PROVIDERS", "")
            ),
            observability=observability,
        )
        CompanionSettings(
            public_base=settings.public_base,
            recovery_owner=settings.owner_id,
        )
        if (
            settings.target not in {"fly", "railway"}
            or settings.storage_backend != "postgres"
            or settings.artifact_backend != "s3"
        ):
            raise VoiceyError(
                "VY-DEP-002",
                detail="results-service requires fly-or-railway/postgres/s3 topology.",
            )
        settings.keyring()
        WebhookSigner(settings.result_current, settings.result_previous)
        _validate_callback_credentials(settings.callback_providers, environment)
        return settings

    def keyring(self) -> RelayKeyring:
        return RelayKeyring(
            current=RelayCredential.parse(self.relay_current),
            previous=(
                None if self.relay_previous is None else RelayCredential.parse(self.relay_previous)
            ),
        )


class _ManagedServer(uvicorn.Server):
    """Keep signal ownership in the results-service supervisor."""

    @contextmanager
    def capture_signals(self) -> Generator[None, None, None]:
        yield


async def run_results_service(
    *,
    environment: Mapping[str, str] | None = None,
) -> None:
    """Preflight managed persistence, then supervise relay and maintenance."""
    values = dict(os.environ if environment is None else environment)
    settings = ResultsServiceSettings.from_environment(values)
    repository = PostgresRepository(
        settings.database_url,
        min_size=settings.pool_min,
        max_size=settings.pool_max,
    )
    journal = PostgresRelayJournal(
        settings.database_url,
        min_size=settings.pool_min,
        max_size=settings.pool_max,
    )
    artifacts = S3ArtifactStore(
        settings.object_bucket,
        prefix=settings.object_prefix,
        endpoint_url=settings.object_endpoint,
        region_name=settings.object_region,
        access_key_id=settings.object_access_key,
        secret_access_key=settings.object_secret_key,
        force_path_style=settings.object_force_path_style,
    )
    async with repository, journal:
        report = await managed_persistence_preflight(
            dsn=settings.database_url,
            repository=repository,
            journal=journal,
            artifact_store=artifacts,
            target=settings.target,
            storage_backend=settings.storage_backend,
            artifact_backend=settings.artifact_backend,
        )
        telemetry = Telemetry(
            agent_name="results-service",
            runtime="companion",
            settings=settings.observability,
            environment=values,
        )
        instrumented_repository = InstrumentedRepository(repository, telemetry)
        if settings.observability.prometheus_enabled:
            await instrumented_repository.refresh_dlq_depth()
        companion_repository = cast("CompanionRepository", instrumented_repository)
        telemetry_server = TelemetryServer(telemetry)
        recording_runtime = _build_recording_runtime(
            settings,
            values,
            repository=cast("PostgresRepository", instrumented_repository),
            artifacts=artifacts,
        )
        service = CompanionService(
            companion_repository,
            journal,
            artifacts,
            keyring=settings.keyring(),
            current_result_secret=settings.result_current,
            previous_result_secret=settings.result_previous,
            settings=CompanionSettings(
                public_base=settings.public_base,
                recovery_owner=settings.owner_id,
            ),
            artifact_ready=artifacts.ready,
            callback_ingresses=recording_runtime.ingresses,
        )
        maintenance = CompanionMaintenance(
            companion_repository,
            artifacts,
            owner_id=settings.owner_id,
            current_result_secret=settings.result_current,
            previous_result_secret=settings.result_previous,
        )
        _log_preflight(report)
        try:
            await telemetry_server.start()
            await _supervise(
                service,
                maintenance,
                port=settings.port,
                maintenance_interval_s=settings.maintenance_interval_s,
                drain_grace_s=settings.drain_grace_s,
            )
        finally:
            await maintenance.close()
            await telemetry_server.stop()
            recording_runtime.close()


async def run_results_preflight(
    *,
    environment: Mapping[str, str] | None = None,
) -> ManagedPersistenceReport:
    """Apply/validate migrations and prove managed persistence without serving."""
    values = dict(os.environ if environment is None else environment)
    settings = ResultsServiceSettings.from_environment(values)
    repository = PostgresRepository(
        settings.database_url,
        min_size=settings.pool_min,
        max_size=settings.pool_max,
    )
    journal = PostgresRelayJournal(
        settings.database_url,
        min_size=settings.pool_min,
        max_size=settings.pool_max,
    )
    artifacts = S3ArtifactStore(
        settings.object_bucket,
        prefix=settings.object_prefix,
        endpoint_url=settings.object_endpoint,
        region_name=settings.object_region,
        access_key_id=settings.object_access_key,
        secret_access_key=settings.object_secret_key,
        force_path_style=settings.object_force_path_style,
    )
    async with repository, journal:
        report = await managed_persistence_preflight(
            dsn=settings.database_url,
            repository=repository,
            journal=journal,
            artifact_store=artifacts,
            target=settings.target,
            storage_backend=settings.storage_backend,
            artifact_backend=settings.artifact_backend,
        )
    _log_preflight(report)
    return report


@dataclass(slots=True)
class _RecordingRuntime:
    ingresses: tuple[CarrierCallbackIngress, ...]
    ledgers: tuple[TelephonyLedger, ...]
    temporary: tempfile.TemporaryDirectory[str] | None

    def close(self) -> None:
        for ledger in self.ledgers:
            ledger.close()
        if self.temporary is not None:
            self.temporary.cleanup()


def _build_recording_runtime(
    settings: ResultsServiceSettings,
    environment: Mapping[str, str],
    *,
    repository: PostgresRepository,
    artifacts: S3ArtifactStore,
) -> _RecordingRuntime:
    if not settings.callback_providers:
        return _RecordingRuntime(ingresses=(), ledgers=(), temporary=None)
    try:
        temporary = tempfile.TemporaryDirectory(prefix="voicey-recording-adapters-")
    except OSError as exc:
        raise VoiceyError(
            "VY-DEP-003",
            detail="recording adapter temporary storage is unavailable.",
        ) from exc
    ledgers: list[TelephonyLedger] = []
    adapters: dict[CallbackProvider, CarrierCallbackAdapter] = {}
    try:
        for provider in settings.callback_providers:
            ledger = TelephonyLedger(Path(temporary.name) / f"{provider}.sqlite3")
            ledgers.append(ledger)
            if provider == "twilio":
                from voicey.telephony.twilio import TwilioAdapter

                adapter: CarrierCallbackAdapter = TwilioAdapter(
                    account_sid=environment.get("TWILIO_ACCOUNT_SID"),
                    auth_token=environment.get("TWILIO_AUTH_TOKEN"),
                    ledger=ledger,
                    expected_public_base=settings.public_base,
                )
            elif provider == "telnyx":
                from voicey.telephony.telnyx import TelnyxAdapter

                adapter = TelnyxAdapter(
                    api_key=environment.get("TELNYX_API_KEY"),
                    public_key=environment.get("TELNYX_PUBLIC_KEY"),
                    connection_id=environment.get("TELNYX_CONNECTION_ID"),
                    ledger=ledger,
                )
            elif provider == "vobiz":
                from voicey.telephony.vobiz import VobizAdapter

                adapter = VobizAdapter(
                    auth_id=environment.get("VOBIZ_AUTH_ID"),
                    auth_token=environment.get("VOBIZ_AUTH_TOKEN"),
                    ledger=ledger,
                    expected_public_base=settings.public_base,
                )
            else:
                from voicey.telephony.plivo import PlivoAdapter

                adapter = PlivoAdapter(
                    auth_id=environment.get("PLIVO_AUTH_ID"),
                    auth_token=environment.get("PLIVO_AUTH_TOKEN"),
                    ledger=ledger,
                    expected_public_base=settings.public_base,
                )
            adapters[provider] = adapter
        handler = PipecatRecordingHandler(
            repository=cast("StorageRepository", repository),
            artifact_store=artifacts,
            access_base=settings.public_base,
            current_secret=settings.result_current,
            previous_secret=settings.result_previous,
            twilio=cast("TwilioRecordingAdapter | None", adapters.get("twilio")),
            telnyx=cast("TelnyxRecordingAdapter | None", adapters.get("telnyx")),
            vobiz=cast("VobizRecordingAdapter | None", adapters.get("vobiz")),
            plivo=cast("PlivoRecordingAdapter | None", adapters.get("plivo")),
        )
        handles = {
            "twilio": handler.handle_twilio,
            "telnyx": handler.handle_telnyx,
            "vobiz": handler.handle_vobiz,
            "plivo": handler.handle_plivo,
        }

        async def observe(event: CallEvent) -> None:
            await repository.record_provider_observation(
                event.provider_call_id,
                _provider_state(event.type),
            )

        ingresses = tuple(
            CarrierCallbackIngress(
                provider=provider,
                adapter=adapters[provider],
                handle=handles[provider],
                observe=observe,
            )
            for provider in settings.callback_providers
        )
        return _RecordingRuntime(
            ingresses=ingresses,
            ledgers=tuple(ledgers),
            temporary=temporary,
        )
    except ModuleNotFoundError as exc:
        for ledger in ledgers:
            ledger.close()
        temporary.cleanup()
        raise VoiceyError(
            "VY-TEL-001",
            detail="install voicey[companion] for recording ingress.",
        ) from exc
    except Exception:
        for ledger in ledgers:
            ledger.close()
        temporary.cleanup()
        raise


def _provider_state(event_type: str) -> ProviderCallState:
    if event_type == "completed":
        return "completed"
    if event_type == "failed":
        return "failed"
    return "active"


async def _supervise(
    service: CompanionService,
    maintenance: CompanionMaintenance,
    *,
    port: int,
    maintenance_interval_s: float,
    drain_grace_s: float,
) -> None:
    server = _ManagedServer(
        uvicorn.Config(
            service.app,
            host="0.0.0.0",
            port=port,
            log_level="info",
            proxy_headers=False,
            timeout_graceful_shutdown=30,
        )
    )
    server_task = asyncio.create_task(server.serve(), name="voicey-results-listener")
    maintenance_stop = asyncio.Event()
    maintenance_task = asyncio.create_task(
        _maintenance_loop(
            maintenance,
            maintenance_stop,
            interval_s=maintenance_interval_s,
        ),
        name="voicey-results-maintenance",
    )
    shutdown = asyncio.Event()
    restore_signals = _install_signal_handlers(shutdown)
    shutdown_task = asyncio.create_task(shutdown.wait(), name="voicey-results-signal")
    try:
        await _wait_started(server, server_task)
        _LOG.info("results_service_ready", port=port)
        done, _ = await asyncio.wait(
            {server_task, maintenance_task, shutdown_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        unexpected = [task for task in done if task is not shutdown_task]
        if unexpected:
            await unexpected[0]
            raise VoiceyError(
                "VY-DEP-003",
                detail="a results-service process exited early.",
            )
    finally:
        service.begin_drain()
        _LOG.info("results_service_draining", grace_s=drain_grace_s)
        if drain_grace_s:
            await asyncio.sleep(drain_grace_s)
        maintenance_stop.set()
        await maintenance_task
        try:
            await maintenance.run_once()
        except VoiceyError as exc:
            _LOG.error(
                "results_service_final_maintenance_failed",
                error_code=exc.code,
                detail=exc.detail,
            )
        server.should_exit = True
        await asyncio.gather(server_task, return_exceptions=True)
        shutdown_task.cancel()
        with suppress(asyncio.CancelledError):
            await shutdown_task
        restore_signals()
        _LOG.info("results_service_drained")


async def _maintenance_loop(
    maintenance: CompanionMaintenance,
    stop: asyncio.Event,
    *,
    interval_s: float,
) -> None:
    while not stop.is_set():
        try:
            report = await maintenance.run_once()
            if report.recovery.stale or report.delivery.claimed or report.purged_artifacts:
                _LOG.info(
                    "results_maintenance",
                    stale=report.recovery.stale,
                    terminalized=report.recovery.terminalized,
                    deferred=report.recovery.deferred,
                    delivery_claimed=report.delivery.claimed,
                    delivery_failed=report.delivery.failed,
                    purged_artifacts=report.purged_artifacts,
                )
        except VoiceyError as exc:
            _LOG.error(
                "results_maintenance_failed",
                error_code=exc.code,
                detail=exc.detail,
            )
        with suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=interval_s)


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
                raise VoiceyError(
                    "VY-DEP-003",
                    detail="results-service listener exited before readiness.",
                )
            await asyncio.sleep(0.02)

    try:
        await asyncio.wait_for(wait(), timeout=timeout_s)
    except TimeoutError as exc:
        raise VoiceyError(
            "VY-DEP-003",
            detail="results-service listener did not become ready.",
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


def _required(environment: Mapping[str, str], name: str) -> str:
    value = environment.get(name, "").strip()
    if not value:
        raise VoiceyError("VY-DEP-003", detail=f"{name} is required.")
    return value


def _validate_callback_credentials(
    providers: tuple[CallbackProvider, ...],
    environment: Mapping[str, str],
) -> None:
    required = {
        "twilio": ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN"),
        "telnyx": (
            "TELNYX_API_KEY",
            "TELNYX_PUBLIC_KEY",
            "TELNYX_CONNECTION_ID",
        ),
        "vobiz": ("VOBIZ_AUTH_ID", "VOBIZ_AUTH_TOKEN"),
        "plivo": ("PLIVO_AUTH_ID", "PLIVO_AUTH_TOKEN"),
    }
    missing = sorted(
        name
        for provider in providers
        for name in required[provider]
        if not environment.get(name, "").strip()
    )
    if missing:
        raise VoiceyError(
            "VY-DEP-003",
            detail=f"recording ingress is missing {', '.join(missing)}.",
        )


def _required_any(environment: Mapping[str, str], names: tuple[str, ...]) -> str:
    for name in names:
        value = environment.get(name, "").strip()
        if value:
            return value
    raise VoiceyError(
        "VY-DEP-003",
        detail=f"one of {', '.join(names)} is required.",
    )


def _integer(environment: Mapping[str, str], name: str, *, default: int) -> int:
    raw = environment.get(name)
    try:
        return default if raw is None else int(raw)
    except ValueError as exc:
        raise VoiceyError("VY-DEP-003", detail=f"{name} must be an integer.") from exc


def _number(environment: Mapping[str, str], name: str, *, default: float) -> float:
    raw = environment.get(name)
    try:
        return default if raw is None else float(raw)
    except ValueError as exc:
        raise VoiceyError("VY-DEP-003", detail=f"{name} must be a number.") from exc


def _flag(environment: Mapping[str, str], name: str) -> bool:
    value = environment.get(name, "").strip().casefold()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"", "0", "false", "no", "off"}:
        return False
    raise VoiceyError("VY-DEP-003", detail=f"{name} must be a boolean flag.")


def _log_preflight(report: ManagedPersistenceReport) -> None:
    _LOG.info(
        "results_persistence_ready",
        target=report.target,
        storage_backend=report.storage_backend,
        artifact_backend=report.artifact_backend,
        rolling_generation=report.rolling_generation,
        stale_writer_rejected=report.stale_writer_rejected,
        terminal_event_count=report.terminal_event_count,
    )


def main(argv: Sequence[str] = ()) -> None:
    configure_logging(format="json")
    parser = argparse.ArgumentParser(prog="voicey-results-service")
    parser.add_argument(
        "--preflight-only",
        action="store_true",
        help="apply migrations and verify Postgres/object storage, then exit",
    )
    arguments = parser.parse_args(list(argv))
    try:
        if arguments.preflight_only:
            asyncio.run(run_results_preflight())
        else:
            asyncio.run(run_results_service())
    except VoiceyError as exc:
        _LOG.error(
            "results_service_start_failed",
            error_code=exc.code,
            detail=exc.detail,
        )
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main(sys.argv[1:])
