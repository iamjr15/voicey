from __future__ import annotations

# pyright: reportPrivateUsage=false
import asyncio
import base64
from collections.abc import Callable
from pathlib import Path
from typing import ClassVar, cast

import pytest
import uvicorn
from fastapi import FastAPI

import voicekit.deploy.results_service as runtime
from voicekit.deploy.managed import ManagedPersistenceReport
from voicekit.errors import VoicekitError
from voicekit.relay import RelayCredential
from voicekit.relay.companion import MaintenanceRun
from voicekit.results import DeliveryRun, RecoveryRun, encode_secret
from voicekit.storage.postgres import PostgresRepository
from voicekit.storage.s3 import S3ArtifactStore
from voicekit.telephony.models import CallEvent

_CURRENT_RESULT = encode_secret(b"r" * 32)
_PREVIOUS_RESULT = encode_secret(b"p" * 32)


def _environment(*, callbacks: str = "") -> dict[str, str]:
    current = RelayCredential.issue("current-key").reveal()
    previous = RelayCredential.issue("previous-key").reveal()
    database_url = "postgresql://voicekit:test@db.example.test/voicekit"  # pragma: allowlist secret
    return {
        "VOICEKIT_PUBLIC_BASE": "https://results.example.test",
        "DATABASE_URL": database_url,
        "VOICEKIT_OBJECT_BUCKET": "voicekit-artifacts",
        "VOICEKIT_OBJECT_PREFIX": "production",
        "VOICEKIT_OBJECT_ENDPOINT": "https://objects.example.test",
        "AWS_REGION": "auto",
        "AWS_ACCESS_KEY_ID": "object-access",
        "AWS_SECRET_ACCESS_KEY": "object-secret",  # pragma: allowlist secret
        "VOICEKIT_OBJECT_FORCE_PATH_STYLE": "true",
        "VOICEKIT_RELAY_CREDENTIAL": current,
        "VOICEKIT_RELAY_PREVIOUS_CREDENTIAL": previous,
        "VOICEKIT_RESULTS_SECRET": _CURRENT_RESULT,
        "VOICEKIT_RESULTS_PREVIOUS_SECRET": _PREVIOUS_RESULT,
        "VOICEKIT_DEPLOY_TARGET": "fly",
        "VOICEKIT_STORAGE_BACKEND": "postgres",
        "VOICEKIT_ARTIFACT_BACKEND": "s3",
        "VOICEKIT_DB_POOL_MIN": "0",
        "VOICEKIT_DB_POOL_MAX": "2",
        "VOICEKIT_DB_CONNECTION_BUDGET": "4",
        "VOICEKIT_MAINTENANCE_INTERVAL_S": "0.5",
        "VOICEKIT_DRAIN_GRACE_S": "0",
        "VOICEKIT_RESULTS_OWNER": "results-test",
        "VOICEKIT_CALLBACK_PROVIDERS": callbacks,
        "PORT": "9090",
        "TWILIO_ACCOUNT_SID": f"AC{'a' * 32}",
        "TWILIO_AUTH_TOKEN": "twilio-token",
        "TELNYX_API_KEY": "telnyx-key",  # pragma: allowlist secret
        "TELNYX_PUBLIC_KEY": base64.b64encode(b"k" * 32).decode(),
        "TELNYX_CONNECTION_ID": "connection-1",
        "VOBIZ_AUTH_ID": "MA_test",
        "VOBIZ_AUTH_TOKEN": "vobiz-token",
        "PLIVO_AUTH_ID": f"MA{'a' * 18}",
        "PLIVO_AUTH_TOKEN": "plivo-token",
    }


