from __future__ import annotations

import json
import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import Literal

import pytest

from voicekit.config.manifest import ProjectManifest, RecipeSelection
from voicekit.config.models import ModelAxis
from voicekit.errors import VoicekitError
from voicekit.recipes.source import (
    RECIPE_LOCK_NAME,
    RecipeBaseline,
    RecipeBaselineStore,
    build_recipe_baseline,
    recipe_files,
)
from voicekit.upgrade import (
    UpgradeCommandResult,
    UpgradeManager,
    UvCliRunner,
)


def _manifest(
    recipe: Literal["scratch", "appointment-booking"] = "scratch",
    *,
    version: str = "1.0.0",
) -> ProjectManifest:
    models: dict[ModelAxis, str] = {
        "stt": "deepgram/nova-3",
        "llm": "anthropic/claude-sonnet-5",
        "tts": "cartesia/sonic-3.5",
    }
    return ProjectManifest(
        project_name="upgrade-test",
        runtime="pipecat",
        recipe=RecipeSelection(name=recipe, version=version),
        channels=frozenset({"web"}),
        models=models,
    )


def _project(root: Path, *, lock: str | None = "0.1.0") -> None:
    (root / "pyproject.toml").write_text(
        '[project]\nname = "demo"\nversion = "0.1.0"\ndependencies = ["voicekit[pipecat]>=0.1"]\n',
        encoding="utf-8",
    )
    if lock is not None:
        (root / "uv.lock").write_text(
            f'[[package]]\nname = "voicekit"\nversion = "{lock}"\n',
            encoding="utf-8",
        )


class FakeRunner:
    def __init__(
        self,
        root: Path,
        *,
        target: str = "0.2.0",
        fail: str | None = None,
        uv_version: str = "uv 0.11.7",
        drift: dict[str, object] | None = None,
        mutate_source: bool = False,
        mutate_pyproject: bool = False,
    ) -> None:
        self.root = root
        self.target = target
        self.fail = fail
        self.uv_version = uv_version
        self.drift = drift or {
            "status": "current",
            "conflicts": 0,
            "next_step": "voicekit doctor",
        }
        self.mutate_source = mutate_source
        self.mutate_pyproject = mutate_pyproject
        self.calls: list[tuple[tuple[str, ...], bool]] = []
        self.sync_count = 0

    def run(
        self,
        arguments: Sequence[str],
        *,
        cwd: Path,
        check: bool = True,
        timeout_s: float = 1200,
    ) -> UpgradeCommandResult:
        del timeout_s
        args = tuple(arguments)
        self.calls.append((args, check))
        assert cwd == self.root
        if args == ("--version",):
            return UpgradeCommandResult(0, self.uv_version, "")
        if args[0] == "lock":
            if self.fail == "lock":
                raise VoicekitError("VK-UPG-002", detail="injected lock failure")
            (self.root / "uv.lock").write_text(
                f'[[package]]\nname = "voicekit"\nversion = "{self.target}"\n',
                encoding="utf-8",
            )
            return UpgradeCommandResult(0, "", "")
        if args[:2] == ("sync", "--locked"):
            self.sync_count += 1
            if self.fail == "sync" and self.sync_count == 1:
                raise VoicekitError("VK-UPG-002", detail="injected sync failure")
            return UpgradeCommandResult(0, "", "")
        if args and args[0] == "run":
            if self.fail == "drift":
                raise VoicekitError("VK-UPG-002", detail="injected drift failure")
            if self.mutate_source:
                (self.root / "flow.py").write_text("mutated by dependency\n", encoding="utf-8")
            if self.mutate_pyproject:
                (self.root / "pyproject.toml").write_text(
                    '[project]\ndependencies = ["voicekit", "unexpected"]\n',
                    encoding="utf-8",
                )
            value = "not-json" if self.fail == "json" else json.dumps(self.drift)
            return UpgradeCommandResult(0, value, "")
        raise AssertionError(args)


