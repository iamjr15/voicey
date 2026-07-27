"""Native Pipecat scratch scaffold generated only after validated choices."""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from voicekit.config.manifest import ManifestStore, ProjectManifest
from voicekit.config.models import RuntimeName
from voicekit.errors import VoicekitError


@dataclass(frozen=True, slots=True)
class ScratchScaffold:
    """Values required to render a working native-flow project."""

    project_name: str
    description: str
    stt: str
    llm: str
    tts: str
    phone_provider: str | None
    phone_number: str | None
    web_enabled: bool
    runtime: RuntimeName = "pipecat"
    recipe_name: str = "scratch"
    recipe_version: str = "1.0.0"


class ScaffoldWriter:
    """Preflight every path, then atomically create a resumable scaffold."""

    def write(
        self,
        project_dir: Path,
        scaffold: ScratchScaffold,
        manifest: ProjectManifest,
    ) -> tuple[Path, ...]:
        if scaffold.runtime != manifest.runtime:
            raise VoicekitError(
                "VK-CLI-007",
                detail=(
                    f"scaffold runtime {scaffold.runtime!r} does not match "
                    f"manifest runtime {manifest.runtime!r}."
                ),
            )
        rendered = _render(scaffold)
        gitignore = project_dir / ".gitignore"
        if gitignore.is_symlink():
            raise VoicekitError("VK-SEC-002", detail=str(gitignore))
        if gitignore.exists():
            try:
                rendered[".gitignore"] = _merge_gitignore(
                    gitignore.read_text(encoding="utf-8"),
                    rendered[".gitignore"],
                )
            except OSError as exc:
                raise VoicekitError(
                    "VK-CLI-003",
                    detail=f"could not inspect existing scaffold paths in {project_dir}.",
                ) from exc
        try:
            conflicts = [
                relative
                for relative, payload in rendered.items()
                if (project_dir / relative).exists()
                and relative != ".gitignore"
                and (project_dir / relative).read_text(encoding="utf-8") != payload
            ]
        except OSError as exc:
            raise VoicekitError(
                "VK-CLI-003",
                detail=f"could not inspect existing scaffold paths in {project_dir}.",
            ) from exc
        if conflicts:
            raise VoicekitError(
                "VK-CLI-003",
                detail=f"refusing to overwrite existing {conflicts[0]}.",
            )
        written: list[Path] = []
        replaced: dict[Path, str] = {}
        try:
            for relative, payload in rendered.items():
                destination = project_dir / relative
                if destination.exists():
                    if destination.read_text(encoding="utf-8") != payload:
                        replaced[destination] = destination.read_text(encoding="utf-8")
                        _write_new(destination, payload)
                    continue
                _write_new(destination, payload)
                written.append(destination)
            ManifestStore(project_dir / "voicekit.jsonc").save(manifest)
        except Exception:
            for path in reversed(written):
                path.unlink(missing_ok=True)
            for path, payload in replaced.items():
                _write_new(path, payload)
            raise
        return tuple(written)