def test_results_service_settings_parse_full_contract_and_error_paths() -> None:
    environment = _environment(callbacks="twilio,telnyx,vobiz,plivo")
    settings = runtime.ResultsServiceSettings.from_environment(environment)

    assert settings.port == 9090
    assert settings.pool_min == 0
    assert settings.pool_max == 2
    assert settings.object_force_path_style
    assert settings.callback_providers == ("twilio", "telnyx", "vobiz", "plivo")
    assert settings.keyring().previous is not None

    defaults = _environment()
    del defaults["VOICEKIT_RESULTS_OWNER"]
    del defaults["VOICEKIT_OBJECT_FORCE_PATH_STYLE"]
    default_settings = runtime.ResultsServiceSettings.from_environment(defaults)
    assert default_settings.owner_id.startswith("results-")
    assert not default_settings.object_force_path_style

    for name, value in (
        ("PORT", "0"),
        ("PORT", "not-an-integer"),
        ("VOICEKIT_MAINTENANCE_INTERVAL_S", "not-a-number"),
        ("VOICEKIT_OBJECT_FORCE_PATH_STYLE", "maybe"),
    ):
        invalid = {**environment, name: value}
        with pytest.raises(VoicekitError) as caught:
            runtime.ResultsServiceSettings.from_environment(invalid)
        assert caught.value.code == "VK-DEP-003"

    missing = dict(environment)
    del missing["VOICEKIT_PUBLIC_BASE"]
    with pytest.raises(VoicekitError) as required:
        runtime.ResultsServiceSettings.from_environment(missing)
    assert required.value.code == "VK-DEP-003"

    missing_dsn = dict(environment)
    del missing_dsn["DATABASE_URL"]
    with pytest.raises(VoicekitError) as database:
        runtime.ResultsServiceSettings.from_environment(missing_dsn)
    assert database.value.code == "VK-DEP-003"

    missing_callback = dict(environment)
    del missing_callback["TWILIO_AUTH_TOKEN"]
    with pytest.raises(VoicekitError) as callback:
        runtime.ResultsServiceSettings.from_environment(missing_callback)
    assert callback.value.code == "VK-DEP-003"

    topology = {**environment, "VOICEKIT_DEPLOY_TARGET": "railway"}
    with pytest.raises(VoicekitError) as incompatible:
        runtime.ResultsServiceSettings.from_environment(topology)
    assert incompatible.value.code == "VK-DEP-002"

    alternate_dsn = dict(environment)
    alternate_dsn["VOICEKIT_DATABASE_URL"] = alternate_dsn.pop("DATABASE_URL")
    assert (
        runtime.ResultsServiceSettings.from_environment(alternate_dsn).database_url
        == alternate_dsn["VOICEKIT_DATABASE_URL"]
    )


class _ObservationRepository:
    def __init__(self) -> None:
        self.observations: list[tuple[str, str]] = []

    async def record_provider_observation(self, call_id: str, state: str) -> None:
        self.observations.append((call_id, state))


def test_build_callback_runtime_all_providers_and_cleanup() -> None:
    settings = runtime.ResultsServiceSettings.from_environment(
        _environment(callbacks="twilio,telnyx,vobiz,plivo")
    )
    repository = _ObservationRepository()
    built = runtime._build_recording_runtime(
        settings,
        _environment(callbacks="twilio,telnyx,vobiz,plivo"),
        repository=cast("PostgresRepository", repository),
        artifacts=cast("S3ArtifactStore", object()),
    )
    temporary_path = Path(cast("runtime.tempfile.TemporaryDirectory[str]", built.temporary).name)

    assert [item.provider for item in built.ingresses] == [
        "twilio",
        "telnyx",
        "vobiz",
        "plivo",
    ]
    observe = built.ingresses[0].observe
    assert observe is not None

    async def invoke_observer() -> None:
        await observe(
            CallEvent(
                type="completed",
                provider_call_id="provider-call",
                provider_status="completed",
            )
        )

    asyncio.run(invoke_observer())
    assert repository.observations == [("provider-call", "completed")]
    assert runtime._provider_state("failed") == "failed"
    assert runtime._provider_state("ringing") == "active"

    built.close()
    assert not temporary_path.exists()
    empty = runtime._build_recording_runtime(
        runtime.ResultsServiceSettings.from_environment(_environment()),
        _environment(),
        repository=cast("PostgresRepository", repository),
        artifacts=cast("S3ArtifactStore", object()),
    )
    empty.close()
    assert not empty.ingresses


def test_build_callback_runtime_catalogues_setup_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = runtime.ResultsServiceSettings.from_environment(_environment(callbacks="twilio"))

    def no_temporary(*_args: object, **_kwargs: object) -> object:
        raise OSError("no temp")

    monkeypatch.setattr(runtime.tempfile, "TemporaryDirectory", no_temporary)
    with pytest.raises(VoicekitError) as temporary:
        runtime._build_recording_runtime(
            settings,
            _environment(callbacks="twilio"),
            repository=cast("PostgresRepository", _ObservationRepository()),
            artifacts=cast("S3ArtifactStore", object()),
        )
    assert temporary.value.code == "VK-DEP-003"


@pytest.mark.parametrize(
    ("failure", "code"),
    [
        (ModuleNotFoundError("optional dependency"), "VK-TEL-001"),
        (VoicekitError("VK-TEL-005", detail="ledger"), "VK-TEL-005"),
    ],
)
def test_build_callback_runtime_cleans_partial_adapter_failure(
    monkeypatch: pytest.MonkeyPatch,
    failure: Exception,
    code: str,
) -> None:
    settings = runtime.ResultsServiceSettings.from_environment(_environment(callbacks="twilio"))

    def fail_ledger(_path: Path) -> object:
        raise failure

    monkeypatch.setattr(runtime, "TelephonyLedger", fail_ledger)
    with pytest.raises(VoicekitError) as caught:
        runtime._build_recording_runtime(
            settings,
            _environment(callbacks="twilio"),
            repository=cast("PostgresRepository", _ObservationRepository()),
            artifacts=cast("S3ArtifactStore", object()),
        )
    assert caught.value.code == code


