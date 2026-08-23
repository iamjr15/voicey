"""Fail-closed Pipecat Cloud and LiveKit Cloud worker bootstraps."""

from __future__ import annotations

import asyncio
import importlib
import os
import sys
import uuid
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, cast
from urllib.parse import urlsplit

from voicey.config.manifest import ManifestStore, ProjectManifest
from voicey.config.models import Agent
from voicey.errors import VoiceyError
from voicey.obs.logging import configure_logging, get_logger
from voicey.obs.telemetry import InstrumentedRepository, Telemetry, TelemetryServer
from voicey.relay.auth import RelayCredential
from voicey.relay.client import RelayClient

if TYPE_CHECKING:
    from pipecat.transports.base_transport import BaseTransport
    from pipecat.transports.websocket.fastapi import FastAPIWebsocketParams

    from voicey.runtimes.pipecat.lifecycle import PipecatCall

_LOG = get_logger(component="cloud-worker")
CloudRuntime = Literal["pipecat", "livekit"]


@dataclass(frozen=True, slots=True)
class CloudWorkerSettings:
    """Validated environment-only inputs shared by both cloud runtimes."""

    runtime: CloudRuntime
    project_root: Path
    relay_url: str
    relay_credential: RelayCredential

    @classmethod
    def from_environment(
        cls,
        environment: Mapping[str, str],
        *,
        expected_runtime: CloudRuntime,
    ) -> CloudWorkerSettings:
        runtime = environment.get("VOICEY_RUNTIME", "")
        if runtime != expected_runtime:
            raise VoiceyError(
                "VY-DEP-008",
                detail=(
                    f"cloud image expects {expected_runtime!r}; VOICEY_RUNTIME selects {runtime!r}."
                ),
            )
        root_value = environment.get("VOICEY_PROJECT_ROOT", "/app/project")
        root = Path(root_value)
        if not root.is_absolute():
            raise VoiceyError(
                "VY-DEP-008",
                detail="VOICEY_PROJECT_ROOT must be an absolute path.",
            )
        relay_url = environment.get("VOICEY_RELAY_URL", "").rstrip("/")
        credential = RelayCredential.parse(_required(environment, "VOICEY_RELAY_CREDENTIAL"))
        parsed = urlsplit(relay_url)
        loopback = parsed.hostname in {"127.0.0.1", "localhost", "::1"}
        if (
            parsed.scheme not in ({"http", "https"} if loopback else {"https"})
            or not parsed.hostname
            or parsed.query
            or parsed.fragment
            or parsed.username
            or parsed.password
        ):
            raise VoiceyError("VY-DEP-008", detail="VOICEY_RELAY_URL is invalid.")
        return cls(
            runtime=expected_runtime,
            project_root=root.resolve(),
            relay_url=relay_url,
            relay_credential=credential,
        )


@dataclass(frozen=True, slots=True)
class RelayRepositoryFactory:
    """Open one authenticated relay stream for each dispatched LiveKit job."""

    base_url: str
    credential: RelayCredential

    async def __call__(self) -> RelayClient:
        return await RelayClient(self.base_url, self.credential).open()


async def validate_cloud_worker_startup(settings: CloudWorkerSettings) -> None:
    """Refuse platform admission until durable signed readiness is acknowledged."""
    async with RelayClient(settings.relay_url, settings.relay_credential):
        return


