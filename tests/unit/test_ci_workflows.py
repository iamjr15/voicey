from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).parents[2]
WORKFLOWS = ROOT / ".github" / "workflows"


def _workflow(name: str) -> str:
    return (WORKFLOWS / name).read_text(encoding="utf-8")


def _job(workflow: str, name: str) -> str:
    match = re.search(
        rf"^  {re.escape(name)}:\n(?P<body>.*?)(?=^  [a-z0-9-]+:|\Z)",
        workflow,
        re.MULTILINE | re.DOTALL,
    )
    assert match is not None
    return match.group("body")


def test_every_third_party_action_is_immutable() -> None:
    uses: list[tuple[str, str]] = []
    for workflow in sorted(WORKFLOWS.glob("*.yml")):
        for action in re.findall(r"^\s*uses:\s+([^\s]+)", workflow.read_text(), re.MULTILINE):
            uses.append((workflow.name, action))

    assert uses
    assert all(re.fullmatch(r"[^@]+@[0-9a-f]{40}", action) for _, action in uses), uses


def test_branch_ci_resolves_current_wheel_without_claiming_a_release_channel() -> None:
    branch_workflows = "\n".join(
        _workflow(name) for name in ("ci.yml", "p4-aggregate.yml", "security.yml")
    )

    assert "voicey-0.0.0.dev0" not in branch_workflows
    assert "--channel verification" in _workflow("ci.yml")
    assert "find dist -maxdepth 1 -name '*.whl'" in branch_workflows


def test_full_python_matrices_install_every_supported_integration() -> None:
    workflow = _workflow("ci.yml")
    for job_name in ("quality", "integration-edges"):
        job = _job(workflow, job_name)
        assert job.count("run: uv sync --frozen --all-extras") == 1


def test_p4_aggregate_keeps_generated_evidence_out_of_source_scan_scope() -> None:
    workflow = _workflow("p4-aggregate.yml")
    assert "--report .voicey/verification/p4-gate-report.json" in workflow
    assert "path: .voicey/verification/*.json" in workflow


def test_registry_release_tag_namespaces_cannot_overlap() -> None:
    npm = _workflow("publish.yml")
    pypi = _workflow("release.yml")

    assert '"npm-v*"' in npm
    assert '"refs/tags/npm-v$version"' in npm
    assert "working-directory: npm/voicey" in npm
    assert 'test "$RELEASE_REF" = "refs/tags/v$version"' in pypi
    assert "npm-v" not in pypi