class _AsyncResource:
    def __init__(self, *_args: object, **_kwargs: object) -> None:
        self.closed = False

    async def __aenter__(self) -> _AsyncResource:
        return self

    async def __aexit__(
        self,
        _exc_type: type[BaseException] | None,
        _exc: BaseException | None,
        _traceback: object,
    ) -> None:
        self.closed = True


class _Artifacts:
    def __init__(self, *_args: object, **_kwargs: object) -> None:
        pass

    async def ready(self) -> bool:
        return True


class _BuiltCallbacks:
    ingresses: tuple[()] = ()

    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _Service:
    def __init__(self, *_args: object, **_kwargs: object) -> None:
        self.app = FastAPI()
        self.drained = False

    def begin_drain(self) -> None:
        self.drained = True


class _Maintenance:
    def __init__(self, *_args: object, **_kwargs: object) -> None:
        self.closed = False
        self.runs = 0

    async def run_once(self) -> MaintenanceRun:
        self.runs += 1
        return _maintenance_report(claimed=1)

    async def close(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_run_results_service_wires_preflight_supervisor_and_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    callbacks = _BuiltCallbacks()
    captured: dict[str, object] = {}

    async def preflight(**_kwargs: object) -> ManagedPersistenceReport:
        return _preflight_report()

    async def supervise(
        service: object,
        maintenance: object,
        **kwargs: object,
    ) -> None:
        captured.update(service=service, maintenance=maintenance, **kwargs)

    def build_callbacks(*_args: object, **_kwargs: object) -> _BuiltCallbacks:
        return callbacks

    monkeypatch.setattr(runtime, "PostgresRepository", _AsyncResource)
    monkeypatch.setattr(runtime, "PostgresRelayJournal", _AsyncResource)
    monkeypatch.setattr(runtime, "S3ArtifactStore", _Artifacts)
    monkeypatch.setattr(runtime, "managed_persistence_preflight", preflight)
    monkeypatch.setattr(runtime, "_build_recording_runtime", build_callbacks)
    monkeypatch.setattr(runtime, "CompanionService", _Service)
    monkeypatch.setattr(runtime, "CompanionMaintenance", _Maintenance)
    monkeypatch.setattr(runtime, "_supervise", supervise)

    await runtime.run_results_service(environment=_environment())

    maintenance = cast("_Maintenance", captured["maintenance"])
    assert captured["port"] == 9090
    assert maintenance.closed
    assert callbacks.closed


class _FakeServer:
    exit_immediately: ClassVar[bool] = False

    def __init__(self, _config: object) -> None:
        self.started = False
        self._should_exit = False
        self._exit = asyncio.Event()

    @property
    def should_exit(self) -> bool:
        return self._should_exit

    @should_exit.setter
    def should_exit(self, value: bool) -> None:
        self._should_exit = value
        if value:
            self._exit.set()

    async def serve(self) -> None:
        self.started = True
        if self.exit_immediately:
            return
        await self._exit.wait()


def _maintenance_report(*, claimed: int = 0) -> MaintenanceRun:
    return MaintenanceRun(
        recovery=RecoveryRun(stale=0, active=0, terminalized=0, deferred=0),
        delivery=DeliveryRun(
            claimed=claimed,
            delivered=claimed,
            failed=0,
            dead_lettered=0,
        ),
        purged_artifacts=0,
    )


@pytest.mark.asyncio
async def test_supervisor_drains_after_signal_and_detects_early_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    restored: list[bool] = []

    def install(shutdown: asyncio.Event) -> Callable[[], None]:
        shutdown.set()
        return lambda: restored.append(True)

    monkeypatch.setattr(runtime, "_ManagedServer", _FakeServer)
    monkeypatch.setattr(runtime, "_install_signal_handlers", install)
    service = _Service()
    maintenance = _Maintenance()
    await runtime._supervise(
        cast("runtime.CompanionService", service),
        cast("runtime.CompanionMaintenance", maintenance),
        port=8080,
        maintenance_interval_s=0.1,
        drain_grace_s=0,
    )

    assert service.drained
    assert maintenance.runs >= 1
    assert restored == [True]

    _FakeServer.exit_immediately = True
    with pytest.raises(VoicekitError) as caught:
        await runtime._supervise(
            cast("runtime.CompanionService", _Service()),
            cast("runtime.CompanionMaintenance", _Maintenance()),
            port=8080,
            maintenance_interval_s=0.1,
            drain_grace_s=0,
        )
    _FakeServer.exit_immediately = False
    assert caught.value.code == "VK-DEP-003"


class _StoppingMaintenance:
    def __init__(self, stop: asyncio.Event, *, fail: bool) -> None:
        self.stop = stop
        self.fail = fail

    async def run_once(self) -> MaintenanceRun:
        self.stop.set()
        if self.fail:
            raise VoicekitError("VK-REL-006", detail="injected")
        return _maintenance_report(claimed=1)


@pytest.mark.asyncio
@pytest.mark.parametrize("fail", [False, True])
async def test_maintenance_loop_handles_work_and_catalogued_failure(fail: bool) -> None:
    stop = asyncio.Event()
    await runtime._maintenance_loop(
        cast("runtime.CompanionMaintenance", _StoppingMaintenance(stop, fail=fail)),
        stop,
        interval_s=0.1,
    )
    assert stop.is_set()


class _FinalFailureMaintenance(_Maintenance):
    async def run_once(self) -> MaintenanceRun:
        self.runs += 1
        raise VoicekitError("VK-REL-006", detail="final")


@pytest.mark.asyncio
async def test_supervisor_waits_grace_and_logs_final_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def install(shutdown: asyncio.Event) -> Callable[[], None]:
        shutdown.set()
        return lambda: None

    monkeypatch.setattr(runtime, "_ManagedServer", _FakeServer)
    monkeypatch.setattr(runtime, "_install_signal_handlers", install)
    await runtime._supervise(
        cast("runtime.CompanionService", _Service()),
        cast("runtime.CompanionMaintenance", _FinalFailureMaintenance()),
        port=8080,
        maintenance_interval_s=0.1,
        drain_grace_s=0.001,
    )


@pytest.mark.asyncio
async def test_wait_started_success_task_failure_and_timeout() -> None:
    running = asyncio.create_task(asyncio.sleep(1))
    started = _FakeServer(object())
    started.started = True
    await runtime._wait_started(cast("uvicorn.Server", started), running)
    running.cancel()

    stopped = _FakeServer(object())
    done = asyncio.create_task(asyncio.sleep(0))
    await done
    with pytest.raises(VoicekitError) as exited:
        await runtime._wait_started(cast("uvicorn.Server", stopped), done)
    assert exited.value.code == "VK-DEP-003"

    never = _FakeServer(object())
    pending = asyncio.create_task(asyncio.sleep(1))
    with pytest.raises(VoicekitError) as timeout:
        await runtime._wait_started(cast("uvicorn.Server", never), pending, timeout_s=0.001)
    pending.cancel()
    assert timeout.value.code == "VK-DEP-003"


@pytest.mark.asyncio
async def test_signal_handler_sets_event_and_restores(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    installed: list[tuple[object, object]] = []

    def get_signal(sig: object) -> str:
        return f"old-{sig}"

    def set_signal(sig: object, handler: object) -> None:
        installed.append((sig, handler))

    monkeypatch.setattr(runtime.signal, "getsignal", get_signal)
    monkeypatch.setattr(runtime.signal, "signal", set_signal)
    shutdown = asyncio.Event()
    restore = runtime._install_signal_handlers(shutdown)
    handler = cast("Callable[[int, object], None]", installed[0][1])
    handler(15, None)
    await asyncio.sleep(0)
    assert shutdown.is_set()

    restore()
    assert len(installed) == 4


def test_managed_server_context_and_logging_helpers() -> None:
    server = runtime._ManagedServer(uvicorn.Config(FastAPI()))
    with server.capture_signals():
        assert server.config is not None

    runtime._log_preflight(_preflight_report())
    assert runtime._provider_state("completed") == "completed"


def test_results_service_main_success_and_catalogued_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configured: list[str] = []

    async def success() -> None:
        return None

    async def failure() -> None:
        raise VoicekitError("VK-DEP-003", detail="injected")

    def configure(*, format: str) -> None:
        configured.append(format)

    monkeypatch.setattr(runtime, "configure_logging", configure)
    monkeypatch.setattr(runtime, "run_results_service", success)
    runtime.main()
    monkeypatch.setattr(runtime, "run_results_service", failure)
    with pytest.raises(SystemExit) as exited:
        runtime.main()

    assert configured == ["json", "json"]
    assert exited.value.code == 1


def _preflight_report() -> ManagedPersistenceReport:
    return ManagedPersistenceReport(
        target="fly",
        storage_backend="postgres",
        artifact_backend="s3",
        schema_ready=True,
        relay_journal_ready=True,
        artifact_round_trip=True,
        rolling_generation=2,
        stale_writer_rejected=True,
        terminal_event_count=1,
    )
