from __future__ import annotations

from pathlib import Path

import pytest

from voicekit.errors import VoicekitError
from voicekit.playground import assets


def test_embedded_frontend_contains_built_spa() -> None:
    with assets.embedded_frontend() as frontend:
        assert (frontend / "index.html").is_file()
        assert any((frontend / "assets").iterdir())


def test_embedded_frontend_reports_broken_wheel(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    def missing_files(_package: str) -> Path:
        return tmp_path

    monkeypatch.setattr(assets, "files", missing_files)
    with pytest.raises(VoicekitError) as missing, assets.embedded_frontend():
        pytest.fail("missing assets must not be yielded")
    assert missing.value.code == "VK-WEB-005"
