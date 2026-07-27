from __future__ import annotations

import json
import re
from importlib.metadata import version
from pathlib import Path
from typing import Any, cast

ROOT = Path(__file__).parents[2]
RUNTIMES = ("pipecat", "livekit")
REQUIRED_FEATURES = {
    "native_workflows",
    "greeting",
    "tool_sequence",
    "webhook_payload",
    "config_mapping",
    "fenced_lifecycle",
    "web_channel",
    "twilio_phone",
    "telnyx_phone",
    "recording",
    "dtmf",
    "cold_transfer",
    "warm_transfer",
}


def test_checked_in_parity_matrix_is_complete_and_evidence_backed() -> None:
    matrix = cast(
        "dict[str, Any]",
        json.loads((ROOT / "docs" / "runtime-parity-matrix.json").read_text()),
    )
    rows = cast("list[dict[str, Any]]", matrix["rows"])

    assert matrix["schema_version"] == 1
    assert matrix["runtimes"] == {
        "pipecat": version("pipecat-ai"),
        "livekit": version("livekit-agents"),
    }
    assert {row["feature"] for row in rows} == REQUIRED_FEATURES
    assert len(rows) == len(REQUIRED_FEATURES)
    for row in rows:
        assert row["contract"]
        for runtime in RUNTIMES:
            cell = cast("dict[str, str]", row[runtime])
            assert cell["status"] in {"supported", "declared_exclusion"}
            _assert_evidence(cell["evidence"])
            if cell["status"] == "declared_exclusion":
                assert cell["reason"]
                assert re.fullmatch(r"P[3-4]", cell["target_phase"])
            else:
                assert "reason" not in cell
                assert "target_phase" not in cell

    exclusions = [
        (row["feature"], runtime)
        for row in rows
        for runtime in RUNTIMES
        if row[runtime]["status"] == "declared_exclusion"
    ]
    assert exclusions == [("warm_transfer", "pipecat")]


def _assert_evidence(reference: str) -> None:
    relative, _, selector = reference.partition("::")
    relative = relative.split("#", maxsplit=1)[0]
    path = ROOT / relative
    assert path.is_file(), reference
    if not selector:
        return
    function_name = selector.split("[", maxsplit=1)[0]
    source = path.read_text(encoding="utf-8")
    assert f"def {function_name}(" in source, reference
