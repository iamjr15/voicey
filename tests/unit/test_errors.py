from __future__ import annotations

import ast
from pathlib import Path

import pytest

from voicey.errors import ERROR_CATALOG, VoiceyError, error_docs_url


def test_registered_error_exposes_stable_code_and_fix() -> None:
    error = VoiceyError("VY-RES-004", detail="event evt_test")

    assert error.code == "VY-RES-004"
    assert error.definition.fix
    assert "event evt_test" in str(error)


def test_unregistered_error_code_is_a_bug() -> None:
    with pytest.raises(AssertionError, match="unregistered voicey error code"):
        VoiceyError("VY-NOT-REGISTERED")


def test_catalog_keys_match_definitions() -> None:
    assert all(key == definition.code for key, definition in ERROR_CATALOG.items())


def test_every_statically_raised_error_is_registered_and_documented() -> None:
    source_root = Path(__file__).parents[2] / "src" / "voicey"
    raised: set[str] = set()
    for path in source_root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "VoiceyError"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
            ):
                raised.add(node.args[0].value)

    assert raised <= ERROR_CATALOG.keys()
    documentation = (source_root.parents[1] / "docs" / "errors.md").read_text(encoding="utf-8")
    assert all(f"## {code}" in documentation for code in ERROR_CATALOG)


def test_error_docs_url_rejects_unregistered_codes() -> None:
    assert error_docs_url("VY-CLI-001").endswith("#vy-cli-001")
    with pytest.raises(AssertionError, match="unregistered"):
        error_docs_url("VY-NOT-REGISTERED")
