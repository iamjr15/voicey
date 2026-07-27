"""Stable terminal, JSON, and JUnit result contracts."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from xml.etree import ElementTree


@dataclass(frozen=True, slots=True)
class AttemptResult:
    """One runtime attempt for one profile-expanded case."""

    passed: bool
    failures: tuple[str, ...]
    duration_ms: int
    turn_count: int
    transcript: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class CaseResult:
    """One case including stability evidence after any initial failure."""

    name: str
    runtime: str
    tier: str
    attempts: tuple[AttemptResult, ...]

    @property
    def passed(self) -> bool:
        return all(attempt.passed for attempt in self.attempts)

    @property
    def stability(self) -> float:
        passed = sum(attempt.passed for attempt in self.attempts)
        return 100 * passed / len(self.attempts)

    @property
    def duration_ms(self) -> int:
        return sum(attempt.duration_ms for attempt in self.attempts)


@dataclass(frozen=True, slots=True)
class SuiteResult:
    """Unified command result; exit zero only when every case is fully stable."""

    runtime: str
    tier: str
    cases: tuple[CaseResult, ...]

    @property
    def passed(self) -> bool:
        return bool(self.cases) and all(case.passed for case in self.cases)


def result_json(result: SuiteResult, *, next_step: str) -> str:
    """Serialize a stable machine-readable command result."""
    payload = {
        "passed": result.passed,
        "runtime": result.runtime,
        "tier": result.tier,
        "cases": [
            {
                **asdict(case),
                "passed": case.passed,
                "stability": case.stability,
                "duration_ms": case.duration_ms,
            }
            for case in result.cases
        ],
        "next_step": next_step,
    }
    return json.dumps(payload, sort_keys=True)


def write_junit(result: SuiteResult, path: Path) -> Path:
    """Write one deterministic JUnit testcase per profile-expanded scenario."""
    path.parent.mkdir(parents=True, exist_ok=True)
    suite = ElementTree.Element(
        "testsuite",
        {
            "name": f"voicekit.{result.runtime}.{result.tier}",
            "tests": str(len(result.cases)),
            "failures": str(sum(not case.passed for case in result.cases)),
            "time": f"{sum(case.duration_ms for case in result.cases) / 1000:.3f}",
        },
    )
    for case in result.cases:
        node = ElementTree.SubElement(
            suite,
            "testcase",
            {
                "classname": f"voicekit.{case.runtime}.{case.tier}",
                "name": case.name,
                "time": f"{case.duration_ms / 1000:.3f}",
            },
        )
        properties = ElementTree.SubElement(node, "properties")
        ElementTree.SubElement(
            properties,
            "property",
            {"name": "stability_percent", "value": f"{case.stability:.1f}"},
        )
        if not case.passed:
            failures = [
                f"attempt {index + 1}: {failure}"
                for index, attempt in enumerate(case.attempts)
                for failure in attempt.failures
            ]
            failure = ElementTree.SubElement(
                node,
                "failure",
                {"message": f"stability {case.stability:.1f}%"},
            )
            failure.text = "\n".join(failures)
    ElementTree.indent(suite)
    path.write_bytes(ElementTree.tostring(suite, encoding="utf-8", xml_declaration=True))
    return path