async def run_pipecat_cloud_session(
    runner_args: object,
    *,
    environment: Mapping[str, str] | None = None,
) -> None:
    """Run one current Pipecat ``RunnerArguments`` session against the relay."""
    values = dict(os.environ if environment is None else environment)
    settings = CloudWorkerSettings.from_environment(values, expected_runtime="pipecat")
    manifest, agent = _load_project(settings)
    if manifest.runtime != "pipecat" or agent.runtime != "pipecat":
        raise VoiceyError("VY-DEP-008", detail="deployed project is not a Pipecat agent.")

    from pipecat.workers.runner import WorkerRunner

    from voicey.runtimes.pipecat.admission import AdmissionController
    from voicey.runtimes.pipecat.lifecycle import (
        PipecatLifecycleManager,
        PipecatRepository,
    )
    from voicey.runtimes.pipecat.session import PipecatSessionBuilder

    raw_repository = RelayClient(settings.relay_url, settings.relay_credential)
    telemetry = Telemetry.from_agent(agent, environment=values)
    telemetry_server = TelemetryServer(telemetry)
    repository = cast(
        "PipecatRepository",
        InstrumentedRepository(raw_repository, telemetry),
    )
    lifecycle: Any | None = None
    session: Any | None = None
    wait_task: asyncio.Task[object] | None = None
    transfer: _CloudTransfer | None = None
    try:
        # This happens before the transport consumes/accepts a caller handshake.
        await raw_repository.open()
        await telemetry_server.start()
        transport, call = await _pipecat_transport_and_call(
            runner_args,
            manifest=manifest,
            agent=agent,
            environment=values,
        )
        admission = AdmissionController(1)
        admission_lease = await admission.acquire(call.call_id)
        lifecycle = await PipecatLifecycleManager(
            repository,
            admission,
            owner_id=f"pipecat_cloud_{uuid.uuid4().hex}",
        ).begin(agent, call, admission_lease)
        transfer = _cloud_transfer(
            provider=call.provider,
            environment=values,
            scratch_root=Path("/tmp") / f"voicey-{call.call_id}",
        )
        sample_rate = 8000 if call.channel == "phone" else 16000
        session = PipecatSessionBuilder(
            repository,
            transfer_handler=transfer,
        ).build(
            agent=agent,
            call=call,
            lifecycle=lifecycle,
            transport=transport,
            sample_rate=sample_rate,
        )
        runner = WorkerRunner(
            handle_sigint=bool(getattr(runner_args, "handle_sigint", False)),
            handle_sigterm=bool(getattr(runner_args, "handle_sigterm", False)),
        )
        await session.start(runner)
        wait_task = asyncio.create_task(
            session.wait(),
            name=f"voicey-cloud-{call.call_id}",
        )
        await runner.run()
        await wait_task
    finally:
        try:
            if wait_task is not None and not wait_task.done() and session is not None:
                await session.end("caller_hangup")
                await wait_task
            elif lifecycle is not None and lifecycle.terminal_event is None:
                await lifecycle.finish("setup_error", provider_state="failed")
        finally:
            if transfer is not None:
                transfer.close()
            await raw_repository.close()
            await telemetry_server.stop()


async def run_livekit_cloud_worker(
    *,
    environment: Mapping[str, str] | None = None,
) -> None:
    """Start the pinned native ``AgentServer`` with a per-job relay factory."""
    values = dict(os.environ if environment is None else environment)
    settings = CloudWorkerSettings.from_environment(values, expected_runtime="livekit")
    manifest, agent = _load_project(settings)
    if manifest.runtime != "livekit" or agent.runtime != "livekit":
        raise VoiceyError("VY-DEP-008", detail="deployed project is not a LiveKit agent.")
    await validate_cloud_worker_startup(settings)

    from voicey.runtimes.livekit import LiveKitHost, LiveKitHostSettings

    host = LiveKitHost(
        agent=agent,
        repository_factory=RelayRepositoryFactory(
            settings.relay_url,
            settings.relay_credential,
        ),
        settings=LiveKitHostSettings(
            num_idle_processes=_integer(values, "VOICEY_CLOUD_IDLE_PROCESSES", default=2),
            drain_timeout_s=agent.limits.max_duration_s,
            session_end_timeout_s=float(agent.limits.max_duration_s),
            health_port=_integer(values, "VOICEY_CLOUD_HEALTH_PORT", default=8081),
            browser_reservation_ttl_s=120.0,
        ),
    )
    await host.run(devmode=False)