def _render(scaffold: ScratchScaffold) -> dict[str, str]:
    from voicekit.recipes.source import recipe_files

    is_recipe = scaffold.recipe_name != "scratch"
    phone = "None"
    if scaffold.phone_provider is not None and scaffold.phone_number is not None:
        phone = (
            "Phone(\n"
            f"        provider={scaffold.phone_provider!r},\n"
            f"        number={scaffold.phone_number!r},\n"
            "        inbound=True,\n"
            "        outbound=True,\n"
            "        record=False,\n"
            "    )"
        )
    web = (
        'Web(enabled=True, allowed_origins=["http://localhost:5173"])'
        if scaffold.web_enabled
        else "Web()"
    )
    description = scaffold.description.strip()
    behavior_import = ", Behavior" if is_recipe else ""
    environment_import = "from os import environ\n\n" if is_recipe else ""
    behavior = (
        "    behavior=Behavior(\n"
        '        voicemail="leave_message",\n'
        '        transfer_number=environ.get("VOICEKIT_TRANSFER_NUMBER"),\n'
        "    ),\n"
        if is_recipe
        else ""
    )
    agent = f'''"""Generated voicekit agent. Customize the TODO-marked integration points."""

{environment_import}from voicekit import Agent{behavior_import}, Models, Phone, Results, Web

agent = Agent(
    name={scaffold.project_name!r},
    runtime={scaffold.runtime!r},
    models=Models(
        stt={scaffold.stt!r},
        llm={scaffold.llm!r},
        tts={scaffold.tts!r},
    ),
    persona={description!r},
    flow="flow:entry",
    tools="tools",
    phone={phone},
    web={web},
{behavior}    results=Results(
        webhook="https://example.invalid/voicekit-results",  # TODO: receiver
        secret_env="VOICEKIT_WEBHOOK_SECRET",
    ),
)
'''
    if scaffold.runtime == "pipecat":
        flow = '''"""Native Pipecat Flows entrypoint."""

from pathlib import Path

from pipecat.flows import FlowManager, NodeConfig


def entry(_flow_manager: FlowManager) -> NodeConfig:
    system = Path("prompts/system.md").read_text(encoding="utf-8")
    return NodeConfig(
        name="entry",
        role_message=system,
        task_messages=[
            {
                "role": "developer",
                "content": "Greet the caller and help with the stated task.",
            }
        ],
        respond_immediately=True,
    )
'''
        flow_reference = "flow:entry"
        flow_assertion = 'assert entry.__name__ == "entry"'
        flow_import = "from flow import entry"
        runtime_label = "Pipecat Flows"
    else:
        flow = '''"""Native LiveKit agent-workflow entrypoint."""

from pathlib import Path

from livekit.agents import Agent, FunctionTool


def entrypoint(tools: list[FunctionTool]) -> Agent:
    system = Path("prompts/system.md").read_text(encoding="utf-8")
    return Agent(
        instructions=(
            system
            + "\\nGreet the caller and help with the stated task. "
            "Use the supplied tools when they are useful."
        ),
        tools=tools,
    )
'''
        flow_reference = "flow:entrypoint"
        flow_assertion = 'assert entrypoint.__name__ == "entrypoint"'
        flow_import = "from flow import entrypoint"
        runtime_label = "LiveKit agent workflow"
    agent = agent.replace('flow="flow:entry"', f"flow={flow_reference!r}")
    tools = '''"""TODO: replace this example with your real integration."""

from voicekit import tool


@tool(say_while_running="Let me check that.")
def example_lookup(query: str) -> dict[str, str]:
    """Return a deterministic placeholder result for a caller query."""
    return {"query": query, "status": "TODO: connect your service"}
'''
    test = f'''"""Generated scaffold smoke test."""

from agent import agent
{flow_import}


def test_generated_agent_is_ready_to_start() -> None:
    assert agent.runtime == {scaffold.runtime!r}
    {flow_assertion}
    assert agent.flow == {flow_reference!r}
'''
    scenario = '''"""TODO: expand this into the P1 Pipecat Evals scenario suite."""


def test_example_conversation_goal() -> None:
    goal = "The agent greets the caller and addresses the configured task."
    assert goal
'''
    extras = [scaffold.runtime]
    if scaffold.phone_provider is not None:
        carrier_extra = "livekit" if scaffold.phone_provider == "sip" else scaffold.phone_provider
        if carrier_extra not in extras:
            extras.append(carrier_extra)
    extra_list = ",".join(extras)
    pyproject = f"""[project]
name = {scaffold.project_name!r}
version = "0.1.0"
requires-python = ">=3.11,<3.15"
dependencies = [
  "voicekit[{extra_list}]",
]

[tool.pytest.ini_options]
testpaths = ["tests"]
"""
    env_names = {
        "VOICEKIT_WEBHOOK_SECRET",
        _provider_key(scaffold.stt),
        _provider_key(scaffold.llm),
        _provider_key(scaffold.tts),
    }
    if scaffold.phone_provider == "twilio":
        env_names.update({"TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN"})
    elif scaffold.phone_provider == "telnyx":
        env_names.update(
            {
                "TELNYX_API_KEY",
                "TELNYX_CONNECTION_ID",
                "TELNYX_PUBLIC_KEY",
            }
        )
    elif scaffold.phone_provider == "vobiz":
        env_names.update({"VOBIZ_AUTH_ID", "VOBIZ_AUTH_TOKEN"})
    elif scaffold.phone_provider == "plivo":
        env_names.update({"PLIVO_AUTH_ID", "PLIVO_AUTH_TOKEN"})
    elif scaffold.phone_provider == "sip":
        env_names.update(
            {
                "VOICEKIT_SIP_ADDRESS",
                "VOICEKIT_SIP_ALLOWED_ADDRESSES",
                "VOICEKIT_SIP_MEDIA_ENCRYPTION",
                "VOICEKIT_SIP_PASSWORD",
                "VOICEKIT_SIP_TRANSPORT",
                "VOICEKIT_SIP_USERNAME",
            }
        )
    if scaffold.runtime == "livekit":
        env_names.update({"LIVEKIT_URL", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET"})
        if scaffold.phone_provider == "twilio":
            env_names.update(
                {
                    "VOICEKIT_LIVEKIT_SIP_URI",
                    "VOICEKIT_TWILIO_SIP_DOMAIN",
                    "VOICEKIT_TWILIO_SIP_PASSWORD",
                    "VOICEKIT_TWILIO_SIP_USERNAME",
                }
            )
        elif scaffold.phone_provider == "telnyx":
            env_names.update(
                {
                    "VOICEKIT_LIVEKIT_SIP_URI",
                    "VOICEKIT_TELNYX_SIP_PASSWORD",
                    "VOICEKIT_TELNYX_SIP_USERNAME",
                }
            )
        elif scaffold.phone_provider == "vobiz":
            env_names.update(
                {
                    "VOICEKIT_LIVEKIT_SIP_URI",
                    "VOICEKIT_VOBIZ_SIP_CREDENTIAL_ID",
                    "VOICEKIT_VOBIZ_SIP_PASSWORD",
                    "VOICEKIT_VOBIZ_SIP_USERNAME",
                }
            )
        elif scaffold.phone_provider == "plivo":
            env_names.update(
                {
                    "VOICEKIT_LIVEKIT_SIP_URI",
                    "VOICEKIT_PLIVO_SIP_PASSWORD",
                    "VOICEKIT_PLIVO_SIP_USERNAME",
                }
            )
    if is_recipe:
        env_names.add("VOICEKIT_TRANSFER_NUMBER")
    env_example = "\n".join(f"{name}=" for name in sorted(env_names)) + "\n"
    readme = f"""# {scaffold.project_name}

{description}

Generated as a native {runtime_label}. Start with `voicekit doctor`, then run
`voicekit dev`. Search for `TODO` before connecting production systems.
"""
    rendered = {
        "agent.py": agent,
        "flow.py": flow,
        "tools.py": tools,
        "prompts/system.md": description + "\n",
        "tests/test_agent.py": test,
        "tests/test_scenario.py": scenario,
        "pyproject.toml": pyproject,
        ".env.example": env_example,
        ".gitignore": ".env*\n!.env.example\n__pycache__/\n.venv/\n.voicekit/\n",
        "README.md": readme,
    }
    if is_recipe:
        for generated_only in (
            "flow.py",
            "tools.py",
            "prompts/system.md",
            "tests/test_agent.py",
            "tests/test_scenario.py",
            "README.md",
        ):
            rendered.pop(generated_only)
        rendered.update(recipe_files(scaffold.recipe_name, scaffold.runtime))
    return rendered


def _provider_key(model_id: str) -> str:
    provider = model_id.split("/", maxsplit=1)[0]
    return {
        "deepgram": "DEEPGRAM_API_KEY",
        "openai": "OPENAI_API_KEY",
        "anthropic": "ANTHROPIC_API_KEY",
        "google": "GEMINI_API_KEY",
        "cartesia": "CARTESIA_API_KEY",
        "elevenlabs": "ELEVENLABS_API_KEY",
    }[provider]


def _write_new(path: Path, payload: str) -> None:
    temporary_path: Path | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            text=True,
        )
        temporary_path = Path(temporary_name)
        with os.fdopen(descriptor, "w", encoding="utf-8") as temporary:
            temporary.write(payload)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_path, path)
    except OSError as exc:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise VoicekitError("VK-CLI-003", detail=f"could not create {path}.") from exc


def _merge_gitignore(existing: str, generated: str) -> str:
    lines = existing.splitlines()
    for line in generated.splitlines():
        if line not in lines:
            lines.append(line)
    return "\n".join(lines).strip() + "\n"
