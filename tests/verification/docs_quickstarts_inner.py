"""Installed-wheel implementation of the verbatim docs quickstart gate."""

from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import os
import re
import shlex
import stat
import sys
from collections.abc import Generator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

from livekit.agents import Agent as LiveKitAgent
from typer.testing import CliRunner

from voicekit._p0.livekit_probe import run_livekit_probe
from voicekit._p0.pipecat_probe import run_pipecat_probe
from voicekit.cli.app import app
from voicekit.cli.keys import KeyCheck
from voicekit.config.catalog import ProviderKind
from voicekit.results import WebhookSigner

_BLOCK = re.compile(
    r"<!-- voicekit-doc-test:start -->\s*```bash\s*(.*?)```\s*"
    r"<!-- voicekit-doc-test:end -->",
    re.DOTALL,
)
_REFERENCE_ENV = {
    "DEEPGRAM_API_KEY": "docs-deepgram",  # pragma: allowlist secret
    "ANTHROPIC_API_KEY": "docs-anthropic",  # pragma: allowlist secret
    "CARTESIA_API_KEY": "docs-cartesia",  # pragma: allowlist secret
    "LIVEKIT_URL": "wss://docs.invalid",
    "LIVEKIT_API_KEY": "docs-livekit",  # pragma: allowlist secret
    "LIVEKIT_API_SECRET": "docs-livekit-secret-at-least-32-bytes",  # pragma: allowlist secret
}
_KEY_NAMES = {
    "deepgram": ("DEEPGRAM_API_KEY",),
    "anthropic": ("ANTHROPIC_API_KEY",),
    "cartesia": ("CARTESIA_API_KEY",),
}


class ProviderMockValidator:
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
            detail="provider-mocked documentation quickstart validation",
            fix="inject the fixture value",
        )


class LiveKitMockValidator:
    async def validate(self, values: Mapping[str, str]) -> KeyCheck:
        names = ("LIVEKIT_URL", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET")
        return KeyCheck(
            provider="livekit",
            env_names=names,
            status="valid" if all(values.get(name) for name in names) else "missing",
            detail="provider-mocked documentation quickstart validation",
            fix="inject the fixture value",
        )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime", choices=("pipecat", "livekit"), required=True)
    parser.add_argument("--docs-root", type=Path, required=True)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    args.workspace.mkdir(parents=True, exist_ok=True)
    runtime = args.runtime
    page = args.docs_root / f"quickstart-{runtime}.md"
    command = _command(page)
    with (
        _environment(_REFERENCE_ENV),
        _working_directory(args.workspace),
        patch("voicekit.cli.wizard.ProviderKeyValidator", ProviderMockValidator),
        patch("voicekit.cli.wizard.LiveKitKeyValidator", LiveKitMockValidator),
    ):
        result = CliRunner().invoke(app, command[1:])
    if result.exit_code != 0:
        raise AssertionError(f"{page.name} command failed: {result.stderr or result.stdout}")
    project = args.workspace / f"hello-{runtime}"
    if stat.S_IMODE((project / ".env").stat().st_mode) != 0o600:
        raise AssertionError(f"{runtime} quickstart .env is not mode 0600")
    environment_text = (project / ".env").read_text(encoding="utf-8")
    if any(secret in environment_text for secret in _REFERENCE_ENV.values()):
        raise AssertionError(f"{runtime} injected fixture leaked into project .env")
    native_entry, tool_status = _exercise_project(project, runtime)
    probe = asyncio.run(run_pipecat_probe() if runtime == "pipecat" else run_livekit_probe())
    WebhookSigner(probe.webhook_secret).verify(
        probe.signed_webhook.headers,
        probe.signed_webhook.body,
        now=1_750_000_001,
    )
    if not probe.browser.connected or probe.phone_termination_count != 1:
        raise AssertionError(f"{runtime} provider-mocked lifecycle did not complete")
    evidence = [
        {
            "runtime": runtime,
            "command": shlex.join(command),
            "native_entry": native_entry,
            "typed_tool": tool_status,
            "browser_connected": True,
            "signed_webhook_verified": True,
            "terminal_reason": probe.phone_termination.reason,
        }
    ]
    report = {"status": "green", "quickstarts": evidence}
    args.report.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(report, sort_keys=True))
    return 0


def _command(page: Path) -> list[str]:
    match = _BLOCK.search(page.read_text(encoding="utf-8"))
    if match is None:
        raise AssertionError(f"{page} has no marked quickstart command")
    normalized = re.sub(r"\\\s*\n\s*", " ", match.group(1)).strip()
    command = shlex.split(normalized)
    if command[:2] != ["voicekit", "init"]:
        raise AssertionError(f"{page} marked command must begin with `voicekit init`")
    return command


def _exercise_project(project: Path, runtime: str) -> tuple[str, str]:
    sys.path.insert(0, str(project))
    previous = Path.cwd()
    os.chdir(project)
    for name in ("agent", "flow", "tools"):
        sys.modules.pop(name, None)
    try:
        agent = importlib.import_module("agent").agent
        flow = importlib.import_module("flow")
        authored_tools = importlib.import_module("tools")
        tool_result = authored_tools.example_lookup("order-123")
        if runtime == "pipecat":
            node = cast("dict[str, Any]", flow.entry(cast("Any", None)))
            native_entry = str(node["name"])
        else:
            native = flow.entrypoint([])
            if not isinstance(native, LiveKitAgent):
                raise AssertionError("documented LiveKit flow is not a native Agent")
            native_entry = type(native).__name__
        if agent.runtime != runtime or tool_result["status"] != "TODO: connect your service":
            raise AssertionError(f"{runtime} generated project contract drifted")
        return native_entry, str(tool_result["status"])
    finally:
        for name in ("agent", "flow", "tools"):
            sys.modules.pop(name, None)
        os.chdir(previous)
        sys.path.remove(str(project))


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
def _working_directory(path: Path) -> Generator[None, None, None]:
    previous = Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(previous)


if __name__ == "__main__":
    raise SystemExit(main())