async def _pipecat_transport_and_call(
    runner_args: object,
    *,
    manifest: ProjectManifest,
    agent: Agent,
    environment: Mapping[str, str],
) -> tuple[BaseTransport, PipecatCall]:
    """Build only symbols verified against installed Pipecat 1.6.0."""
    from pipecat.runner.types import (
        DailyRunnerArguments,
        SmallWebRTCRunnerArguments,
        WebSocketRunnerArguments,
    )
    from pipecat.runner.utils import (
        create_transport,  # pyright: ignore[reportUnknownVariableType]
        parse_telephony_websocket,
    )
    from pipecat.transports.base_transport import TransportParams
    from pipecat.transports.websocket.fastapi import FastAPIWebsocketTransport

    from voicey.runtimes.pipecat.lifecycle import PipecatCall

    runner_args = _normalize_pipecat_cloud_arguments(runner_args)
    session_id = str(getattr(runner_args, "session_id", "") or uuid.uuid4().hex)
    if isinstance(runner_args, WebSocketRunnerArguments):
        parsed = cast(
            "tuple[str, Any]",
            await parse_telephony_websocket(runner_args.websocket),
        )
        provider, call_data = parsed
        selected = manifest.carriers[0] if manifest.carriers else provider
        expected_wire = "plivo" if selected == "vobiz" else selected
        if provider != expected_wire:
            raise VoiceyError(
                "VY-DEP-008",
                detail=(
                    f"{selected} cloud media negotiated {provider!r}, not the "
                    f"required {expected_wire!r} wire format."
                ),
            )
        if selected not in {"twilio", "telnyx", "vobiz", "plivo"}:
            raise VoiceyError(
                "VY-DEP-008",
                detail=f"Pipecat Cloud telephony provider {selected!r} is unsupported.",
            )
        runner_args.transport_type = provider
        runner_args.call_data = call_data
        params = _telephony_params(
            selected,
            call_data,
            environment=environment,
            max_duration_s=agent.limits.max_duration_s,
        )
        transport = FastAPIWebsocketTransport(
            websocket=runner_args.websocket,
            params=params,
        )
        call_id = cast(str, _call_data_text(call_data, "call_id", "call_control_id"))
        call = PipecatCall(
            call_id=call_id,
            channel="phone",
            direction="inbound",
            provider=selected,
            provider_call_id=call_id,
            from_number=_call_data_text(call_data, "from_number", "from"),
            to_number=_call_data_text(call_data, "to_number", "to"),
        )
        return transport, call

    if isinstance(runner_args, DailyRunnerArguments):
        try:
            from pipecat.transports.daily.transport import DailyParams
        except ImportError as exc:
            raise VoiceyError(
                "VY-DEP-008",
                detail="Pipecat Cloud Daily transport dependency is unavailable.",
            ) from exc
        transport = await create_transport(
            runner_args,
            {
                "daily": lambda: DailyParams(
                    audio_in_enabled=True,
                    audio_out_enabled=True,
                )
            },
        )
        return transport, PipecatCall(
            call_id=f"pcc_{session_id}",
            channel="web",
            direction="inbound",
            provider="daily",
        )

    if isinstance(runner_args, SmallWebRTCRunnerArguments):
        transport = await create_transport(
            runner_args,
            {
                "webrtc": lambda: TransportParams(
                    audio_in_enabled=True,
                    audio_out_enabled=True,
                    audio_in_sample_rate=16000,
                    audio_out_sample_rate=16000,
                )
            },
        )
        return transport, PipecatCall(
            call_id=f"pcc_{session_id}",
            channel="web",
            direction="inbound",
            provider="smallwebrtc",
        )

    raise VoiceyError(
        "VY-DEP-008",
        detail=f"unsupported Pipecat Cloud runner arguments {type(runner_args).__name__}.",
    )


def _normalize_pipecat_cloud_arguments(session_args: object) -> object:
    """Translate only the pinned Pipecat Cloud base-image session contract."""
    argument_type = type(session_args)
    if argument_type.__module__ != "pipecatcloud.agent":
        return session_args

    from pipecat.runner.types import DailyRunnerArguments, WebSocketRunnerArguments

    session_id = getattr(session_args, "session_id", None)
    body = getattr(session_args, "body", None)
    if argument_type.__name__ == "DailySessionArguments":
        room_url = getattr(session_args, "room_url", None)
        token = getattr(session_args, "token", None)
        if not isinstance(room_url, str) or not room_url or not isinstance(token, str):
            raise VoiceyError(
                "VY-DEP-008",
                detail="Pipecat Cloud Daily session arguments are incomplete.",
            )
        return DailyRunnerArguments(
            room_url=room_url,
            token=token,
            body=body,
            session_id=session_id,
        )
    if argument_type.__name__ == "WebSocketSessionArguments":
        websocket = getattr(session_args, "websocket", None)
        if websocket is None:
            raise VoiceyError(
                "VY-DEP-008",
                detail="Pipecat Cloud WebSocket session arguments are incomplete.",
            )
        return WebSocketRunnerArguments(
            websocket=websocket,
            body=body,
            session_id=session_id,
        )
    if argument_type.__name__ == "PipecatSessionArguments":
        raise VoiceyError(
            "VY-DEP-008",
            detail="generic Pipecat Cloud sessions do not identify a supported transport.",
        )
    raise VoiceyError(
        "VY-DEP-008",
        detail=f"unsupported Pipecat Cloud session arguments {argument_type.__name__}.",
    )


