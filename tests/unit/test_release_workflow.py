from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "release.yml"


def _workflow() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def test_release_workflow_pins_every_third_party_action() -> None:
    text = _workflow()
    uses = re.findall(r"^\s*uses:\s+([^\s]+)", text, flags=re.MULTILINE)

    assert uses
    assert all(re.fullmatch(r"[^@]+@[0-9a-f]{40}", use) for use in uses)


def test_release_workflow_isolates_oidc_from_build_machinery() -> None:
    text = _workflow()
    prepare, publish = text.split("\n  publish:\n", maxsplit=1)

    assert "id-token: write" not in prepare
    assert "uv build" in prepare
    assert "run_p4_release_gate.py" in prepare
    assert prepare.index("Validate immutable release tag") < prepare.index(
        "Sync locked release environment"
    )
    assert "id-token: write" in publish
    assert "uv build" not in publish
    assert "actions/checkout" not in publish
    assert "needs: prepare" in publish
    assert "name: pypi" in publish
    assert "pypa/gh-action-pypi-publish@" in publish
    assert "password:" not in text
    assert "secrets." not in text


def test_release_workflow_requires_exact_tags_and_canary_evidence() -> None:
    text = _workflow()

    assert 'test "$RELEASE_REF" = "refs/tags/v$version"' in text
    assert 'test "$(git rev-parse HEAD)" = "$(git rev-list -n 1 "$RELEASE_REF")"' in text
    assert "canary tags must contain a PEP 440 prerelease version" in text
    assert "stable tags must contain a final PEP 440 version" in text
    assert "voicey-canary-${{ inputs.canary_run_id }}" in text
    assert "--canary-report canary-evidence/p4-release-report.json" in text


def test_release_workflow_publishes_only_verified_complete_bundle() -> None:
    text = _workflow()

    assert "mkdir -p release-bundle/dist" in text
    assert "sha256sum dist/* p4-release-report.json > SHA256SUMS" in text
    assert "sha256sum --check SHA256SUMS" in text
    assert "find dist -maxdepth 1 -name '*.whl'" in text
    assert "find dist -maxdepth 1 -name '*.tar.gz'" in text
    assert "packages-dir: release-bundle/dist/" in text
    assert "attestations: true" in text
    assert "skip-existing: false" in text