@pytest.mark.parametrize(
    ("prerelease", "mode", "channel"),
    [
        (False, "if-necessary-or-explicit", "stable"),
        (True, "allow", "canary"),
    ],
)
def test_upgrade_changes_only_lock_and_runs_fresh_drift_process(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    prerelease: bool,
    mode: str,
    channel: str,
) -> None:
    _project(tmp_path)
    runner = FakeRunner(tmp_path)
    pyproject_before = (tmp_path / "pyproject.toml").read_bytes()
    monkeypatch.setattr("voicekit.upgrade.__version__", "0.1.0")

    report = UpgradeManager(tmp_path, runner=runner).upgrade(
        _manifest(),
        prerelease=prerelease,
    )

    assert report.from_version == "0.1.0"
    assert report.to_version == "0.2.0"
    assert report.channel == channel
    assert report.changed is True
    assert report.pyproject_unchanged is True
    assert report.recipe_sources_unchanged is True
    assert report.recipe_drift["status"] == "current"
    assert report.next_step == "voicekit doctor"
    assert (tmp_path / "pyproject.toml").read_bytes() == pyproject_before
    assert (
        ("lock", "--upgrade-package", "voicekit", "--prerelease", mode),
        True,
    ) in runner.calls
    assert (
        (
            "run",
            "--locked",
            "--prerelease",
            mode,
            "voicekit",
            "recipes",
            "update-check",
            "--json",
        ),
        True,
    ) in runner.calls
    assert (("sync", "--locked", "--prerelease", mode), True) in runner.calls


def test_upgrade_bootstraps_baseline_without_changing_recipe_sources(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _project(tmp_path)
    sources = recipe_files("appointment-booking", "pipecat")
    for relative, contents in sources.items():
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents, encoding="utf-8")
    before = {relative: (tmp_path / relative).read_bytes() for relative in sources}
    runner = FakeRunner(
        tmp_path,
        drift={
            "status": "update-available",
            "conflicts": 1,
            "next_step": "review merge guidance",
        },
    )
    monkeypatch.setattr("voicekit.upgrade.__version__", "0.1.0")

    report = UpgradeManager(tmp_path, runner=runner).upgrade(
        _manifest("appointment-booking"),
        prerelease=False,
    )

    assert (tmp_path / RECIPE_LOCK_NAME).is_file()
    assert {relative: (tmp_path / relative).read_bytes() for relative in sources} == before
    assert report.recipe_drift["conflicts"] == 1
    assert report.next_step == "review merge guidance"


def test_upgrade_accepts_existing_matching_baseline_and_reports_no_change(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _project(tmp_path)
    sources = recipe_files("appointment-booking", "pipecat")
    for relative, contents in sources.items():
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents, encoding="utf-8")
    RecipeBaselineStore(tmp_path / RECIPE_LOCK_NAME).save(
        build_recipe_baseline("appointment-booking", "1.0.0", "pipecat")
    )
    runner = FakeRunner(
        tmp_path,
        target="0.1.0",
        drift={"status": "current"},
    )
    monkeypatch.setattr("voicekit.upgrade.__version__", "0.1.0")

    report = UpgradeManager(tmp_path, runner=runner).upgrade(
        _manifest("appointment-booking"),
        prerelease=False,
    )

    assert report.changed is False
    assert report.next_step == "voicekit doctor"


@pytest.mark.parametrize("failure", ["lock", "sync", "drift", "json"])
def test_upgrade_failure_restores_existing_lock(
    tmp_path: Path,
    failure: str,
) -> None:
    _project(tmp_path)
    original = (tmp_path / "uv.lock").read_bytes()
    runner = FakeRunner(tmp_path, fail=failure)

    with pytest.raises(VoicekitError):
        UpgradeManager(tmp_path, runner=runner).upgrade(
            _manifest(),
            prerelease=False,
        )

    assert (tmp_path / "uv.lock").read_bytes() == original
    if failure != "lock":
        assert runner.sync_count >= 1


