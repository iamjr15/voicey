"""Exercise every packaged first-party recipe from an installed wheel."""

from __future__ import annotations

import argparse
import importlib.util
import json
import tempfile
from pathlib import Path
from types import ModuleType
from typing import Protocol, cast

from livekit.agents import Agent as LiveKitAgent
from pipecat.flows import NodeConfig

from voicey.config.models import RuntimeName
from voicey.recipes.registry import DEFAULT_RECIPE_REGISTRY
from voicey.recipes.source import recipe_files
from voicey.testing import JudgeConfig, discover_scenarios
from voicey.testing.livekit import compile_livekit
from voicey.testing.pipecat import compile_pipecat


class PipecatRecipeModule(Protocol):
    def entry(self, flow_manager: object) -> NodeConfig: ...


class LiveKitRecipeModule(Protocol):
    def entrypoint(self, tools: list[object]) -> object: ...


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument(
        "--runtime",
        choices=("pipecat", "livekit"),
        action="append",
        dest="runtimes",
    )
    args = parser.parse_args()
    runtimes: tuple[RuntimeName, ...] = tuple(args.runtimes or ("pipecat", "livekit"))
    evidence: list[dict[str, object]] = []
    with tempfile.TemporaryDirectory(prefix="voicey-canary-recipes-") as directory:
        workspace = Path(directory)
        for recipe in DEFAULT_RECIPE_REGISTRY.list(include_unavailable=False):
            for runtime in runtimes:
                DEFAULT_RECIPE_REGISTRY.require(recipe.name, runtime)
                root = workspace / recipe.name / runtime
                for relative, contents in recipe_files(recipe.name, runtime).items():
                    target = root / relative
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text(contents, encoding="utf-8")
                scenarios = discover_scenarios(root)
                if runtime == "pipecat":
                    compilation = compile_pipecat(
                        scenarios,
                        output_dir=root / ".compiled",
                        bot=root / "eval_bot.py",
                        audio=False,
                        judge=JudgeConfig(),
                    )
                    module = cast(
                        "PipecatRecipeModule",
                        _load(root / "flow.py", f"canary_{recipe.name}_pipecat"),
                    )
                    node = module.entry(None)
                    native_count = len(compilation.scenarios)
                    native_entry = node.get("name")
                    if not isinstance(native_entry, str) or not native_entry:
                        msg = f"{recipe.name} did not return a named native Pipecat node."
                        raise AssertionError(msg)
                else:
                    compilation = compile_livekit(scenarios)
                    module = cast(
                        "LiveKitRecipeModule",
                        _load(root / "flow.py", f"canary_{recipe.name}_livekit"),
                    )
                    agent = module.entrypoint([])
                    if not isinstance(agent, LiveKitAgent):
                        msg = f"{recipe.name} did not return a native LiveKit Agent."
                        raise AssertionError(msg)
                    native_count = len(compilation)
                    native_entry = type(agent).__name__
                if not scenarios or native_count != len(scenarios):
                    msg = f"{recipe.name}/{runtime} did not compile every scenario."
                    raise AssertionError(msg)
                evidence.append(
                    {
                        "recipe": recipe.name,
                        "recipe_version": recipe.version,
                        "runtime": runtime,
                        "scenarios": len(scenarios),
                        "native_entry": native_entry,
                    }
                )
    report = {
        "status": "green",
        "recipes": evidence,
        "recipe_count": len({row["recipe"] for row in evidence}),
        "runtime_count": len(runtimes),
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(report, sort_keys=True))
    return 0


def _load(path: Path, module_name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(module_name.replace("-", "_"), path)
    if spec is None or spec.loader is None:
        raise AssertionError(f"could not load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


if __name__ == "__main__":
    raise SystemExit(main())
