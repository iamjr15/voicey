"""Supervised local Pipecat host and temporary phone routing."""

from __future__ import annotations

import asyncio
import importlib
import os
import sys
import webbrowser
from collections.abc import Callable, Generator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, cast

import uvicorn
from fastapi import FastAPI

from voicekit.cli.context import ProjectContext, require_manifest
from voicekit.config.models import Agent
from voicekit.errors import VoicekitError
from voicekit.playground import (
    OriginPolicy,
    PlaygroundService,
    PlaygroundSettings,
    ReloadController,
    SessionTokenManager,
    WebSessionSecurity,
    embedded_frontend,
)
from voicekit.runtimes.pipecat import PipecatHost, PipecatHostSettings
from voicekit.storage.sqlite import SQLiteRepository
from voicekit.telephony.models import RollbackToken
from voicekit.telephony.twilio import TwilioAdapter
from voicekit.tunnel import TunnelManager, TunnelPreference, TunnelProbe

if TYPE_CHECKING:
    from livekit.api import LiveKitAPI

    from voicekit.telephony.ledger import TelephonyLedger
    from voicekit.telephony.telnyx import TelnyxAdapter

DevNotice = Callable[[str], None]


class _PhoneProvisioner(Protocol):
    async def rollback(self, operation_id: str) -> object: ...