def test_upgrade_failure_removes_new_lock(tmp_path: Path) -> None:
    _project(tmp_path, lock=None)
    runner = FakeRunner(tmp_path, fail="sync")

    with pytest.raises(VoicekitError):
        UpgradeManager(tmp_path, runner=runner).upgrade(
            _manifest(),
            prerelease=False,
        )

    assert not (tmp_path / "uv.lock").exists()


@pytest.mark.parametrize("target", ["0.2.0rc1", "not-a-version"])
def test_stable_upgrade_rejects_prerelease_or_invalid_voicekit_lock(
    tmp_path: Path,
    target: str,
) -> None:
    _project(tmp_path)
    original = (tmp_path / "uv.lock").read_bytes()
    runner = FakeRunner(tmp_path, target=target)

    with pytest.raises(VoicekitError) as captured:
        UpgradeManager(tmp_path, runner=runner).upgrade(
            _manifest(),
            prerelease=False,
        )

    assert captured.value.code == "VK-UPG-002"
    assert (tmp_path / "uv.lock").read_bytes() == original
    assert runner.sync_count == 1


def test_upgrade_detects_recipe_source_mutation_and_restores_lock(
    tmp_path: Path,
) -> None:
    _project(tmp_path)
    sources = recipe_files("appointment-booking", "pipecat")
    for relative, contents in sources.items():
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents, encoding="utf-8")
    original_lock = (tmp_path / "uv.lock").read_bytes()
    runner = FakeRunner(tmp_path, mutate_source=True)

    with pytest.raises(VoicekitError) as captured:
        UpgradeManager(tmp_path, runner=runner).upgrade(
            _manifest("appointment-booking"),
            prerelease=False,
        )

    assert captured.value.code == "VK-UPG-002"
    assert (tmp_path / "uv.lock").read_bytes() == original_lock
    assert (tmp_path / "flow.py").read_text(encoding="utf-8") == "mutated by dependency\n"


def test_upgrade_detects_pyproject_mutation_and_restores_lock(
    tmp_path: Path,
) -> None:
    _project(tmp_path)
    original_lock = (tmp_path / "uv.lock").read_bytes()
    runner = FakeRunner(tmp_path, mutate_pyproject=True)

    with pytest.raises(VoicekitError) as captured:
        UpgradeManager(tmp_path, runner=runner).upgrade(_manifest(), prerelease=False)

    assert captured.value.code == "VK-UPG-002"
    assert (tmp_path / "uv.lock").read_bytes() == original_lock
    assert "unexpected" in (tmp_path / "pyproject.toml").read_text(encoding="utf-8")


def test_upgrade_rejects_missing_or_mismatched_recipe_baseline(
    tmp_path: Path,
) -> None:
    _project(tmp_path)
    missing = UpgradeManager(tmp_path, runner=FakeRunner(tmp_path))
    with pytest.raises(VoicekitError) as no_base:
        missing.upgrade(
            _manifest("appointment-booking", version="0.9.0"),
            prerelease=False,
        )
    assert no_base.value.code == "VK-UPG-003"

    RecipeBaselineStore(tmp_path / RECIPE_LOCK_NAME).save(
        RecipeBaseline(
            schema_version=1,
            name="appointment-booking",
            version="9.0.0",
            runtime="pipecat",
            files={"flow.py": "base\n"},
        )
    )
    with pytest.raises(VoicekitError) as mismatch:
        UpgradeManager(tmp_path, runner=FakeRunner(tmp_path)).upgrade(
            _manifest("appointment-booking"),
            prerelease=False,
        )
    assert mismatch.value.code == "VK-UPG-003"


