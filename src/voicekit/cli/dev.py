"""Supervised local Pipecat host and temporary phone routing."""

from __future__ import annotations

import asyncio
import importlib
import os
import sys
import webbrowser
from collections.abc import Callable, Generator
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import cast

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

DevNotice = Callable[[str], None]


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
    if manifest.runtime != "pipecat":
        raise VoicekitError(
            "VK-CLI-005",
            detail="the LiveKit production dev host lands in P2.",
        )
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
        tunnel_handle = None
        rollback: RollbackToken | None = None
        adapter: TwilioAdapter | None = None
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
                if phone or (agent.phone is not None and agent.phone.provider == "twilio"):
                    adapter = TwilioAdapter(
                        account_sid=context.environment.get("TWILIO_ACCOUNT_SID"),
                        auth_token=context.environment.get("TWILIO_AUTH_TOKEN"),
                        ledger_path=context.root / ".voicekit" / "telephony.sqlite3",
                        expected_public_base=external_base,
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
                            pending_media_timeout_s=float(tokens.ttl_s),
                        ),
                        twilio=adapter,
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
