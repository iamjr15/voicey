"""Run the bounded P4 soak and write its truthful machine-readable report."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import tempfile
from dataclasses import asdict
from pathlib import Path
from typing import cast

from voicekit.testing.soak import (
    SoakConfig,
    SoakRuntime,
    run_engine_soak,
)


async def _main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--duration-s", type=float, default=86_400)
    parser.add_argument("--max-concurrent", type=int, default=8)
    parser.add_argument("--call-hold-s", type=float, default=1.0)
    parser.add_argument(
        "--runtime",
        choices=("pipecat", "livekit", "both"),
        default="both",
    )
    parser.add_argument(
        "--report",
        type=Path,
        default=Path(".voicekit/verification/p4-soak-report.json"),
    )
    args = parser.parse_args()
    runtimes = cast(
        "tuple[SoakRuntime, ...]",
        ("pipecat", "livekit") if args.runtime == "both" else (args.runtime,),
    )
    report_path = args.report.expanduser().resolve()
    report_path.parent.mkdir(parents=True, exist_ok=True)

    temporary_root = Path(
        tempfile.mkdtemp(
            dir=report_path.parent,
            prefix=".p4-soak-",
        )
    )
    database = temporary_root / "calls.sqlite3"
    try:
        report = await run_engine_soak(
            database,
            SoakConfig(
                duration_s=args.duration_s,
                max_concurrent=args.max_concurrent,
                call_hold_s=args.call_hold_s,
            ),
            runtimes=runtimes,
        )
    finally:
        await asyncio.to_thread(_remove_database, database, temporary_root)

    payload = {
        **asdict(report),
        "status": "green" if report.healthy else "failed",
        "requested_duration_s": args.duration_s,
        "wall_clock_complete": (args.duration_s >= 86_400 and report.duration_s >= 86_400),
    }
    temporary = report_path.with_suffix(f"{report_path.suffix}.tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, report_path)
    print(json.dumps(payload, sort_keys=True))
    report.assert_healthy()
    return 0


def _remove_database(database: Path, temporary_root: Path) -> None:
    for suffix in ("", "-wal", "-shm"):
        Path(f"{database}{suffix}").unlink(missing_ok=True)
    temporary_root.rmdir()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