@pytest.mark.parametrize("kind", ["symlink", "directory"])
def test_upgrade_rejects_unsafe_recipe_owned_paths(
    tmp_path: Path,
    kind: str,
) -> None:
    _project(tmp_path)
    RecipeBaselineStore(tmp_path / RECIPE_LOCK_NAME).save(
        RecipeBaseline(
            schema_version=1,
            name="appointment-booking",
            version="1.0.0",
            runtime="pipecat",
            files={"flow.py": "base\n"},
        )
    )
    if kind == "symlink":
        (tmp_path / "flow.py").symlink_to(tmp_path / "missing-target")
    else:
        (tmp_path / "flow.py").mkdir()

    with pytest.raises(VoicekitError) as captured:
        UpgradeManager(tmp_path, runner=FakeRunner(tmp_path)).upgrade(
            _manifest("appointment-booking"),
            prerelease=False,
        )

    assert captured.value.code in {"VK-SEC-002", "VK-UPG-003"}


@pytest.mark.parametrize("version", ["uv 0.10.9", "uv 1.0.0", "unknown"])
def test_upgrade_rejects_unsupported_uv_versions(
    tmp_path: Path,
    version: str,
) -> None:
    _project(tmp_path)
    runner = FakeRunner(tmp_path, uv_version=version)

    with pytest.raises(VoicekitError) as captured:
        UpgradeManager(tmp_path, runner=runner).upgrade(
            _manifest(),
            prerelease=False,
        )

    assert captured.value.code == "VK-UPG-001"
    assert all(call[0][0] != "lock" for call in runner.calls)


@pytest.mark.parametrize(
    "pyproject",
    [
        "not = [toml",
        '[project]\nname = "demo"\ndependencies = ["httpx"]\n',
        '[project]\nname = "demo"\ndependencies = "voicekit"\n',
    ],
)
def test_upgrade_rejects_invalid_project_contract(
    tmp_path: Path,
    pyproject: str,
) -> None:
    (tmp_path / "pyproject.toml").write_text(pyproject, encoding="utf-8")
    runner = FakeRunner(tmp_path)

    with pytest.raises(VoicekitError) as captured:
        UpgradeManager(tmp_path, runner=runner).upgrade(
            _manifest(),
            prerelease=False,
        )

    assert captured.value.code == "VK-UPG-001"
    assert runner.calls == []