def _telephony_params(
    provider: str,
    call_data: object,
    *,
    environment: Mapping[str, str],
    max_duration_s: int,
) -> FastAPIWebsocketParams:
    from pipecat.serializers.plivo import PlivoFrameSerializer
    from pipecat.serializers.telnyx import TelnyxFrameSerializer
    from pipecat.serializers.twilio import TwilioFrameSerializer
    from pipecat.transports.websocket.fastapi import FastAPIWebsocketParams

    call_id = _call_data_text(call_data, "call_id", "call_control_id")
    stream_id = _call_data_text(call_data, "stream_id")
    if not call_id or not stream_id:
        raise VoiceyError(
            "VY-DEP-008",
            detail="cloud telephony handshake omitted call or stream identity.",
        )
    if provider == "twilio":
        serializer = TwilioFrameSerializer(
            stream_sid=stream_id,
            call_sid=call_id,
            account_sid=_required(environment, "TWILIO_ACCOUNT_SID"),
            auth_token=_required(environment, "TWILIO_AUTH_TOKEN"),
            params=TwilioFrameSerializer.InputParams(
                twilio_sample_rate=8000,
                sample_rate=8000,
                auto_hang_up=True,
            ),
        )
    elif provider == "telnyx":
        encoding = _call_data_text(call_data, "outbound_encoding") or "PCMU"
        if encoding != "PCMU":
            raise VoiceyError(
                "VY-TEL-010",
                detail=f"Telnyx media negotiated unsupported encoding {encoding!r}.",
            )
        serializer = TelnyxFrameSerializer(
            stream_id=stream_id,
            outbound_encoding=encoding,
            inbound_encoding="PCMU",
            call_control_id=call_id,
            api_key=_required(environment, "TELNYX_API_KEY"),
            params=TelnyxFrameSerializer.InputParams(
                telnyx_sample_rate=8000,
                sample_rate=8000,
                inbound_encoding="PCMU",
                outbound_encoding=encoding,
                auto_hang_up=True,
            ),
        )
    else:
        auth_id_name = "VOBIZ_AUTH_ID" if provider == "vobiz" else "PLIVO_AUTH_ID"
        auth_token_name = "VOBIZ_AUTH_TOKEN" if provider == "vobiz" else "PLIVO_AUTH_TOKEN"
        serializer = PlivoFrameSerializer(
            stream_id=stream_id,
            call_id=call_id,
            auth_id=_required(environment, auth_id_name),
            auth_token=_required(environment, auth_token_name),
            params=PlivoFrameSerializer.InputParams(
                plivo_sample_rate=8000,
                sample_rate=8000,
                auto_hang_up=False,
            ),
        )
    return FastAPIWebsocketParams(
        audio_in_enabled=True,
        audio_out_enabled=True,
        audio_in_sample_rate=8000,
        audio_out_sample_rate=8000,
        add_wav_header=False,
        serializer=serializer,
        session_timeout=max_duration_s + 30,
        allowed_origins=[],
    )


class _CloudTransfer:
    def __init__(self, adapter: object) -> None:
        self._adapter = adapter

    async def __call__(self, call_id: str, number: str) -> None:
        operation = cast(Any, self._adapter).cold_transfer
        await asyncio.to_thread(operation, call_id, number)

    def close(self) -> None:
        ledger = getattr(self._adapter, "ledger", None)
        if ledger is not None:
            with suppress(Exception):
                ledger.close()
        client = getattr(self._adapter, "_client", None)
        if client is not None and hasattr(client, "close"):
            with suppress(Exception):
                client.close()


