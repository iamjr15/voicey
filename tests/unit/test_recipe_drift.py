from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, cast

import pytest

from voicekit.config.manifest import ProjectManifest, RecipeSelection
from voicekit.config.models import ModelAxis
from voicekit.errors import VoicekitError
from voicekit.recipes.drift import RecipeDriftAnalyzer
from voicekit.recipes.registry import RecipeDefinition, RecipeRegistry
from voicekit.recipes.source import (
    RECIPE_LOCK_NAME,
    RecipeBaseline,
    RecipeBaselineStore,
    build_recipe_baseline,
    recipe_files,
)


def _manifest(
    *,
    recipe: str = "appointment-booking",
    version: str = "1.0.0",
) -> ProjectManifest:
    models: dict[ModelAxis, str] = {
        "stt": "deepgram/nova-3",
        "llm": "anthropic/claude-sonnet-5",
        "tts": "cartesia/sonic-3.5",
    }
    return ProjectManifest(
        project_name="drift-test",
        runtime="pipecat",
        recipe=RecipeSelection(name=recipe, version=version),
        channels=frozenset({"web"}),
        models=models,
    )


def _registry(version: str) -> RecipeRegistry:
    return RecipeRegistry(
        (
            RecipeDefinition(
                name="appointment-booking",
                version=version,
                description="test",
                runtimes=frozenset({"pipecat"}),
                min_engine="0.1.0",
                source_available=True,
            ),
        )
    )


def _write_sources(root: Path, sources: dict[str, str]) -> None:
    for relative, contents in sources.items():
        destination = root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(contents, encoding="utf-8")


def _tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def test_current_recipe_report_is_read_only_and_uses_tracked_baseline(
    tmp_path: Path,
) -> None:
    baseline = build_recipe_baseline("appointment-booking", "1.0.0", "pipecat")
    _write_sources(tmp_path, baseline.files)
    RecipeBaselineStore(tmp_path / RECIPE_LOCK_NAME).save(baseline)
    before = _tree_digest(tmp_path)

    report = RecipeDriftAnalyzer(tmp_path).analyze(_manifest())

    assert report.status == "current"
    assert report.baseline_source == "tracked"
    assert report.files
    assert {row.status for row in report.files} == {"unchanged"}
    assert report.local_changes == 0
    assert report.upstream_changes == 0
    assert report.conflicts == 0
    assert report.ai_merge_prompt is None
    assert report.next_step == "voicekit doctor"
    assert _tree_digest(tmp_path) == before


