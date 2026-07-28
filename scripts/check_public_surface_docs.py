#!/usr/bin/env python3
"""Require release notes and docs whenever public snapshots change."""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path

from voicekit.release.snapshots import public_surface_docs_errors


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", required=True, help="Base Git revision for the PR diff.")
    args = parser.parse_args()
    completed = subprocess.run(
        ["git", "diff", "--name-only", "--diff-filter=ACMRT", f"{args.base}...HEAD"],
        check=True,
        capture_output=True,
        text=True,
    )
    changed = {Path(line) for line in completed.stdout.splitlines() if line}
    errors = public_surface_docs_errors(changed)
    if errors:
        for error in errors:
            print(f"public surface docs error: {error}")
        return 1
    print("Public-surface snapshot changes have matching release notes and docs.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