def _cloud_transfer(
    *,
    provider: str | None,
    environment: Mapping[str, str],
    scratch_root: Path,
) -> _CloudTransfer | None:
    ledger = scratch_root / "telephony.sqlite3"
    if provider == "twilio":
        from voicey.telephony.twilio import TwilioAdapter

        adapter: object = TwilioAdapter(
            account_sid=environment.get("TWILIO_ACCOUNT_SID"),
            auth_token=environment.get("TWILIO_AUTH_TOKEN"),
            ledger_path=ledger,
        )
    elif provider == "telnyx":
        from voicey.telephony.telnyx import TelnyxAdapter

        adapter = TelnyxAdapter(
            api_key=environment.get("TELNYX_API_KEY"),
            public_key=environment.get("TELNYX_PUBLIC_KEY"),
            connection_id=environment.get("TELNYX_CONNECTION_ID"),
            ledger_path=ledger,
        )
    elif provider == "vobiz":
        from voicey.telephony.vobiz import VobizAdapter

        adapter = VobizAdapter(
            auth_id=environment.get("VOBIZ_AUTH_ID"),
            auth_token=environment.get("VOBIZ_AUTH_TOKEN"),
            ledger_path=ledger,
        )
    elif provider == "plivo":
        from voicey.telephony.plivo import PlivoAdapter

        adapter = PlivoAdapter(
            auth_id=environment.get("PLIVO_AUTH_ID"),
            auth_token=environment.get("PLIVO_AUTH_TOKEN"),
            ledger_path=ledger,
        )
    else:
        return None
    return _CloudTransfer(adapter)


def _load_project(settings: CloudWorkerSettings) -> tuple[ProjectManifest, Agent]:
    root = settings.project_root
    if not root.is_dir():
        raise VoiceyError("VY-DEP-008", detail="cloud project directory is unavailable.")
    manifest = ManifestStore(root / "voicey.jsonc").load()
    text = str(root)
    sys.path.insert(0, text)
    try:
        module = importlib.import_module(manifest.agent_module)
        value: object = cast(Any, module).agent
    except (ImportError, AttributeError) as exc:
        raise VoiceyError(
            "VY-DEP-008",
            detail=f"{manifest.agent_module}.py must export an Agent named `agent`.",
        ) from exc
    finally:
        with suppress(ValueError):
            sys.path.remove(text)
    if not isinstance(value, Agent):
        raise VoiceyError(
            "VY-DEP-008",
            detail=f"{manifest.agent_module}.agent is not a voicey Agent.",
        )
    return manifest, value


def _call_data_text(call_data: object, *names: str) -> str | None:
    for name in names:
        value: object | None = None
        if isinstance(call_data, Mapping):
            value = cast("Mapping[object, object]", call_data).get(name)
        if value is None:
            value = getattr(cast(object, call_data), name, None)
        if isinstance(value, str) and value:
            return value
    return None


def _required(environment: Mapping[str, str], name: str) -> str:
    value = environment.get(name, "").strip()
    if not value:
        raise VoiceyError("VY-DEP-008", detail=f"cloud worker requires {name}.")
    return value


def _integer(environment: Mapping[str, str], name: str, *, default: int) -> int:
    try:
        return int(environment.get(name, str(default)))
    except ValueError as exc:
        raise VoiceyError("VY-DEP-008", detail=f"{name} must be an integer.") from exc


def main() -> None:
    """LiveKit image entrypoint. Pipecat imports the session callable from bot.py."""
    configure_logging(format="json")
    runtime = sys.argv[1] if len(sys.argv) > 1 else ""
    try:
        if runtime != "livekit":
            raise VoiceyError(
                "VY-DEP-008",
                detail="cloud runtime entrypoint expects the `livekit` argument.",
            )
        asyncio.run(run_livekit_cloud_worker())
    except VoiceyError as exc:
        _LOG.error("cloud_worker_start_failed", error_code=exc.code, detail=exc.detail)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