async def run_dev(
    context: ProjectContext,
    *,
    phone: bool,
    tunnel: TunnelPreference,
    public_url: str | None,
    port: int,
    notice: DevNotice,
    open_browser: bool = False,
) -> None:
    """Run until interrupted, restoring temporary carrier routing in all cases."""
    manifest = require_manifest(context)
    if phone and "phone" not in manifest.channels:
        raise VoicekitError(
            "VK-CLI-007",
            detail="this project has no phone channel; resume init or omit --phone.",
        )
    if not 1 <= port <= 65534:
        raise VoicekitError(
            "VK-CLI-010",
            detail="--port must be from 1 through 65534; the admin listener uses port + 1.",
        )

    with _project_environment(context.environment), _project_import_path(context.root):
        agent = _load_agent(manifest.agent_module)
        if manifest.runtime == "livekit":
            await _run_livekit_dev(
                context,
                agent=agent,
                phone=phone,
                tunnel=tunnel,
                public_url=public_url,
                port=port,
                notice=notice,
                open_browser=open_browser,
            )
            return
        tunnel_handle = None
        rollback: RollbackToken | None = None
        adapter: TwilioAdapter | TelnyxAdapter | None = None
        try:
            external_base = "https://localhost.invalid"
            public_origin = f"http://127.0.0.1:{port}"
            admin_port = port + 1
            admin_origin = f"http://127.0.0.1:{admin_port}"
            if phone:
                tunnel_handle = await TunnelManager(environment=context.environment).open(
                    port,
                    preference=tunnel,
                    public_url=public_url,
                )
                external_base = tunnel_handle.public_url
            repository_path = context.root / ".voicekit" / "calls.sqlite3"
            async with SQLiteRepository(repository_path) as repository:
                selected_provider = (
                    agent.phone.provider
                    if agent.phone is not None
                    else (manifest.carriers[0] if phone and manifest.carriers else None)
                )
                if selected_provider == "twilio":
                    adapter = TwilioAdapter(
                        account_sid=context.environment.get("TWILIO_ACCOUNT_SID"),
                        auth_token=context.environment.get("TWILIO_AUTH_TOKEN"),
                        ledger_path=context.root / ".voicekit" / "telephony.sqlite3",
                        expected_public_base=external_base,
                    )
                elif selected_provider == "telnyx":
                    from voicekit.telephony.telnyx import TelnyxAdapter

                    adapter = TelnyxAdapter(
                        api_key=context.environment.get("TELNYX_API_KEY"),
                        public_key=context.environment.get("TELNYX_PUBLIC_KEY"),
                        connection_id=context.environment.get("TELNYX_CONNECTION_ID"),
                        ledger_path=context.root / ".voicekit" / "telephony.sqlite3",
                    )
                secret = context.environment.get(agent.results.secret_env, "")
                tokens = SessionTokenManager(
                    secret,
                    audience=public_origin,
                    agent_name=agent.name,
                    max_active=agent.limits.max_concurrent,
                )
                security = WebSessionSecurity(
                    tokens,
                    OriginPolicy(
                        allowed_origins=frozenset(
                            {
                                *agent.web.allowed_origins,
                                admin_origin,
                            }
                        ),
                        expected_public_origin=public_origin,
                    ),
                )
                with embedded_frontend() as frontend:
                    host = PipecatHost(
                        agent=agent,
                        repository=repository,
                        settings=PipecatHostSettings(
                            public_base=external_base,
                            twilio_account_sid=context.environment.get(
                                "TWILIO_ACCOUNT_SID",
                                "",
                            ),
                            twilio_auth_token=context.environment.get(
                                "TWILIO_AUTH_TOKEN",
                                "",
                            ),
                            telnyx_api_key=context.environment.get(
                                "TELNYX_API_KEY",
                                "",
                            ),
                            pending_media_timeout_s=float(tokens.ttl_s),
                        ),
                        twilio=(
                            cast("TwilioAdapter", adapter)
                            if selected_provider == "twilio"
                            else None
                        ),
                        telnyx=(
                            cast("TelnyxAdapter", adapter)
                            if selected_provider == "telnyx"
                            else None
                        ),
                        web_sessions=security,
                    )
                    reloads = ReloadController(
                        root=context.root,
                        agent_module=manifest.agent_module,
                        runtime=host,
                        load_agent=lambda: _load_agent(manifest.agent_module),
                    )
                    playground = PlaygroundService(
                        agent=agent,
                        public_app=host.app,
                        repository=repository,
                        tokens=tokens,
                        settings=PlaygroundSettings(
                            admin_origin=admin_origin,
                            public_origin=public_origin,
                            frontend_dir=frontend,
                        ),
                        reserve_web_call=host.reserve_web_call,
                        reload_status=reloads.snapshot,
                    )

                    def apply_revision(updated: Agent) -> None:
                        security.update_agent(
                            updated.name,
                            frozenset({*updated.web.allowed_origins, admin_origin}),
                        )
                        playground.update_agent(updated)

                    reloads.on_loaded = apply_revision
                    probe = TunnelProbe()
                    if phone:
                        probe.install(host.app)
                    public_server = _server(host.app, port=port)
                    admin_server = _server(playground.admin_app, port=admin_port)
                    public_task = asyncio.create_task(
                        public_server.serve(),
                        name="voicekit-public-listener",
                    )
                    admin_task = asyncio.create_task(
                        admin_server.serve(),
                        name="voicekit-admin-listener",
                    )
                    server_tasks = {public_task, admin_task}
                    reload_stop = asyncio.Event()
                    reload_task: asyncio.Task[None] | None = None
                    try:
                        await asyncio.gather(
                            _wait_started(public_server, public_task),
                            _wait_started(admin_server, admin_task),
                        )
                        reload_task = asyncio.create_task(
                            reloads.watch(reload_stop),
                            name="voicekit-reload-watcher",
                        )
                        if phone:
                            assert tunnel_handle is not None
                            assert adapter is not None
                            await probe.verify(tunnel_handle.public_url, timeout_s=15)
                            rollback = await asyncio.to_thread(
                                adapter.point_inbound,
                                cast("str", manifest.phone_number),
                                host.target,
                            )
                            notice(f"Phone route: {manifest.phone_number} -> {external_base}")
                        notice(f"Public listener: {public_origin}")
                        notice(f"Playground: {admin_origin}")
                        if phone:
                            notice("Tunnel scope: public listener only; admin routes stay local.")
                        if open_browser:
                            await asyncio.to_thread(webbrowser.open, admin_origin)
                        notice("Press Ctrl-C to stop; temporary phone routing will be restored.")
                        done, _pending = await asyncio.wait(
                            server_tasks,
                            return_when=asyncio.FIRST_COMPLETED,
                        )
                        for task in done:
                            await task
                    finally:
                        reload_stop.set()
                        public_server.should_exit = True
                        admin_server.should_exit = True
                        await asyncio.gather(*server_tasks, return_exceptions=True)
                        if reload_task is not None:
                            await reload_task
        finally:
            if adapter is not None and rollback is not None:
                await asyncio.to_thread(adapter.restore, rollback)
                notice(f"Restored phone route using rollback token {rollback.token}.")
            if adapter is not None:
                adapter.ledger.close()
            if tunnel_handle is not None:
                await tunnel_handle.close()


