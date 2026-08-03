from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

ROOT = Path(__file__).parents[2]


def _hook_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("voicey_hatch_build", ROOT / "hatch_build.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _instance(hook: ModuleType, tmp_path: Path) -> Any:
    return hook.CustomBuildHook(
        root=str(ROOT),
        config={},
        build_config=object(),
        metadata=object(),
        directory=str(tmp_path),
        target_name="wheel",
    )


def test_build_hook_skip_requires_existing_assets(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    hook = _hook_module()
    monkeypatch.setattr(hook, "OUTPUT", tmp_path / "missing")
    monkeypatch.setenv("VOICEY_SKIP_FRONTEND_BUILD", "1")
    with pytest.raises(RuntimeError, match="VY-WEB-005"):
        _instance(hook, tmp_path).initialize("0", {})


def test_build_hook_has_actionable_missing_npm_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    hook = _hook_module()
    monkeypatch.delenv("VOICEY_SKIP_FRONTEND_BUILD", raising=False)

    def missing_npm(_binary: str) -> None:
        return None

    monkeypatch.setattr(hook.shutil, "which", missing_npm)
    with pytest.raises(RuntimeError, match="npm is required"):
        _instance(hook, tmp_path).initialize("0", {})