def test_three_way_report_classifies_every_drift_state(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    base = {
        "unchanged.py": "same\n",
        "local.py": "base local\n",
        "upstream.py": "base upstream\n",
        "converged.py": "base converged\n",
        "conflict.py": "base conflict\n",
        "removed.py": "base removed\n",
    }
    local = {
        **base,
        "local.py": "local edit\n",
        "converged.py": "shared edit\n",
        "conflict.py": "local conflict\n",
    }
    upstream = {
        **base,
        "upstream.py": "upstream edit\n",
        "converged.py": "shared edit\n",
        "conflict.py": "upstream conflict\n",
        "added.py": "upstream added\n",
    }
    upstream.pop("removed.py")
    baseline = RecipeBaseline(
        schema_version=1,
        name="appointment-booking",
        version="1.0.0",
        runtime="pipecat",
        files=base,
    )
    RecipeBaselineStore(tmp_path / RECIPE_LOCK_NAME).save(baseline)
    _write_sources(tmp_path, local)

    def updated_sources(_name: str, _runtime: str) -> dict[str, str]:
        return upstream

    monkeypatch.setattr(
        "voicekit.recipes.drift.recipe_files",
        updated_sources,
    )

    report = RecipeDriftAnalyzer(tmp_path, registry=_registry("1.1.0")).analyze(_manifest())
    statuses = {row.path: row.status for row in report.files}

    assert report.status == "update-available"
    assert statuses == {
        "added.py": "upstream-only",
        "conflict.py": "conflict",
        "converged.py": "converged",
        "local.py": "local-only",
        "removed.py": "upstream-only",
        "unchanged.py": "unchanged",
        "upstream.py": "upstream-only",
    }
    assert report.local_changes == 3
    assert report.upstream_changes == 5
    assert report.conflicts == 1
    assert report.ai_merge_prompt is not None
    assert "never secrets" not in report.ai_merge_prompt.casefold()
    assert "Never copy secrets" in report.ai_merge_prompt
    assert "no MCP" in report.ai_merge_prompt
    assert "overwrite a file wholesale" in report.ai_merge_prompt
    assert report.next_step.endswith("voicekit test")
    row = next(item for item in report.files if item.path == "conflict.py")
    assert row.base_sha256 == hashlib.sha256(base["conflict.py"].encode()).hexdigest()
    assert row.local_sha256 == hashlib.sha256(local["conflict.py"].encode()).hexdigest()
    assert row.upstream_sha256 == hashlib.sha256(upstream["conflict.py"].encode()).hexdigest()


def test_missing_baseline_is_reconstructed_only_for_current_recipe(
    tmp_path: Path,
) -> None:
    current = RecipeDriftAnalyzer(tmp_path).analyze(_manifest())
    old = RecipeDriftAnalyzer(tmp_path).analyze(_manifest(version="0.9.0"))

    assert current.status == "current"
    assert current.baseline_source == "reconstructed-current"
    assert current.local_changes == len(recipe_files("appointment-booking", "pipecat"))
    assert old.status == "baseline-missing"
    assert old.baseline_source == "missing"
    assert "restore the missing baseline" in old.next_step
    assert not (tmp_path / RECIPE_LOCK_NAME).exists()


def test_scratch_and_ahead_recipe_have_explicit_statuses(tmp_path: Path) -> None:
    scratch = RecipeDriftAnalyzer(tmp_path).analyze(_manifest(recipe="scratch", version="1.0.0"))
    RecipeBaselineStore(tmp_path / RECIPE_LOCK_NAME).save(
        RecipeBaseline(
            schema_version=1,
            name="appointment-booking",
            version="2.0.0",
            runtime="pipecat",
            files={"flow.py": "future\n"},
        )
    )
    ahead = RecipeDriftAnalyzer(
        tmp_path,
        registry=_registry("1.0.0"),
    ).analyze(_manifest(version="2.0.0"))

    assert scratch.status == "scratch"
    assert scratch.baseline_source == "not-applicable"
    assert scratch.files == ()
    assert scratch.next_step == "voicekit doctor"
    assert ahead.status == "ahead"
    assert ahead.baseline_source == "tracked"


@pytest.mark.parametrize(
    "payload",
    [
        None,
        {},
        {
            "schema_version": 2,
            "name": "appointment-booking",
            "version": "1.0.0",
            "runtime": "pipecat",
            "files": {"flow.py": "source"},
        },
        {
            "schema_version": 1,
            "name": "appointment-booking",
            "version": "1.0.0",
            "runtime": "pipecat",
            "files": {"../flow.py": "source"},
        },
        {
            "schema_version": 1,
            "name": "appointment-booking",
            "version": "1.0.0",
            "runtime": "pipecat",
            "files": {1: "source"},
        },
    ],
)
def test_recipe_baseline_rejects_invalid_shapes(payload: object) -> None:
    with pytest.raises(VoicekitError) as captured:
        RecipeBaseline.from_payload(payload)

    assert captured.value.code == "VK-UPG-003"


def test_recipe_baseline_and_local_source_safety_errors(
    tmp_path: Path,
) -> None:
    lock = tmp_path / RECIPE_LOCK_NAME
    lock.symlink_to(tmp_path / "outside.json")
    with pytest.raises(VoicekitError) as symlink:
        RecipeBaselineStore(lock).load()
    assert symlink.value.code == "VK-SEC-002"

    lock.unlink()
    lock.write_text("{", encoding="utf-8")
    with pytest.raises(VoicekitError) as corrupt:
        RecipeBaselineStore(lock).load()
    assert corrupt.value.code == "VK-UPG-003"

    baseline = RecipeBaseline(
        schema_version=1,
        name="appointment-booking",
        version="1.0.0",
        runtime="pipecat",
        files={"flow.py": "base"},
    )
    RecipeBaselineStore(lock).save(baseline)
    (tmp_path / "flow.py").mkdir()
    with pytest.raises(VoicekitError) as directory:
        RecipeDriftAnalyzer(tmp_path).analyze(_manifest())
    assert directory.value.code == "VK-UPG-003"


def test_recipe_drift_rejects_broken_symlink_and_non_utf8_source(
    tmp_path: Path,
) -> None:
    baseline = RecipeBaseline(
        schema_version=1,
        name="appointment-booking",
        version="1.0.0",
        runtime="pipecat",
        files={"flow.py": "base"},
    )
    RecipeBaselineStore(tmp_path / RECIPE_LOCK_NAME).save(baseline)
    path = tmp_path / "flow.py"
    path.symlink_to(tmp_path / "missing-source")
    with pytest.raises(VoicekitError) as symlink:
        RecipeDriftAnalyzer(tmp_path).analyze(_manifest())
    assert symlink.value.code == "VK-SEC-002"

    path.unlink()
    path.write_bytes(b"\xff")
    with pytest.raises(VoicekitError) as encoding:
        RecipeDriftAnalyzer(tmp_path).analyze(_manifest())
    assert encoding.value.code == "VK-UPG-003"


def test_recipe_drift_rejects_mismatched_baseline_and_invalid_semver(
    tmp_path: Path,
) -> None:
    RecipeBaselineStore(tmp_path / RECIPE_LOCK_NAME).save(
        RecipeBaseline(
            schema_version=1,
            name="appointment-booking",
            version="9.0.0",
            runtime="pipecat",
            files={"flow.py": "base"},
        )
    )
    with pytest.raises(VoicekitError) as mismatch:
        RecipeDriftAnalyzer(tmp_path).analyze(_manifest())
    assert mismatch.value.code == "VK-UPG-003"

    RecipeBaselineStore(tmp_path / RECIPE_LOCK_NAME).save(
        RecipeBaseline(
            schema_version=1,
            name="appointment-booking",
            version="not-semver",
            runtime="pipecat",
            files={"flow.py": "base"},
        )
    )
    with pytest.raises(VoicekitError) as invalid:
        RecipeDriftAnalyzer(tmp_path, registry=_registry("1.0.0")).analyze(
            _manifest(version="not-semver")
        )
    assert invalid.value.code == "VK-UPG-003"


def test_recipe_baseline_runtime_validation_is_strict() -> None:
    payload: dict[str, Any] = {
        "schema_version": 1,
        "name": "appointment-booking",
        "version": "1.0.0",
        "runtime": "unknown",
        "files": {"flow.py": "source"},
    }
    with pytest.raises(VoicekitError):
        RecipeBaseline.from_payload(cast(object, payload))
