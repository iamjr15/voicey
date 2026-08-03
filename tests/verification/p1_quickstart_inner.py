"""Installed-wheel half of the P1 quickstart gate."""

from __future__ import annotations

import asyncio
import importlib
import json
import os
import stat
import sys
from collections.abc import Generator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

from typer.testing import CliRunner

from voicey._p0.pipecat_probe import run_pipecat_probe
from voicey.cli.app import app
from voicey.cli.keys import KeyCheck
from voicey.config.catalog import ProviderKind
from voicey.results import WebhookSigner

_REFERENCE_ENV = {
    "DEEPGRAM_API_KEY": "quickstart-deepgram",  # pragma: allowlist secret
    "ANTHROPIC_API_KEY": "quickstart-anthropic",  # pragma: allowlist secret
    "CARTESIA_API_KEY": "quickstart-cartesia",  # pragma: allowlist secret
}
_KEY_NAMES = {
    "deepgram": ("DEEPGRAM_API_KEY",),
    "anthropic": ("ANTHROPIC_API_KEY",),
    "cartesia": ("CARTESIA_API_KEY",),
}


class _ProviderMockValidator:
    async def validate(
        self,
        kind: ProviderKind,
        identifier: str,
        values: Mapping[str, str],
    ) -> KeyCheck:
        del kind
        provider = identifier.split("/", maxsplit=1)[0]
        names = _KEY_NAMES[provider]
        return KeyCheck(
            provider=provider,
            env_names=names,
            status="valid" if all(values.get(name) for name in names) else "missing",
            detail="provider-mocked P1 quickstart validation",
            fix="inject the fixture value",
        )


def main() -> int:
    if len(sys.argv) != 2:
        raise SystemExit("usage: p1_quickstart_inner.py PROJECT")
    project = Path(sys.argv[1]).resolve()
    runner = CliRunner()
    with (
        _environment(_REFERENCE_ENV),
        patch("voicey.cli.wizard.ProviderKeyValidator", _ProviderMockValidator),
    ):
        result = runner.invoke(
            app,
            [
                "init",
                str(project),
                "--name",
                "quickstart-agent",
                "--recipe",
                "scratch",
                "--description",
                "Help callers check an order.",
                "--channels",
                "web",
                "--runtime",
                "pipecat",
                "--models",
                ("stt=deepgram/nova-3,llm=anthropic/claude-sonnet-5,tts=cartesia/sonic-3.5"),
                "--no-draft-prompts",
                "--yes",
            ],
        )
    if result.exit_code != 0:
        raise AssertionError(f"installed CLI init failed: {result.stderr or result.stdout}")

    environment_payload = (project / ".env").read_text(encoding="utf-8")
    if any(value in environment_payload for value in _REFERENCE_ENV.values()):
        raise AssertionError("injected provider fixture leaked into the generated .env")
    if stat.S_IMODE((project / ".env").stat().st_mode) != 0o600:
        raise AssertionError("generated .env is not mode 0600")

    with _project_import(project):
        agent_module = importlib.import_module("agent")
        flow_module = importlib.import_module("flow")
        tools_module = importlib.import_module("tools")
        agent = agent_module.agent
        node = cast("dict[str, Any]", flow_module.entry(cast("Any", None)))
        tool_result = tools_module.example_lookup("order-123")
    if agent.runtime != "pipecat" or agent.flow != "flow:entry":
        raise AssertionError("generated agent does not select its native Pipecat flow")
    if node["name"] != "entry" or not node["respond_immediately"]:
        raise AssertionError("generated native flow is not ready to greet")
    if tool_result["status"] != "TODO: connect your service":
        raise AssertionError("generated typed tool did not execute")

    probe = asyncio.run(run_pipecat_probe())
    WebhookSigner(probe.webhook_secret).verify(
        probe.signed_webhook.headers,
        probe.signed_webhook.body,
        now=1_750_000_001,
    )
    if not probe.browser.connected or probe.phone_termination_count != 1:
        raise AssertionError("provider-mocked Pipecat/browser lifecycle did not complete")

    print(
        json.dumps(
            {
                "cli_init": True,
                "native_flow": node["name"],
                "typed_tool": tool_result["status"],
                "browser_connected": probe.browser.connected,
                "terminal_reason": probe.phone_termination.reason,
                "signed_webhook_verified": True,
            },
            sort_keys=True,
        )
    )
    return 0


@contextmanager
def _environment(values: Mapping[str, str]) -> Generator[None, None, None]:
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


@contextmanager
def _project_import(project: Path) -> Generator[None, None, None]:
    previous = Path.cwd()
    sys.path.insert(0, str(project))
    os.chdir(project)
    try:
        yield
    finally:
        os.chdir(previous)
        sys.path.remove(str(project))


if __name__ == "__main__":
    raise SystemExit(main())