def test_upgrade_rejects_symlinked_project_and_lock(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.write_text('[project]\ndependencies = ["voicekit"]\n', encoding="utf-8")
    (tmp_path / "pyproject.toml").symlink_to(outside)
    runner = FakeRunner(tmp_path)
    with pytest.raises(VoicekitError) as project_error:
        UpgradeManager(tmp_path, runner=runner).upgrade(_manifest(), prerelease=False)
    assert project_error.value.code == "VK-UPG-001"

    (tmp_path / "pyproject.toml").unlink()
    _project(tmp_path, lock=None)
    (tmp_path / "uv.lock").symlink_to(outside)
    with pytest.raises(VoicekitError) as lock_error:
        UpgradeManager(tmp_path, runner=runner).upgrade(_manifest(), prerelease=False)
    assert lock_error.value.code == "VK-SEC-002"


@pytest.mark.parametrize(
    "lock",
    [
        "not = [toml",
        "version = 1\n",
        '[[package]]\nname = "httpx"\nversion = "1.0.0"\n',
        (
            '[[package]]\nname = "voicekit"\nversion = "1.0.0"\n'
            '[[package]]\nname = "voicekit"\nversion = "2.0.0"\n'
        ),
    ],
)
def test_upgrade_rejects_invalid_resulting_lock(tmp_path: Path, lock: str) -> None:
    _project(tmp_path)
    runner = FakeRunner(tmp_path)

    def invalid_lock_run(
        arguments: Sequence[str],
        *,
        cwd: Path,
        check: bool = True,
        timeout_s: float = 1200,
    ) -> UpgradeCommandResult:
        result = FakeRunner.run(
            runner,
            arguments,
            cwd=cwd,
            check=check,
            timeout_s=timeout_s,
        )
        if tuple(arguments) and arguments[0] == "lock":
            (tmp_path / "uv.lock").write_text(lock, encoding="utf-8")
        return result

    runner.run = invalid_lock_run  # type: ignore[method-assign]
    with pytest.raises(VoicekitError) as captured:
        UpgradeManager(tmp_path, runner=runner).upgrade(_manifest(), prerelease=False)
    assert captured.value.code == "VK-UPG-002"


def test_upgrade_rejects_missing_resulting_lock_and_invalid_drift_shape(
    tmp_path: Path,
) -> None:
    _project(tmp_path)
    runner = FakeRunner(tmp_path)
    original_run = runner.run

    def missing_lock_run(
        arguments: Sequence[str],
        *,
        cwd: Path,
        check: bool = True,
        timeout_s: float = 1200,
    ) -> UpgradeCommandResult:
        result = original_run(arguments, cwd=cwd, check=check, timeout_s=timeout_s)
        if tuple(arguments) and arguments[0] == "lock":
            (tmp_path / "uv.lock").unlink()
        return result

    runner.run = missing_lock_run  # type: ignore[method-assign]
    with pytest.raises(VoicekitError) as missing:
        UpgradeManager(tmp_path, runner=runner).upgrade(_manifest(), prerelease=False)
    assert missing.value.code == "VK-UPG-002"

    _project(tmp_path)
    invalid = FakeRunner(tmp_path, drift={"next_step": "voicekit doctor"})
    with pytest.raises(VoicekitError) as shape:
        UpgradeManager(tmp_path, runner=invalid).upgrade(_manifest(), prerelease=False)
    assert shape.value.code == "VK-UPG-002"


def test_upgrade_ignores_unrelated_non_table_lock_entries(tmp_path: Path) -> None:
    _project(tmp_path)
    runner = FakeRunner(tmp_path)
    original_run = runner.run

    def mixed_lock_run(
        arguments: Sequence[str],
        *,
        cwd: Path,
        check: bool = True,
        timeout_s: float = 1200,
    ) -> UpgradeCommandResult:
        result = original_run(arguments, cwd=cwd, check=check, timeout_s=timeout_s)
        if tuple(arguments) and arguments[0] == "lock":
            (tmp_path / "uv.lock").write_text(
                'package = [1, { name = "voicekit", version = "0.2.0" }]\n',
                encoding="utf-8",
            )
        return result

    runner.run = mixed_lock_run  # type: ignore[method-assign]
    report = UpgradeManager(tmp_path, runner=runner).upgrade(
        _manifest(),
        prerelease=False,
    )
    assert report.to_version == "0.2.0"


def test_uv_cli_runner_maps_missing_nonzero_and_timeout(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    def missing_executable(_value: str) -> None:
        return None

    monkeypatch.setattr("voicekit.upgrade.shutil.which", missing_executable)
    with pytest.raises(VoicekitError) as missing:
        UvCliRunner()
    assert missing.value.code == "VK-UPG-001"

    runner = UvCliRunner("/opt/uv")

    def nonzero(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(["uv"], 7, "", "secret-token")

    monkeypatch.setattr("voicekit.upgrade.subprocess.run", nonzero)
    with pytest.raises(VoicekitError) as failed:
        runner.run(["lock", "--index-url", "https://secret"], cwd=tmp_path)
    assert failed.value.code == "VK-UPG-002"
    assert "secret-token" not in str(failed.value)
    assert "https://secret" not in str(failed.value)
    unchecked = runner.run(["lock"], cwd=tmp_path, check=False)
    assert unchecked.returncode == 7

    def timeout(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired("uv", 1)

    monkeypatch.setattr("voicekit.upgrade.subprocess.run", timeout)
    with pytest.raises(VoicekitError) as timed_out:
        runner.run(["sync"], cwd=tmp_path)
    assert timed_out.value.code == "VK-UPG-002"