@dataclass(frozen=True, slots=True)
class _SQLiteRepositoryFactory:
    path: Path

    async def __call__(self) -> SQLiteRepository:
        return await SQLiteRepository(self.path).open()


async def _run_livekit_dev(
    context: ProjectContext,
    *,
    agent: Agent,
    phone: bool,
    tunnel: TunnelPreference,
    public_url: str | None,
    port: int,
    notice: DevNotice,
    open_browser: bool,
) -> None:
    """Supervise the native LiveKit worker and the same protected playground."""
    del tunnel, public_url
    if agent.runtime != "livekit":
        raise VoicekitError(
            "VK-CLI-007",
            detail="voicekit.jsonc selects LiveKit but agent.py exports another runtime.",
        )
    if port > 65533:
        raise VoicekitError(
            "VK-CLI-010",
            detail="LiveKit dev also reserves port + 2 for its worker health listener.",
        )
    from voicekit.runtimes.livekit import (
        LiveKitHost,
        LiveKitHostSettings,
        LiveKitTokenIssuer,
    )

    server_url = _required_environment(context.environment, "LIVEKIT_URL")
    api_key = _required_environment(context.environment, "LIVEKIT_API_KEY")
    api_secret = _required_environment(context.environment, "LIVEKIT_API_SECRET")
    public_origin = f"http://127.0.0.1:{port}"
    admin_origin = f"http://127.0.0.1:{port + 1}"
    repository_path = context.root / ".voicekit" / "calls.sqlite3"
    repository_factory = _SQLiteRepositoryFactory(repository_path)
    public_app = FastAPI(
        title=f"voicekit livekit signaling:{agent.name}",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    secret = context.environment.get(agent.results.secret_env, "")
    tokens = SessionTokenManager(
        secret,
        audience=public_origin,
        agent_name=agent.name,
        max_active=agent.limits.max_concurrent,
    )
    security = WebSessionSecurity(
        tokens,
        OriginPolicy(
            allowed_origins=frozenset({*agent.web.allowed_origins, admin_origin}),
            expected_public_origin=public_origin,
        ),
    )
    issuer = LiveKitTokenIssuer(
        server_url=server_url,
        api_key=api_key,
        api_secret=api_secret,
        agent_name=agent.name,
        ttl_s=tokens.ttl_s,
    )
    host = LiveKitHost(
        agent=agent,
        repository_factory=repository_factory,
        settings=LiveKitHostSettings(
            num_idle_processes=0,
            drain_timeout_s=agent.limits.max_duration_s,
            session_end_timeout_s=float(agent.limits.max_duration_s),
            health_port=port + 2,
            browser_reservation_ttl_s=float(tokens.ttl_s),
        ),
    )
    provisioner: _PhoneProvisioner | None = None
    provision_operation_id: str | None = None
    livekit_client: LiveKitAPI | None = None
    telephony_ledger: TelephonyLedger | None = None
    async with SQLiteRepository(repository_path) as repository:
        with embedded_frontend() as frontend:
            reloads = ReloadController(
                root=context.root,
                agent_module=require_manifest(context).agent_module,
                runtime=host,
                load_agent=lambda: _load_agent(require_manifest(context).agent_module),
            )
            playground = PlaygroundService(
                agent=agent,
                public_app=public_app,
                repository=repository,
                tokens=tokens,
                settings=PlaygroundSettings(
                    admin_origin=admin_origin,
                    public_origin=public_origin,
                    frontend_dir=frontend,
                    connect_origins=(server_url,),
                ),
                reserve_web_call=host.reserve_web_call,
                reload_status=reloads.snapshot,
                public_security=security,
                room_token_issuer=issuer,
                cancel_web_call=host.fail_web_reservation,
            )

            def apply_revision(updated: Agent) -> None:
                security.update_agent(
                    updated.name,
                    frozenset({*updated.web.allowed_origins, admin_origin}),
                )
                playground.update_agent(updated)

            reloads.on_loaded = apply_revision
            public_server = _server(public_app, port=port)
            admin_server = _server(playground.admin_app, port=port + 1)
            public_task = asyncio.create_task(
                public_server.serve(),
                name="voicekit-livekit-token-listener",
            )
            admin_task = asyncio.create_task(
                admin_server.serve(),
                name="voicekit-admin-listener",
            )
            worker_task = asyncio.create_task(
                host.run(devmode=True),
                name="voicekit-livekit-worker",
            )
            service_tasks = {public_task, admin_task, worker_task}
            reload_stop = asyncio.Event()
            reload_task: asyncio.Task[None] | None = None
            try:
                await asyncio.gather(
                    _wait_started(public_server, public_task),
                    _wait_started(admin_server, admin_task),
                )
                reload_task = asyncio.create_task(
                    reloads.watch(reload_stop),
                    name="voicekit-reload-watcher",
                )
                if phone:
                    (
                        provisioner,
                        provision_operation_id,
                        livekit_client,
                        telephony_ledger,
                    ) = await _provision_livekit_phone(
                        context,
                        agent=agent,
                        api_key=api_key,
                        api_secret=api_secret,
                        server_url=server_url,
                    )
                    notice(
                        f"Phone SIP route: {require_manifest(context).phone_number} -> "
                        f"LiveKit agent {agent.name}"
                    )
                    notice("LiveKit SIP media bypasses the HTTP tunnel.")
                notice(f"Public token listener: {public_origin}")
                notice(f"Playground: {admin_origin}")
                notice(f"LiveKit worker health: http://127.0.0.1:{port + 2}")
                if open_browser:
                    await asyncio.to_thread(webbrowser.open, admin_origin)
                notice("Press Ctrl-C to stop; temporary phone routing will be restored.")
                done, _pending = await asyncio.wait(
                    service_tasks,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for task in done:
                    await task
            finally:
                reload_stop.set()
                public_server.should_exit = True
                admin_server.should_exit = True
                with suppress(Exception):
                    await host.drain()
                await asyncio.gather(*service_tasks, return_exceptions=True)
                if reload_task is not None:
                    await reload_task
                if provisioner is not None and provision_operation_id is not None:
                    await provisioner.rollback(provision_operation_id)
                    notice(
                        "Restored phone routing using SIP provisioning token "
                        f"{provision_operation_id}."
                    )
                if livekit_client is not None:
                    await livekit_client.aclose()
                if telephony_ledger is not None:
                    telephony_ledger.close()


async def _provision_livekit_phone(
    context: ProjectContext,
    *,
    agent: Agent,
    api_key: str,
    api_secret: str,
    server_url: str,
) -> tuple[_PhoneProvisioner, str, LiveKitAPI, TelephonyLedger]:
    from livekit import api as livekit_api

    from voicekit.telephony.ledger import TelephonyLedger

    manifest = require_manifest(context)
    if (
        manifest.phone_number is None
        or agent.phone is None
        or agent.phone.provider not in {"twilio", "telnyx"}
    ):
        raise VoicekitError(
            "VK-CLI-007",
            detail="LiveKit --phone requires one configured Twilio or Telnyx number.",
        )
    livekit_client = livekit_api.LiveKitAPI(server_url, api_key, api_secret)
    ledger = TelephonyLedger(context.root / ".voicekit" / "telephony.sqlite3")
    try:
        if agent.phone.provider == "twilio":
            from twilio.rest import Client

            from voicekit.runtimes.livekit.sip import (
                TwilioElasticSipBackend,
                TwilioLiveKitSipConfig,
                TwilioLiveKitSipProvisioner,
            )

            account_sid = _required_environment(context.environment, "TWILIO_ACCOUNT_SID")
            auth_token = _required_environment(context.environment, "TWILIO_AUTH_TOKEN")
            twilio_provisioner = TwilioLiveKitSipProvisioner(
                livekit=livekit_client.sip,
                twilio=TwilioElasticSipBackend(Client(account_sid, auth_token)),
                ledger=ledger,
            )
            result = await twilio_provisioner.provision(
                TwilioLiveKitSipConfig(
                    number=manifest.phone_number,
                    agent_name=agent.name,
                    livekit_sip_uri=_required_environment(
                        context.environment,
                        "VOICEKIT_LIVEKIT_SIP_URI",
                    ),
                    twilio_domain_name=_required_environment(
                        context.environment,
                        "VOICEKIT_TWILIO_SIP_DOMAIN",
                    ),
                    auth_username=_required_environment(
                        context.environment,
                        "VOICEKIT_TWILIO_SIP_USERNAME",
                    ),
                    auth_password=_required_environment(
                        context.environment,
                        "VOICEKIT_TWILIO_SIP_PASSWORD",
                    ),
                    record=agent.phone.record,
                )
            )
            provisioner: _PhoneProvisioner = twilio_provisioner
        else:
            from voicekit.runtimes.livekit.telnyx import (
                TelnyxLiveKitSipConfig,
                TelnyxLiveKitSipProvisioner,
                TelnyxSipHTTPBackend,
            )

            telnyx_provisioner = TelnyxLiveKitSipProvisioner(
                livekit=livekit_client.sip,
                telnyx=TelnyxSipHTTPBackend(
                    api_key=_required_environment(
                        context.environment,
                        "TELNYX_API_KEY",
                    )
                ),
                ledger=ledger,
            )
            result = await telnyx_provisioner.provision(
                TelnyxLiveKitSipConfig(
                    number=manifest.phone_number,
                    agent_name=agent.name,
                    livekit_sip_uri=_required_environment(
                        context.environment,
                        "VOICEKIT_LIVEKIT_SIP_URI",
                    ),
                    auth_username=_required_environment(
                        context.environment,
                        "VOICEKIT_TELNYX_SIP_USERNAME",
                    ),
                    auth_password=_required_environment(
                        context.environment,
                        "VOICEKIT_TELNYX_SIP_PASSWORD",
                    ),
                )
            )
            provisioner = telnyx_provisioner
    except Exception:
        await livekit_client.aclose()
        ledger.close()
        raise
    return provisioner, result.operation_id, livekit_client, ledger


def _required_environment(environment: dict[str, str], name: str) -> str:
    value = environment.get(name, "")
    if not value:
        raise VoicekitError(
            "VK-CLI-004",
            detail=f"{name} is required. Run `voicekit keys add livekit` and retry.",
        )
    return value


def _server(app: FastAPI, *, port: int) -> uvicorn.Server:
    return uvicorn.Server(
        uvicorn.Config(
            app,
            host="127.0.0.1",
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
    timeout_s: float = 10,
) -> None:
    async def wait() -> None:
        while not server.started:
            if task.done():
                await task
                raise VoicekitError("VK-CLI-009", detail="development server exited early.")
            await asyncio.sleep(0.02)

    try:
        await asyncio.wait_for(wait(), timeout=timeout_s)
    except TimeoutError as exc:
        raise VoicekitError(
            "VK-CLI-009",
            detail="development server did not become ready.",
        ) from exc


def _load_agent(module_name: str) -> Agent:
    try:
        module = importlib.import_module(module_name)
        agent = module.agent
    except (ImportError, AttributeError) as exc:
        raise VoicekitError(
            "VK-CLI-007",
            detail=f"{module_name}.py must export an Agent named `agent`.",
        ) from exc
    if not isinstance(agent, Agent):
        raise VoicekitError(
            "VK-CLI-007",
            detail=f"{module_name}.agent is not a voicekit Agent.",
        )
    return agent


@contextmanager
def _project_import_path(root: Path) -> Generator[None]:
    text = str(root)
    sys.path.insert(0, text)
    try:
        yield
    finally:
        with suppress(ValueError):
            sys.path.remove(text)


@contextmanager
def _project_environment(values: dict[str, str]) -> Generator[None]:
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
