from __future__ import annotations

from pathlib import Path

from voicey.errors import ERROR_CATALOG
from voicey.release.docs import (
    API_REFERENCE_PATHS,
    build_api_reference,
    changed_api_reference,
    write_api_reference,
)

ROOT = Path(__file__).parents[2]


def test_committed_api_reference_is_current_and_complete() -> None:
    assert changed_api_reference(ROOT) == ()
    pages = build_api_reference()

    assert tuple(pages) == API_REFERENCE_PATHS
    assert "`Agent`" in pages[Path("docs/api/config.md")]
    assert "`voicey.testing`" in pages[Path("docs/api/python.md")]
    assert "`WebhookEvent`" in pages[Path("docs/api/webhooks.md")]
    assert pages[Path("docs/api/errors.md")].count("| [`VY-") == len(ERROR_CATALOG)


def test_api_reference_writer_repairs_only_drift(tmp_path: Path) -> None:
    assert write_api_reference(tmp_path) == API_REFERENCE_PATHS
    assert changed_api_reference(tmp_path) == ()

    target = tmp_path / API_REFERENCE_PATHS[0]
    target.write_text("stale\n", encoding="utf-8")

    assert changed_api_reference(tmp_path) == (API_REFERENCE_PATHS[0],)
    assert write_api_reference(tmp_path) == (API_REFERENCE_PATHS[0],)
    assert target.read_text(encoding="utf-8").startswith("<!-- Generated")
