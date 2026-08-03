from __future__ import annotations

import importlib.metadata
import warnings

import pytest

from voicey.compatibility import (
    RUNTIME_REQUIREMENTS,
    RuntimeCompatibilityWarning,
    RuntimeRequirement,
    inspect_runtime_compatibility,
    warn_runtime_compatibility,
)


def test_runtime_requirements_are_internally_consistent() -> None:
    assert set(RUNTIME_REQUIREMENTS) == {"pipecat", "livekit"}
    assert RUNTIME_REQUIREMENTS["pipecat"].tested_versions == ("1.6.0",)
    assert RUNTIME_REQUIREMENTS["livekit"].tested_versions == ("1.6.7",)


def test_requirement_rejects_empty_and_out_of_range_evidence() -> None:
    with pytest.raises(ValueError, match="at least one"):
        RuntimeRequirement("pipecat", "pipecat-ai", ">=1,<2", (), "pipecat")
    with pytest.raises(ValueError, match="outside"):
        RuntimeRequirement("pipecat", "pipecat-ai", ">=1,<2", ("2.0.0",), "pipecat")


@pytest.mark.parametrize(
    ("version", "status"),
    [
        ("1.6.0", "supported"),
        ("1.6.0+vendor.1", "supported"),
        ("1.5.0", "out-of-range"),
        ("1.6.1", "out-of-range"),
        ("not-a-version", "invalid"),
    ],
)
def test_runtime_compatibility_statuses(version: str, status: str) -> None:
    report = inspect_runtime_compatibility(
        "pipecat",
        version_provider=lambda _distribution: version,
    )

    assert report.status == status
    assert report.supported is (status == "supported")
    assert (report.warning is None) is (status == "supported")


def test_runtime_compatibility_reports_missing_distribution() -> None:
    def missing(_distribution: str) -> str:
        raise importlib.metadata.PackageNotFoundError

    report = inspect_runtime_compatibility("livekit", version_provider=missing)

    assert report.status == "missing"
    assert report.installed_version is None
    assert "not installed" in (report.warning or "")


def test_runtime_warning_is_loud_but_non_fatal() -> None:
    with pytest.warns(RuntimeCompatibilityWarning, match="not certified"):
        report = warn_runtime_compatibility(
            "livekit",
            version_provider=lambda _distribution: "2.0.0",
        )

    assert report.status == "out-of-range"


def test_supported_runtime_does_not_warn() -> None:
    with warnings.catch_warnings(record=True) as captured:
        report = warn_runtime_compatibility(
            "livekit",
            version_provider=lambda _distribution: "1.6.7",
        )

    assert report.supported
    assert captured == []
