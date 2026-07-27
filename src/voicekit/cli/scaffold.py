"""Native Pipecat scratch scaffold generated only after validated choices."""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from voicekit.config.manifest import ManifestStore, ProjectManifest
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


class ScaffoldWriter:
    """Preflight every path, then atomically create a resumable scaffold."""

    def write(
        self,
        project_dir: Path,
        scaffold: ScratchScaffold,
        manifest: ProjectManifest,
    ) -> tuple[Path, ...]:
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
    agent = f'''"""Generated voicekit agent. Customize the TODO-marked integration points."""

from voicekit import Agent, Models, Phone, Results, Web

agent = Agent(
    name={scaffold.project_name!r},
    runtime="pipecat",
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
    results=Results(
        webhook="https://example.invalid/voicekit-results",  # TODO: receiver
        secret_env="VOICEKIT_WEBHOOK_SECRET",
    ),
)
'''
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
    tools = '''"""TODO: replace this example with your real integration."""

from voicekit import tool


@tool(say_while_running="Let me check that.")
def example_lookup(query: str) -> dict[str, str]:
    """Return a deterministic placeholder result for a caller query."""
    return {"query": query, "status": "TODO: connect your service"}
'''
    test = '''"""Generated scaffold smoke test."""

from agent import agent
from flow import entry


def test_generated_agent_is_ready_to_start() -> None:
    assert agent.runtime == "pipecat"
    assert entry.__name__ == "entry"
    assert agent.flow == "flow:entry"
'''
    scenario = '''"""TODO: expand this into the P1 Pipecat Evals scenario suite."""


def test_example_conversation_goal() -> None:
    goal = "The agent greets the caller and addresses the configured task."
    assert goal
'''
    extras = ["pipecat"]
    if scaffold.phone_provider is not None:
        extras.append(scaffold.phone_provider)
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
    env_example = "\n".join(f"{name}=" for name in sorted(env_names)) + "\n"
    readme = f"""# {scaffold.project_name}

{description}

Generated as native Pipecat Flows code. Start with `voicekit doctor`, then run
`voicekit dev`. Search for `TODO` before connecting production systems.
"""
    return {
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
