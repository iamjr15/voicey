"""Supervised local Pipecat host and temporary phone routing."""

from __future__ import annotations

import asyncio
import importlib
import os
import sys
from collections.abc import Callable, Generator
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import cast

import uvicorn

from voicekit.cli.context import ProjectContext, require_manifest
from voicekit.config.models import Agent
from voicekit.errors import VoicekitError
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
    if not 1 <= port <= 65535:
        raise VoicekitError("VK-CLI-010", detail="--port must be from 1 through 65535.")

    with _project_environment(context.environment), _project_import_path(context.root):
        agent = _load_agent(manifest.agent_module)
        tunnel_handle = None
        rollback: RollbackToken | None = None
        adapter: TwilioAdapter | None = None
        server: uvicorn.Server | None = None
        server_task: asyncio.Task[None] | None = None
        try:
            external_base = "https://localhost.invalid"
            if phone:
                tunnel_handle = await TunnelManager(environment=context.environment).open(
                    port,
                    preference=tunnel,
                    public_url=public_url,
                )
                external_base = tunnel_handle.public_url
            repository_path = context.root / ".voicekit" / "calls.sqlite3"
            async with SQLiteRepository(repository_path) as repository:
                if phone:
                    adapter = TwilioAdapter(
                        account_sid=context.environment.get("TWILIO_ACCOUNT_SID"),
                        auth_token=context.environment.get("TWILIO_AUTH_TOKEN"),
                        ledger_path=context.root / ".voicekit" / "telephony.sqlite3",
                        expected_public_base=external_base,
                    )
                host = PipecatHost(
                    agent=agent,
                    repository=repository,
                    settings=PipecatHostSettings(
                        public_base=external_base,
                        twilio_account_sid=context.environment.get("TWILIO_ACCOUNT_SID", ""),
                        twilio_auth_token=context.environment.get("TWILIO_AUTH_TOKEN", ""),
                    ),
                    twilio=adapter,
                )
                probe = TunnelProbe()
                if phone:
                    probe.install(host.app)
                server = uvicorn.Server(
                    uvicorn.Config(
                        host.app,
                        host="127.0.0.1",
                        port=port,
                        log_level="info",
                        proxy_headers=False,
                        timeout_graceful_shutdown=30,
                    )
                )
                server_task = asyncio.create_task(server.serve(), name="voicekit-dev-server")
                await _wait_started(server, server_task)
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
                notice(f"Local agent: http://127.0.0.1:{port}")
                notice("Press Ctrl-C to stop; temporary phone routing will be restored.")
                await server_task
        finally:
            if server is not None:
                server.should_exit = True
            if server_task is not None and not server_task.done():
                await server_task
            if adapter is not None and rollback is not None:
                await asyncio.to_thread(adapter.restore, rollback)
                notice(f"Restored phone route using rollback token {rollback.token}.")
            if adapter is not None:
                adapter.ledger.close()
            if tunnel_handle is not None:
                await tunnel_handle.close()


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
