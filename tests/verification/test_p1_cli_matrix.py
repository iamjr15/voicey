from __future__ import annotations

from pathlib import Path
from typing import Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field
from typer.main import get_command
from typer.testing import CliRunner

from voicekit.cli.app import app

ROOT = Path(__file__).parents[2]
MATRIX = ROOT / "docs" / "verification" / "p1-cli-matrix.json"


class CommandContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str
    phase: str
    interaction: str
    json_mode: Literal["required", "not-applicable"] = Field(alias="json")
    arguments: list[str]
    options: list[str]
    paired_options: list[tuple[str, str]]
    question_flag_twins: list[str] = []


class CliContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    contract_version: int
    scope: str
    json_policy: str
    commands: list[CommandContract]


def test_p1_cli_matrix_matches_every_actionable_command_and_exact_flag_surface() -> None:
    contract = CliContract.model_validate_json(MATRIX.read_text(encoding="utf-8"))
    actual = _command_surfaces()
    expected = {command.path: command for command in contract.commands}

    assert contract.contract_version == 1
    assert set(actual) == set(expected)
    for path, declared in expected.items():
        surface = actual[path]
        assert surface["arguments"] == declared.arguments, path
        assert surface["options"] == sorted(declared.options), path
        assert surface["paired_options"] == sorted(declared.paired_options), path
        assert ("--json" in declared.options) is (declared.json_mode == "required"), path
        assert set(declared.question_flag_twins) <= set(declared.options), path
        if declared.interaction.startswith("confirmed-"):
            assert "--yes" in declared.options, path


def test_every_declared_command_has_a_working_help_path() -> None:
    contract = CliContract.model_validate_json(MATRIX.read_text(encoding="utf-8"))
    runner = CliRunner()

    for command in contract.commands:
        argv = [*command.path.split(), "--help"] if command.path else ["--help"]
        result = runner.invoke(app, argv)
        assert result.exit_code == 0, (command.path, result.stdout, result.stderr)
        assert "Usage:" in result.stdout


def _command_surfaces() -> dict[str, dict[str, object]]:
    root = cast(Any, get_command(app))
    surfaces: dict[str, dict[str, object]] = {}

    def walk(command: Any, path: tuple[str, ...]) -> None:
        children = cast("dict[str, Any]", getattr(command, "commands", {}))
        if not path or not children:
            arguments: list[str] = []
            options: list[str] = []
            pairs: list[tuple[str, str]] = []
            for parameter in cast("list[Any]", command.params):
                if parameter.param_type_name == "argument":
                    arguments.append(str(parameter.name))
                    continue
                primary = [str(option) for option in parameter.opts]
                secondary = [str(option) for option in parameter.secondary_opts]
                options.extend(primary)
                options.extend(secondary)
                if secondary:
                    pairs.append((primary[0], secondary[0]))
            surfaces[" ".join(path)] = {
                "arguments": arguments,
                "options": sorted(options),
                "paired_options": sorted(pairs),
            }
        for name, child in sorted(children.items()):
            walk(child, (*path, name))

    walk(root, ())
    return surfaces
